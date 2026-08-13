"""Conformance checks generated from the vendored OHDSI DDL.

The checks in this module are not a hand-written list of things that seemed worth checking. Most of
them are *generated*: NOT NULL checks come from the DDL's nullability, length checks from its
``varchar(n)`` declarations, uniqueness from ``primary_keys.sql``, and referential closure from
``constraints.sql``. A hand-maintained list silently stops matching the specification; a generated
one cannot.

Three checks are hand-written because they encode clinical rather than structural truth: an event
cannot precede its patient's birth, an event's visit must belong to the same patient, and a death
date should not precede the last recorded event.

Concept foreign keys are handled separately and honestly. Nearly every ``*_concept_id`` in the CDM
has a foreign key to CONCEPT, and this repository does not ship the CONCEPT table -- the vocabulary
is licence-gated. So instead of skipping those constraints, ``concept_id_declared`` asserts the
weaker but checkable property that every non-zero concept_id written by the ETL comes from either
the committed concept map or the verified constants, i.e. that the loader never invented one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ddl import Schema, schema
from .etl import CDM_TABLES

# Tables this bridge populates; foreign keys into anything else (CONCEPT, VOCABULARY, EPISODE ...)
# cannot be closed against data that is not here, and are reported as not-applicable.
POPULATED = set(CDM_TABLES)

TEMPORAL_PAIRS = (
    ("observation_period", "observation_period_start_date", "observation_period_end_date"),
    ("visit_occurrence", "visit_start_date", "visit_end_date"),
    ("condition_occurrence", "condition_start_date", "condition_end_date"),
    ("drug_exposure", "drug_exposure_start_date", "drug_exposure_end_date"),
    ("procedure_occurrence", "procedure_date", "procedure_end_date"),
)

EVENT_TABLES = (
    ("visit_occurrence", "visit_start_date"),
    ("condition_occurrence", "condition_start_date"),
    ("procedure_occurrence", "procedure_date"),
    ("drug_exposure", "drug_exposure_start_date"),
    ("measurement", "measurement_date"),
    ("observation", "observation_date"),
)


@dataclass
class CheckResult:
    name: str
    target: str
    severity: str
    examined: int
    failures: int
    detail: str
    examples: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.failures == 0

    def as_dict(self) -> dict:
        return {
            "check": self.name,
            "target": self.target,
            "severity": self.severity,
            "examined": self.examined,
            "failures": self.failures,
            "passed": self.passed,
            "detail": self.detail,
            "examples": self.examples[:5],
        }


class Checker:
    def __init__(self, con, cdm: Schema | None = None):
        self.con = con
        self.schema = cdm or schema()

    # ------------------------------------------------------------------ util
    def _tables_present(self) -> list[str]:
        rows = self.con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
        present = {r[0].lower() for r in rows}
        return [t for t in CDM_TABLES if t in present]

    def _count(self, sql: str, params: tuple = ()) -> int:
        return int(self.con.execute(sql, params).fetchone()[0])

    def _rows(self, table: str) -> int:
        return self._count(f"SELECT count(*) FROM {table}")

    # --------------------------------------------------------------- checks
    def not_null(self) -> list[CheckResult]:
        results = []
        for table in self._tables_present():
            total = self._rows(table)
            for column in self.schema[table].not_null:
                if total == 0:
                    continue
                failures = self._count(
                    f"SELECT count(*) FROM {table} WHERE {column.name} IS NULL"
                )
                results.append(
                    CheckResult(
                        name="not_null",
                        target=f"{table}.{column.name}",
                        severity="error",
                        examined=total,
                        failures=failures,
                        detail="column is NOT NULL in the OHDSI DDL",
                    )
                )
        return results

    def varchar_length(self) -> list[CheckResult]:
        results = []
        for table in self._tables_present():
            total = self._rows(table)
            for column in self.schema[table].bounded_text:
                if total == 0:
                    continue
                failures = self._count(
                    f"SELECT count(*) FROM {table} "
                    f"WHERE {column.name} IS NOT NULL AND length(CAST({column.name} AS VARCHAR)) > ?",
                    (column.max_length,),
                )
                examples = []
                if failures:
                    examples = [
                        r[0]
                        for r in self.con.execute(
                            f"SELECT DISTINCT {column.name} FROM {table} "
                            f"WHERE length(CAST({column.name} AS VARCHAR)) > ? LIMIT 5",
                            (column.max_length,),
                        ).fetchall()
                    ]
                results.append(
                    CheckResult(
                        name="varchar_length",
                        target=f"{table}.{column.name}",
                        severity="error",
                        examined=total,
                        failures=failures,
                        detail=f"DDL declares varchar({column.max_length})",
                        examples=examples,
                    )
                )
        return results

    def primary_key_unique(self) -> list[CheckResult]:
        results = []
        for table in self._tables_present():
            key = self.schema[table].primary_key
            if not key:
                continue
            columns = ", ".join(key)
            total = self._rows(table)
            failures = self._count(
                f"SELECT coalesce(sum(n - 1), 0) FROM "
                f"(SELECT count(*) AS n FROM {table} GROUP BY {columns} HAVING count(*) > 1)"
            )
            results.append(
                CheckResult(
                    name="primary_key_unique",
                    target=f"{table}({columns})",
                    severity="error",
                    examined=total,
                    failures=failures,
                    detail="primary key from OHDSI primary_keys.sql; DuckDB cannot enforce it "
                    "post-hoc, so it is asserted here",
                )
            )
        return results

    def foreign_key_closure(self) -> list[CheckResult]:
        results, skipped = [], set()
        present = set(self._tables_present())
        for fk in self.schema.foreign_keys:
            if fk.table not in present:
                continue
            if fk.ref_table not in POPULATED or fk.ref_table not in present:
                skipped.add(fk.ref_table)
                continue
            total = self._rows(fk.table)
            if total == 0:
                continue
            failures = self._count(
                f"SELECT count(*) FROM {fk.table} c "
                f"LEFT JOIN {fk.ref_table} p ON c.{fk.column} = p.{fk.ref_column} "
                f"WHERE c.{fk.column} IS NOT NULL AND p.{fk.ref_column} IS NULL"
            )
            results.append(
                CheckResult(
                    name="foreign_key_closure",
                    target=f"{fk.table}.{fk.column} -> {fk.ref_table}.{fk.ref_column}",
                    severity="error",
                    examined=total,
                    failures=failures,
                    detail="foreign key from OHDSI constraints.sql",
                )
            )
        if skipped:
            results.append(
                CheckResult(
                    name="foreign_key_not_applicable",
                    target=", ".join(sorted(skipped)),
                    severity="info",
                    examined=0,
                    failures=0,
                    detail="referenced tables this bridge does not populate (vocabulary tables are "
                    "licence-gated); see concept_id_declared for what is checked instead",
                )
            )
        return results

    def concept_id_declared(self, declared: set[int]) -> list[CheckResult]:
        results = []
        for table in self._tables_present():
            total = self._rows(table)
            if total == 0:
                continue
            for column in self.schema[table].columns:
                if not column.is_concept_id:
                    continue
                rows = self.con.execute(
                    f"SELECT DISTINCT {column.name} FROM {table} "
                    f"WHERE {column.name} IS NOT NULL AND {column.name} <> 0"
                ).fetchall()
                undeclared = sorted({int(r[0]) for r in rows} - declared)
                results.append(
                    CheckResult(
                        name="concept_id_declared",
                        target=f"{table}.{column.name}",
                        severity="error",
                        examined=len(rows),
                        failures=len(undeclared),
                        detail="every non-zero concept_id must come from the committed concept map "
                        "or the verified constants",
                        examples=undeclared[:5],
                    )
                )
        return results

    def temporal_order(self) -> list[CheckResult]:
        results = []
        present = set(self._tables_present())
        for table, start, end in TEMPORAL_PAIRS:
            if table not in present:
                continue
            total = self._rows(table)
            if total == 0:
                continue
            failures = self._count(
                f"SELECT count(*) FROM {table} WHERE {end} IS NOT NULL AND {end} < {start}"
            )
            results.append(
                CheckResult(
                    name="temporal_order",
                    target=f"{table}.{start} <= {end}",
                    severity="error",
                    examined=total,
                    failures=failures,
                    detail="end date must not precede start date",
                )
            )
        return results

    def event_after_birth(self) -> list[CheckResult]:
        results = []
        present = set(self._tables_present())
        if "person" not in present:
            return results
        for table, column in EVENT_TABLES:
            if table not in present:
                continue
            total = self._rows(table)
            if total == 0:
                continue
            failures = self._count(
                f"SELECT count(*) FROM {table} e JOIN person p USING (person_id) "
                f"WHERE e.{column} < CAST(p.birth_datetime AS DATE)"
            )
            results.append(
                CheckResult(
                    name="event_after_birth",
                    target=f"{table}.{column}",
                    severity="error",
                    examined=total,
                    failures=failures,
                    detail="a clinical event cannot precede the patient's date of birth",
                )
            )
        return results

    def event_within_observation_period(self) -> list[CheckResult]:
        results = []
        present = set(self._tables_present())
        if "observation_period" not in present:
            return results
        for table, column in EVENT_TABLES:
            if table not in present:
                continue
            total = self._rows(table)
            if total == 0:
                continue
            failures = self._count(
                f"SELECT count(*) FROM {table} e JOIN observation_period o USING (person_id) "
                f"WHERE e.{column} < o.observation_period_start_date "
                f"   OR e.{column} > o.observation_period_end_date"
            )
            results.append(
                CheckResult(
                    name="event_within_observation_period",
                    target=f"{table}.{column}",
                    severity="error",
                    examined=total,
                    failures=failures,
                    detail="OBSERVATION_PERIOD is derived from these events, so an event outside it "
                    "means the derivation is wrong",
                )
            )
        return results

    def visit_person_consistency(self) -> list[CheckResult]:
        results = []
        present = set(self._tables_present())
        if "visit_occurrence" not in present:
            return results
        for table, _column in EVENT_TABLES:
            if table == "visit_occurrence" or table not in present:
                continue
            total = self._rows(table)
            if total == 0:
                continue
            failures = self._count(
                f"SELECT count(*) FROM {table} e "
                f"JOIN visit_occurrence v ON e.visit_occurrence_id = v.visit_occurrence_id "
                f"WHERE e.person_id <> v.person_id"
            )
            results.append(
                CheckResult(
                    name="visit_person_consistency",
                    target=f"{table}.visit_occurrence_id",
                    severity="error",
                    examined=total,
                    failures=failures,
                    detail="an event's visit must belong to the same person as the event",
                )
            )
        return results

    def death_not_before_last_event(self) -> list[CheckResult]:
        present = set(self._tables_present())
        if "death" not in present or self._rows("death") == 0:
            return []
        union = " UNION ALL ".join(
            f"SELECT person_id, {column} AS d FROM {table}"
            for table, column in EVENT_TABLES
            if table in present
        )
        failures = self._count(
            "SELECT count(*) FROM death x WHERE x.death_date < "
            f"(SELECT max(d) FROM ({union}) e WHERE e.person_id = x.person_id)"
        )
        return [
            CheckResult(
                name="death_not_before_last_event",
                target="death.death_date",
                severity="warning",
                examined=self._rows("death"),
                failures=failures,
                detail="post-mortem events are legitimate in some feeds (late-arriving results), "
                "so this is a warning rather than an error",
            )
        ]

    def person_has_observation_period(self) -> list[CheckResult]:
        """A person with no observation period is unusable in an OHDSI analysis.

        The DDL does not require it -- OBSERVATION_PERIOD has a foreign key to PERSON, not the other
        way round -- so this is a data-quality assertion rather than a structural one. It fires
        legitimately: a registration-only ADT feed produces people with demographics and no clinical
        events, and the honest response is a warning naming the count, not a fabricated period.
        """
        present = set(self._tables_present())
        if not {"person", "observation_period"} <= present:
            return []
        total = self._rows("person")
        if total == 0:
            return []
        failures = self._count(
            "SELECT count(*) FROM person p WHERE NOT EXISTS "
            "(SELECT 1 FROM observation_period o WHERE o.person_id = p.person_id)"
        )
        return [
            CheckResult(
                name="person_has_observation_period",
                target="person.person_id",
                severity="warning",
                examined=total,
                failures=failures,
                detail="a person with no clinical event has no observation period and cannot enter "
                "an OHDSI cohort; not fabricated, reported",
            )
        ]

    # ------------------------------------------------------------------- run
    def run_all(self, declared_concept_ids: set[int]) -> list[CheckResult]:
        results: list[CheckResult] = []
        results += self.not_null()
        results += self.varchar_length()
        results += self.primary_key_unique()
        results += self.foreign_key_closure()
        results += self.concept_id_declared(declared_concept_ids)
        results += self.temporal_order()
        results += self.event_after_birth()
        results += self.event_within_observation_period()
        results += self.visit_person_consistency()
        results += self.death_not_before_last_event()
        results += self.person_has_observation_period()
        return results


def gate(results: list[CheckResult]) -> tuple[bool, int, int]:
    """(ok, failing_error_checks, failing_warning_checks) -- CI fails on the first number."""
    errors = sum(1 for r in results if r.severity == "error" and not r.passed)
    warnings = sum(1 for r in results if r.severity == "warning" and not r.passed)
    return errors == 0, errors, warnings
