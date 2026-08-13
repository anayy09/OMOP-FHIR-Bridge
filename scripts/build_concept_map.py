"""Build the committed concept map by resolving the sample corpus's codes against OHDSI.

Why this script exists rather than a hand-written CSV: the OHDSI vocabulary is licence-gated and
cannot be redistributed, but a *derived map covering only the codes in this repository's own sample
data* can be, and it can carry the provenance of how each row was obtained. Every row records the
service that answered, the resolution path taken, and the date. Nothing is filled in from memory.

Resolution, per distinct (system, code):

1. ``POST /vocabulary/lookup/sourcecodes`` returns every concept sharing that CONCEPT_CODE. The one
   whose VOCABULARY_ID matches the FHIR system is the source concept.
2. If that concept is standard and valid, it is also the target (``resolution=standard``).
3. If it is not standard, follow ``Maps to`` through ``/vocabulary/concept/{id}/related`` to a valid
   standard concept (``resolution=mapped``).
4. If a source concept exists but no standard target does, the row keeps the source concept and a
   target of 0 (``resolution=source_only``).
5. If nothing is found at all, the row is written with target 0 (``resolution=unresolved``) so the
   miss is visible in the committed map rather than only at run time.

Usage:
    python scripts/build_concept_map.py --src data/fhir
        --out src/omop_fhir_bridge/vocab_data/concept_map.csv
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omop_fhir_bridge.constants import VOCABULARY_BY_SYSTEM  # noqa: E402
from omop_fhir_bridge.fhir_source import FhirCorpus  # noqa: E402
from omop_fhir_bridge.vocab import write_map  # noqa: E402

DEFAULT_SERVICE = "https://atlas-demo.ohdsi.org/WebAPI"
CACHE = Path(__file__).resolve().parents[1] / ".cache" / "vocab_lookups.json"
BATCH = 40
PAUSE_SECONDS = 0.4


def _post(url: str, payload) -> object:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode())


def _get(url: str) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode())


class Resolver:
    def __init__(self, service: str):
        self.service = service.rstrip("/")
        self.cache: dict = json.loads(CACHE.read_text()) if CACHE.exists() else {}
        self.calls = 0

    def save(self) -> None:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(self.cache, indent=0))

    def service_version(self) -> str:
        try:
            info = _get(f"{self.service}/info")
            return str(info.get("version", "unknown"))
        except Exception:  # noqa: BLE001 - provenance is best-effort, never fatal
            return "unknown"

    def source_codes(self, codes: list[str]) -> dict[str, list[dict]]:
        """CONCEPT_CODE -> concept records, batched and cached."""
        out: dict[str, list[dict]] = {}
        pending = []
        for code in codes:
            cached = self.cache.get(f"code:{code}")
            if cached is None:
                pending.append(code)
            else:
                out[code] = cached
        for start in range(0, len(pending), BATCH):
            batch = pending[start : start + BATCH]
            records = _post(f"{self.service}/vocabulary/lookup/sourcecodes", batch)
            self.calls += 1
            grouped: dict[str, list[dict]] = {code: [] for code in batch}
            for record in records or []:
                grouped.setdefault(record.get("CONCEPT_CODE", ""), []).append(record)
            for code in batch:
                self.cache[f"code:{code}"] = grouped.get(code, [])
                out[code] = grouped.get(code, [])
            print(f"  resolved {min(start + BATCH, len(pending))}/{len(pending)} new codes")
            time.sleep(PAUSE_SECONDS)
        return out

    def maps_to(self, concept_id: int) -> dict | None:
        key = f"mapsto:{concept_id}"
        if key in self.cache:
            return self.cache[key]
        try:
            related = _get(f"{self.service}/vocabulary/concept/{concept_id}/related")
            self.calls += 1
            time.sleep(PAUSE_SECONDS)
        except urllib.error.HTTPError:
            related = []
        target = None
        for record in related or []:
            names = {r.get("RELATIONSHIP_NAME") for r in record.get("RELATIONSHIPS") or []}
            if "Maps to" in names and record.get("STANDARD_CONCEPT") == "S":
                target = record
                break
        self.cache[key] = target
        return target


def collect_codings(corpus: FhirCorpus) -> dict[tuple[str, str], str]:
    """(system, code) -> display, over every place this bridge reads a code from."""
    found: dict[tuple[str, str], str] = {}

    def add(codeable: object) -> None:
        # AllergyIntolerance.type is a bare code, not a CodeableConcept; several R4 elements
        # share a name across both shapes, so anything that is not an object is skipped.
        if not isinstance(codeable, dict):
            return
        for coding in codeable.get("coding") or []:
            system, code = coding.get("system"), coding.get("code")
            if system in VOCABULARY_BY_SYSTEM and code:
                found.setdefault((system, str(code)), coding.get("display") or "")

    def add_quantity(node: dict) -> None:
        quantity = node.get("valueQuantity") or {}
        code, system = quantity.get("code"), quantity.get("system")
        if code and system in VOCABULARY_BY_SYSTEM:
            found.setdefault((system, str(code)), quantity.get("unit") or "")

    for resource in corpus.iter_all():
        rtype = resource.get("resourceType")
        if rtype in {"Condition", "Procedure", "Observation", "Encounter", "AllergyIntolerance"}:
            add(resource.get("code"))
            types = resource.get("type")
            for type_cc in types if isinstance(types, list) else []:
                add(type_cc)
        if rtype == "MedicationRequest":
            add(resource.get("medicationCodeableConcept"))
        if rtype == "Medication":
            # Reached through MedicationRequest.medicationReference rather than being mapped to a
            # row of its own, but its RxNorm code still has to be in the map.
            add(resource.get("code"))
        if rtype == "Immunization":
            add(resource.get("vaccineCode"))
        if rtype == "Observation":
            add(resource.get("valueCodeableConcept"))
            add_quantity(resource)
            for component in resource.get("component") or []:
                add(component.get("code"))
                add(component.get("valueCodeableConcept"))
                add_quantity(component)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", default="data/fhir")
    parser.add_argument(
        "--out", default="src/omop_fhir_bridge/vocab_data/concept_map.csv"
    )
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    args = parser.parse_args()

    corpus = FhirCorpus.load(args.src)
    codings = collect_codings(corpus)
    print(f"{corpus.total()} resources in {len(corpus.files)} files")
    print(f"{len(codings)} distinct codings to resolve against {args.service}")

    resolver = Resolver(args.service)
    version = resolver.service_version()
    lookups = resolver.source_codes(sorted({code for _system, code in codings}))

    rows, stats = [], Counter()
    today = date.today().isoformat()
    for (system, code), display in sorted(codings.items()):
        vocabulary = VOCABULARY_BY_SYSTEM[system]
        candidates = [c for c in lookups.get(code, []) if c.get("VOCABULARY_ID") == vocabulary]
        valid = [c for c in candidates if c.get("INVALID_REASON") in (None, "V", "")]
        source = next((c for c in valid if c.get("STANDARD_CONCEPT") == "S"), None) or next(
            iter(valid), None
        ) or next(iter(candidates), None)

        target, resolution = None, "unresolved"
        if source is not None:
            if source.get("STANDARD_CONCEPT") == "S":
                target, resolution = source, "standard"
            else:
                target = resolver.maps_to(source["CONCEPT_ID"])
                resolution = "mapped" if target else "source_only"
        stats[resolution] += 1
        rows.append(
            {
                "source_system": system,
                "source_code": code,
                "source_display": display[:120],
                "source_concept_id": (source or {}).get("CONCEPT_ID", 0),
                "target_concept_id": (target or {}).get("CONCEPT_ID", 0),
                "target_concept_name": (target or {}).get("CONCEPT_NAME", ""),
                "target_domain_id": (target or {}).get("DOMAIN_ID", ""),
                "target_vocabulary_id": (target or source or {}).get("VOCABULARY_ID", vocabulary),
                "target_standard_concept": (target or {}).get("STANDARD_CONCEPT", ""),
                "resolution": resolution,
                "resolved_at": today,
                "resolver": args.service,
            }
        )
    resolver.save()

    provenance = {
        "generated_by": "scripts/build_concept_map.py",
        "generated_on": today,
        "vocabulary_source": f"OHDSI WebAPI {version}",
        "vocabulary_service": args.service,
        "source_corpus": args.src,
        "distinct_codings": str(len(rows)),
        "resolution_counts": json.dumps(dict(sorted(stats.items()))),
        "licence_note": (
            "Derived from the OHDSI standardised vocabularies via a public WebAPI instance. "
            "Only the codes present in this repository's sample data are included. "
            "The vocabulary itself is licence-gated and is not redistributed here."
        ),
    }
    write_map(Path(args.out), rows, provenance)
    print(f"wrote {len(rows)} rows to {args.out} in {resolver.calls} service calls")
    for resolution, count in sorted(stats.items()):
        print(f"  {resolution:12s} {count:5d}  ({count / len(rows):.1%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
