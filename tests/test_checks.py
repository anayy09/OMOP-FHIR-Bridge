"""Each check has to fail on a database that violates it, or it is decoration.

Every test here corrupts a loaded database in one specific way and asserts that the corresponding
check notices. A check suite that only ever runs against correct data proves nothing.
"""

from __future__ import annotations

from omop_fhir_bridge import constants as K
from omop_fhir_bridge.checks import Checker, gate


def _run(con, concept_map):
    declared = concept_map.declared_concept_ids() | set(K.VERIFIABLE) - {0}
    return Checker(con).run_all(declared)


def _named(results, name: str, contains: str = ""):
    return [r for r in results if r.name == name and contains in r.target]


def _relax(con, table: str) -> None:
    """Rebuild a table without its DDL constraints.

    DuckDB actually enforces the NOT NULL clauses in the OHDSI DDL, so a null cannot be inserted to
    test against -- which is worth knowing: on this engine the generated `not_null` check is a second
    line of defence rather than the only one. It is not redundant, because plenty of OMOP targets
    (and bulk COPY paths) do not enforce it, and because a check that reports a count is more useful
    than a load that aborts on the first bad row. CREATE TABLE AS drops the constraints, which is how
    these tests get a database the engine will let them corrupt.
    """
    con.execute(f"CREATE TABLE {table}_relaxed AS SELECT * FROM {table}")
    con.execute(f"DROP TABLE {table}")
    con.execute(f"ALTER TABLE {table}_relaxed RENAME TO {table}")


def test_clean_load_passes_every_error_check(loaded, concept_map):
    con, _result, _corpus = loaded
    results = _run(con, concept_map)
    ok, errors, _warnings = gate(results)
    assert ok, [r.as_dict() for r in results if not r.passed and r.severity == "error"]
    assert errors == 0
    assert len(results) > 50, "the generated checks should cover many columns, not a handful"


def test_not_null_check_catches_a_null_in_a_required_column(loaded, concept_map):
    con, _result, _corpus = loaded
    _relax(con, "person")
    con.execute("UPDATE person SET year_of_birth = NULL")
    failing = [r for r in _named(_run(con, concept_map), "not_null") if not r.passed]
    assert [r.target for r in failing] == ["person.year_of_birth"]
    assert not gate(_run(con, concept_map))[0]


def test_varchar_length_check_catches_an_overlong_value(loaded, concept_map):
    """LOCATION.state is varchar(2). A feed writing "Massachusetts" has to be caught here rather
    than truncated into a different state."""
    con, _result, _corpus = loaded
    con.execute("UPDATE location SET state = 'Massachusetts'")
    failing = [r for r in _named(_run(con, concept_map), "varchar_length") if not r.passed]
    assert any(r.target == "location.state" for r in failing)
    assert "Massachusetts" in failing[0].examples


def test_primary_key_check_catches_a_duplicate(loaded, concept_map):
    con, _result, _corpus = loaded
    con.execute("INSERT INTO person SELECT * FROM person")
    failing = [r for r in _named(_run(con, concept_map), "primary_key_unique") if not r.passed]
    assert any("person" in r.target for r in failing)


def test_foreign_key_check_catches_an_orphan(loaded, concept_map):
    con, _result, _corpus = loaded
    con.execute("UPDATE condition_occurrence SET person_id = 999999")
    failing = [r for r in _named(_run(con, concept_map), "foreign_key_closure") if not r.passed]
    assert any("condition_occurrence.person_id" in r.target for r in failing)


def test_concept_ids_must_be_declared(loaded, concept_map):
    """The check that stands in for the CONCEPT foreign keys: an id nobody can justify fails."""
    con, _result, _corpus = loaded
    con.execute("UPDATE condition_occurrence SET condition_concept_id = 123456789")
    failing = [r for r in _named(_run(con, concept_map), "concept_id_declared") if not r.passed]
    assert any(r.target == "condition_occurrence.condition_concept_id" for r in failing)
    assert 123456789 in failing[0].examples


def test_concept_id_zero_is_always_acceptable(loaded, concept_map):
    con, _result, _corpus = loaded
    con.execute("UPDATE condition_occurrence SET condition_concept_id = 0")
    assert gate(_run(con, concept_map))[0], "0 means 'looked and found nothing', which is legal"


def test_temporal_order_check_catches_an_inverted_period(loaded, concept_map):
    con, _result, _corpus = loaded
    con.execute("UPDATE visit_occurrence SET visit_end_date = visit_start_date - INTERVAL 3 DAY")
    failing = [r for r in _named(_run(con, concept_map), "temporal_order") if not r.passed]
    assert any("visit_occurrence" in r.target for r in failing)


def test_event_before_birth_is_caught(loaded, concept_map):
    con, _result, _corpus = loaded
    con.execute("UPDATE person SET birth_datetime = TIMESTAMP '2025-01-01 00:00:00'")
    failing = [r for r in _named(_run(con, concept_map), "event_after_birth") if not r.passed]
    assert failing, "an event years before date of birth must not pass"


def test_event_outside_the_observation_period_is_caught(loaded, concept_map):
    con, _result, _corpus = loaded
    con.execute(
        "UPDATE observation_period SET observation_period_end_date = DATE '2024-02-02'"
    )
    failing = [
        r
        for r in _named(_run(con, concept_map), "event_within_observation_period")
        if not r.passed
    ]
    assert failing


def test_an_event_cannot_belong_to_another_persons_visit(loaded, concept_map):
    con, _result, _corpus = loaded
    con.execute(
        "INSERT INTO person (person_id, gender_concept_id, year_of_birth, race_concept_id, "
        "ethnicity_concept_id) VALUES (99, 0, 1970, 0, 0)"
    )
    con.execute("UPDATE condition_occurrence SET person_id = 99")
    failing = [
        r for r in _named(_run(con, concept_map), "visit_person_consistency") if not r.passed
    ]
    assert failing


def test_post_mortem_events_are_a_warning_not_an_error(loaded, concept_map):
    """Real feeds carry late-arriving results, so this must not fail a build on its own."""
    con, _result, _corpus = loaded
    con.execute("UPDATE death SET death_date = DATE '2024-02-01'")
    results = _run(con, concept_map)
    ok, errors, warnings = gate(results)
    assert ok and errors == 0
    assert warnings == 1


def test_vocabulary_foreign_keys_are_reported_as_not_applicable(loaded, concept_map):
    con, _result, _corpus = loaded
    info = _named(_run(con, concept_map), "foreign_key_not_applicable")
    assert info and "concept" in info[0].target
    assert info[0].severity == "info"
