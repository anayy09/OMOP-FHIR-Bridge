"""``ofb`` — the command line for the bridge.

``ofb pipeline`` is what CI runs and what a reader should run first: it loads, checks, exports,
round-trips, regenerates every report and exits non-zero if a conformance gate fails.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import click
import duckdb

from . import constants as K
from . import reports
from .checks import Checker, gate
from .ddl import create_tables, schema
from .etl import Loader
from .export import Exporter, validate_structural
from .fhir_source import FhirCorpus
from .hl7v2 import translate_directory
from .roundtrip import RoundTrip, resource_coverage
from .vocab import ConceptMap

DEFAULT_DB = "out/omop.duckdb"
DEFAULT_REPORTS = "docs/reports"
DEFAULT_EXPORT = "out/fhir-export"


def _connect(db: str, *, must_exist: bool = True):
    path = Path(db)
    if must_exist and not path.exists():
        raise click.ClickException(f"{db} does not exist; run `ofb init-db --db {db}` first")
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def _declared_concept_ids(concept_map: ConceptMap) -> set[int]:
    return concept_map.declared_concept_ids() | set(K.VERIFIABLE) - {0}


@dataclass
class _LoadSummary:
    """Enough of a LoadResult for `ofb roundtrip` to run as its own command."""

    rows_by_source_type: dict = field(default_factory=dict)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="omop-fhir-bridge")
def main() -> None:
    """Bidirectional FHIR R4 <-> OMOP CDM v5.4 bridge."""


@main.command("init-db")
@click.option("--db", default=DEFAULT_DB, show_default=True)
@click.option("--force", is_flag=True, help="replace an existing database file")
def init_db(db: str, force: bool) -> None:
    """Create an empty OMOP CDM v5.4 database from the vendored OHDSI DDL."""
    path = Path(db)
    if path.exists():
        if not force:
            raise click.ClickException(f"{db} already exists; pass --force to replace it")
        path.unlink()
    con = _connect(db, must_exist=False)
    created = create_tables(con)
    con.close()
    click.echo(f"created {len(created)} CDM tables in {db} from the vendored OHDSI v5.4 DDL")


@main.command()
@click.option("--src", default="data/fhir", show_default=True)
@click.option("--db", default=DEFAULT_DB, show_default=True)
@click.option("--reports", "reports_dir", default=DEFAULT_REPORTS, show_default=True)
@click.option("--concept-map", default=None, help="override the committed concept map")
@click.option("--source-name", default="Synthea FHIR R4 sample", show_default=True)
def load(src: str, db: str, reports_dir: str, concept_map: str | None, source_name: str) -> None:
    """Map a FHIR R4 corpus into OMOP tables."""
    corpus = FhirCorpus.load(src)
    vocab = ConceptMap.load(concept_map) if concept_map else ConceptMap.load()
    con = _connect(db)
    result = Loader(con, vocab, source_name=source_name).load(corpus)
    con.close()
    out = Path(reports_dir)
    reports.write_load_report(out / "load-report.md", result, corpus)
    reports.write_json(out / "load-summary.json", result.as_dict())
    click.echo(
        f"loaded {sum(result.source_resource_counts.values()):,} FHIR resources -> "
        f"{sum(result.row_counts.values()):,} OMOP rows; "
        f"{result.coverage.get('overall_mapped_share', 0):.1%} of code lookups mapped"
    )
    for warning in result.warnings[:5]:
        click.echo(f"  warning: {warning}")


@main.command()
@click.option("--db", default=DEFAULT_DB, show_default=True)
@click.option("--reports", "reports_dir", default=DEFAULT_REPORTS, show_default=True)
@click.option("--concept-map", default=None)
def check(db: str, reports_dir: str, concept_map: str | None) -> None:
    """Run every conformance check and fail if an error-severity one does."""
    vocab = ConceptMap.load(concept_map) if concept_map else ConceptMap.load()
    con = _connect(db)
    results = Checker(con, schema()).run_all(_declared_concept_ids(vocab))
    con.close()
    ok, errors, warnings = gate(results)
    reports.write_validation_report(
        Path(reports_dir) / "conformance-report.md", results, ok, errors, warnings
    )
    reports.write_json(
        Path(reports_dir) / "conformance-summary.json",
        {
            "gate_passed": ok,
            "failing_errors": errors,
            "failing_warnings": warnings,
            "checks": [r.as_dict() for r in results],
        },
    )
    click.echo(
        f"{len(results)} checks: {'PASS' if ok else 'FAIL'} "
        f"({errors} error-severity failing, {warnings} warning)"
    )
    for result in results:
        if not result.passed:
            click.echo(f"  {result.severity}: {result.name} {result.target} -> {result.failures}")
    if not ok:
        raise SystemExit(1)


@main.command()
@click.option("--db", default=DEFAULT_DB, show_default=True)
@click.option("--out", "out_dir", default=DEFAULT_EXPORT, show_default=True)
@click.option("--reports", "reports_dir", default=DEFAULT_REPORTS, show_default=True)
@click.option("--concept-map", default=None)
@click.option("--structural/--no-structural", default=True, help="validate with FHIR R4B models")
@click.option("--fhir-server", default=None, help="also run $validate against this FHIR base URL")
@click.option("--upload", is_flag=True, help="PUT the exported resources to --fhir-server")
def export(
    db: str,
    out_dir: str,
    reports_dir: str,
    concept_map: str | None,
    structural: bool,
    fhir_server: str | None,
    upload: bool,
) -> None:
    """Export OMOP tables back to FHIR R4 and validate the result."""
    vocab = ConceptMap.load(concept_map) if concept_map else ConceptMap.load()
    con = _connect(db)
    exporter = Exporter(con, vocab)
    resources = exporter.export()
    counts = exporter.write_ndjson(resources, Path(out_dir))
    con.close()

    summary = {"counts": counts, "total": sum(counts.values())}
    if structural:
        summary["structural_validation"] = validate_structural(resources)
        passed = summary["structural_validation"]["passed"]
        total = summary["structural_validation"]["total"]
        click.echo(f"structural validation (FHIR R4B models): {passed}/{total} passed")
    reports.write_json(Path(reports_dir) / "export-summary.json", summary)
    if fhir_server:
        from .fhir_server import upload_resources, validate_resources, wait_until_ready

        # HAPI takes tens of seconds to boot, so `docker compose up -d` returning does not mean the
        # server is answering. Waiting on /metadata beats a sleep that is either flaky or wasteful.
        click.echo(f"waiting for {fhir_server} to become ready...")
        if not wait_until_ready(fhir_server):
            raise click.ClickException(
                f"{fhir_server} never answered /metadata; is the server up?"
            )

        # Written to its own report rather than into export-summary.json: that file has to stay
        # byte-identical between runs for CI's report-drift check to mean anything, and this one
        # depends on a server being up.
        server_summary = validate_resources(fhir_server, resources)
        if upload:
            server_summary["upload"] = upload_resources(fhir_server, resources)
        reports.write_json(Path(reports_dir) / "fhir-server-validation.json", server_summary)
        reports.write_server_validation_report(
            Path(reports_dir) / "fhir-server-validation.md", server_summary
        )
        click.echo(
            f"server $validate: {server_summary['total_passed']}/"
            f"{server_summary['total_validated']} passed against "
            f"{server_summary['server'].get('software')} "
            f"{server_summary['server'].get('softwareVersion')}"
        )
    click.echo(f"exported {sum(counts.values()):,} resources to {out_dir}")


@main.command()
@click.option("--src", default="data/fhir", show_default=True)
@click.option("--db", default=DEFAULT_DB, show_default=True)
@click.option("--reports", "reports_dir", default=DEFAULT_REPORTS, show_default=True)
@click.option("--concept-map", default=None)
def roundtrip(src: str, db: str, reports_dir: str, concept_map: str | None) -> None:
    """Compare the original FHIR against FHIR re-exported from OMOP, field by field."""
    vocab = ConceptMap.load(concept_map) if concept_map else ConceptMap.load()
    corpus = FhirCorpus.load(src)
    con = _connect(db)
    exporter = Exporter(con, vocab)
    resources = exporter.export()
    con.close()

    summary_path = Path(reports_dir) / "load-summary.json"
    load_summary = _LoadSummary()
    if summary_path.exists():
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        load_summary.rows_by_source_type = payload.get("rows_by_source_resource_type", {})
    comparison = RoundTrip(corpus, resources).run()
    coverage_rows = resource_coverage(corpus, load_summary, resources)
    from .export import ExportResult

    reports.write_roundtrip_report(
        Path(reports_dir) / "roundtrip-report.md", comparison, coverage_rows, ExportResult()
    )
    reports.write_json(
        Path(reports_dir) / "roundtrip-summary.json",
        {"comparison": comparison, "resource_coverage": coverage_rows},
    )
    totals = comparison["totals"]
    click.echo(
        f"round trip: {totals['compared']:,} field comparisons, "
        f"{totals['retained']:,} retained, {totals['transformed']:,} transformed, "
        f"{totals['dropped']:,} dropped"
    )


@main.command("hl7v2")
@click.option("--src", default="data/hl7v2", show_default=True)
@click.option("--out", "out_path", default="out/hl7v2-bundle.json", show_default=True)
def hl7v2_command(src: str, out_path: str) -> None:
    """Translate HL7 v2 ADT messages into a FHIR transaction bundle."""
    result = translate_directory(src)
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.bundle, indent=2), encoding="utf-8", newline="\n")
    click.echo(
        f"translated {result.messages} ADT message(s) {result.by_event} -> "
        f"{result.resources} in {out_path}"
    )
    for warning in result.warnings:
        click.echo(f"  warning: {warning}")


@main.command()
@click.option("--src", default="data/fhir", show_default=True)
@click.option("--hl7v2-src", default="data/hl7v2", show_default=True)
@click.option("--db", default=DEFAULT_DB, show_default=True)
@click.option("--out", "out_dir", default=DEFAULT_EXPORT, show_default=True)
@click.option("--reports", "reports_dir", default=DEFAULT_REPORTS, show_default=True)
@click.option("--fhir-server", default=None, help="also run $validate against this FHIR base URL")
@click.option("--include-hl7v2/--no-include-hl7v2", default=True)
@click.pass_context
def pipeline(
    ctx: click.Context,
    src: str,
    hl7v2_src: str,
    db: str,
    out_dir: str,
    reports_dir: str,
    fhir_server: str | None,
    include_hl7v2: bool,
) -> None:
    """init-db -> load -> check -> export -> roundtrip, regenerating every report. CI runs this."""
    ctx.invoke(init_db, db=db, force=True)
    if include_hl7v2 and Path(hl7v2_src).exists():
        bundle_path = Path(out_dir).parent / "hl7v2-bundle.json"
        ctx.invoke(hl7v2_command, src=hl7v2_src, out_path=str(bundle_path))
        # The ADT-derived bundle is loaded as a second corpus so the v2 feed goes through the same
        # mapper, checks and report as the FHIR feed.
        staged = Path(out_dir).parent / "staged-fhir"
        staged.mkdir(parents=True, exist_ok=True)
        for path in sorted(Path(src).glob("*.json")):
            target = staged / path.name
            if not target.exists() or target.stat().st_mtime < path.stat().st_mtime:
                target.write_bytes(path.read_bytes())
        (staged / "_hl7v2-derived.json").write_bytes(bundle_path.read_bytes())
        src = str(staged)
    ctx.invoke(load, src=src, db=db, reports_dir=reports_dir)
    ctx.invoke(export, db=db, out_dir=out_dir, reports_dir=reports_dir, fhir_server=fhir_server)
    ctx.invoke(roundtrip, src=src, db=db, reports_dir=reports_dir)
    ctx.invoke(check, db=db, reports_dir=reports_dir)


if __name__ == "__main__":
    main()
