"""Re-fetch the vendored OHDSI CDM v5.4 DDL and record what was fetched.

The DDL is committed rather than downloaded at run time, so the build is reproducible offline and a
reader can see exactly which specification the conformance checks are generated from. This script
refreshes it and rewrites ``vendor/README.md`` with the URL, commit, size and SHA-256 of each file --
so "vendored" means "traceable", not "copied from somewhere once".

Usage:
    python scripts/fetch_vendor.py [--ref main] [--dialect duckdb]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from datetime import date
from pathlib import Path

REPO = "OHDSI/CommonDataModel"
FILES = ("ddl", "primary_keys", "constraints", "indices")
ROOT = Path(__file__).resolve().parents[1]


def get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=90) as response:
        return response.read()


def commit_for(ref: str) -> dict:
    payload = json.loads(get(f"https://api.github.com/repos/{REPO}/commits/{ref}").decode())
    return {
        "sha": payload["sha"],
        "date": (payload.get("commit") or {}).get("committer", {}).get("date", "unknown"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--dialect", default="duckdb")
    args = parser.parse_args()

    target = ROOT / "vendor" / "omop-cdm-5.4" / args.dialect
    target.mkdir(parents=True, exist_ok=True)
    commit = commit_for(args.ref)

    records = []
    for name in FILES:
        filename = f"OMOPCDM_{args.dialect}_5.4_{name}.sql"
        url = (
            f"https://raw.githubusercontent.com/{REPO}/{commit['sha']}"
            f"/inst/ddl/5.4/{args.dialect}/{filename}"
        )
        data = get(url)
        (target / filename).write_bytes(data)
        records.append(
            {
                "file": filename,
                "url": url,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
        print(f"  {filename:44s} {len(data):>7,} bytes")

    readme = ROOT / "vendor" / "README.md"
    lines = [
        "# Vendored OMOP CDM v5.4 DDL",
        "",
        f"Fetched by `scripts/fetch_vendor.py` on {date.today().isoformat()}. Do not edit by hand.",
        "",
        f"- Source repository: **{REPO}** (Apache-2.0)",
        f"- Commit: `{commit['sha']}` ({commit['date']})",
        f"- Dialect: `{args.dialect}`, from `inst/ddl/5.4/{args.dialect}/`",
        "",
        "These files are the specification this project is checked against. `ddl.py` parses them to",
        "derive column nullability, `varchar(n)` bounds, primary keys and foreign keys, and every",
        "structural check in `checks.py` is generated from that parse. Editing them by hand would",
        "quietly change what conformance *means* here, which is why they are refreshed by script",
        "and fingerprinted below.",
        "",
        "| file | bytes | SHA-256 |",
        "|---|---|---|",
    ]
    for record in records:
        lines.append(f"| `{record['file']}` | {record['bytes']:,} | `{record['sha256']}` |")
    lines += [
        "",
        "## Why the DuckDB dialect",
        "",
        "DuckDB is the default target because it needs no server, so `ofb pipeline` runs in CI and on",
        "a laptop with no setup. OHDSI publishes the same v5.4 model for PostgreSQL, SQL Server,",
        "Snowflake, BigQuery, Spark and others; re-run this script with `--dialect postgresql` to",
        "vendor those instead. The mapping logic is dialect-independent — only `ddl.py`'s parse and",
        "the connection in `cli.py` care.",
        "",
        "## Manifest",
        "",
        "```json",
        json.dumps({"repo": REPO, "commit": commit, "files": records}, indent=2),
        "```",
        "",
    ]
    readme.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"wrote {readme}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
