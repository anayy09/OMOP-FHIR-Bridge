"""Validation against a real FHIR server, which is the only validation that settles an argument.

Pydantic models check that a resource has the right shape. A HAPI FHIR server running ``$validate``
checks it against the published R4 StructureDefinitions and the terminology bindings that go with
them, and it is the same software a hospital integration team would point at the feed. So the
structural check runs everywhere and this one runs in its own CI job against
``hapiproject/hapi`` from ``docker-compose.yml``.

Failures here are reported per resource type with the server's own OperationOutcome text, not
summarised into a pass/fail, because the interesting output of a validator is which invariant it
thinks you broke.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor


def _request(url: str, payload: dict | None = None, method: str = "GET", timeout: int = 60):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/fhir+json",
            "Accept": "application/fhir+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(body or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": body[:2000]}


def wait_until_ready(base_url: str, timeout_seconds: int = 300, interval: int = 5) -> bool:
    """Poll ``/metadata`` until the server answers. HAPI takes tens of seconds to boot."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            status, body = _request(f"{base_url.rstrip('/')}/metadata", timeout=10)
            if status == 200 and body.get("resourceType") == "CapabilityStatement":
                return True
        except Exception:  # noqa: BLE001 - the server simply is not up yet
            pass
        time.sleep(interval)
    return False


def server_version(base_url: str) -> dict:
    status, body = _request(f"{base_url.rstrip('/')}/metadata", timeout=30)
    if status != 200:
        return {"reachable": False}
    return {
        "reachable": True,
        "fhirVersion": body.get("fhirVersion"),
        "software": (body.get("software") or {}).get("name"),
        "softwareVersion": (body.get("software") or {}).get("version"),
    }


def _issues(outcome: dict) -> list[dict]:
    if outcome.get("resourceType") != "OperationOutcome":
        return []
    return [
        {
            "severity": issue.get("severity"),
            "code": issue.get("code"),
            "diagnostics": (issue.get("diagnostics") or "")[:300],
            "location": (issue.get("expression") or issue.get("location") or [None])[0],
        }
        for issue in outcome.get("issue") or []
    ]


def validate_resources(
    base_url: str,
    resources: dict[str, list[dict]],
    *,
    workers: int = 8,
    limit_per_type: int | None = None,
) -> dict:
    """Run ``$validate`` for every resource and summarise by type and severity."""
    base = base_url.rstrip("/")
    summary: dict = {
        "endpoint": base,
        "server": server_version(base),
        "by_type": {},
        "severity_totals": {},
        "failures": [],
    }
    severity_totals: Counter = Counter()

    def check(item: tuple[str, dict]) -> tuple[str, str | None, list[dict]]:
        rtype, resource = item
        status, body = _request(f"{base}/{rtype}/$validate", resource, method="POST")
        issues = _issues(body)
        if status not in (200, 201) and not issues:
            issues = [
                {
                    "severity": "error",
                    "code": f"http-{status}",
                    "diagnostics": json.dumps(body)[:300],
                    "location": None,
                }
            ]
        return rtype, resource.get("id"), issues

    work: list[tuple[str, dict]] = []
    for rtype, items in resources.items():
        selected = items[:limit_per_type] if limit_per_type else items
        work.extend((rtype, item) for item in selected)

    per_type: dict[str, Counter] = defaultdict(Counter)
    non_blocking: Counter = Counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for rtype, resource_id, issues in pool.map(check, work):
            per_type[rtype]["validated"] += 1
            blocking = [i for i in issues if i["severity"] in ("error", "fatal")]
            for issue in issues:
                severity_totals[issue["severity"] or "unknown"] += 1
                if issue["severity"] not in ("error", "fatal"):
                    # Aggregated rather than listed: "2,768 warnings" is not a finding, but
                    # "2,768 of them are the same best-practice recommendation" is.
                    non_blocking[(issue["diagnostics"] or "")[:120]] += 1
            if blocking:
                per_type[rtype]["failed"] += 1
                if len(summary["failures"]) < 25:
                    summary["failures"].append(
                        {"resourceType": rtype, "id": resource_id, "issues": blocking[:3]}
                    )
            else:
                per_type[rtype]["passed"] += 1

    summary["by_type"] = {
        rtype: {
            "validated": counts["validated"],
            "passed": counts["passed"],
            "failed": counts["failed"],
        }
        for rtype, counts in sorted(per_type.items())
    }
    summary["severity_totals"] = dict(sorted(severity_totals.items()))
    summary["top_non_blocking_issues"] = [
        {"diagnostics": text, "occurrences": count} for text, count in non_blocking.most_common(10)
    ]
    summary["total_validated"] = sum(c["validated"] for c in summary["by_type"].values())
    summary["total_passed"] = sum(c["passed"] for c in summary["by_type"].values())
    summary["total_failed"] = sum(c["failed"] for c in summary["by_type"].values())
    return summary


def upload_resources(base_url: str, resources: dict[str, list[dict]], *, workers: int = 8) -> dict:
    """PUT every resource by id, so a reader can browse the export in a real FHIR UI."""
    base = base_url.rstrip("/")
    statuses: Counter = Counter()

    def put(item: tuple[str, dict]):
        rtype, resource = item
        status, _body = _request(f"{base}/{rtype}/{resource['id']}", resource, method="PUT")
        return status

    work = [(rtype, item) for rtype, items in resources.items() for item in items]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for status in pool.map(put, work):
            statuses[str(status)] += 1
    return {"endpoint": base, "attempted": len(work), "status_counts": dict(sorted(statuses.items()))}
