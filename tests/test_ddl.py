"""The schema must come out of the vendored OHDSI DDL, because every check depends on it."""

from __future__ import annotations

import pytest

from omop_fhir_bridge.ddl import schema


def test_core_tables_are_parsed():
    cdm = schema()
    for table in ("person", "visit_occurrence", "condition_occurrence", "measurement", "death"):
        assert table in cdm.tables, f"{table} missing from the parsed schema"
    # v5.4 has 39 tables; a parser that silently matched only some of them would be worse than one
    # that failed outright.
    assert len(cdm.tables) >= 39


def test_nullability_matches_the_specification():
    person = schema()["person"]
    required = {c.name for c in person.not_null}
    assert required == {
        "person_id",
        "gender_concept_id",
        "year_of_birth",
        "race_concept_id",
        "ethnicity_concept_id",
    }
    assert person.column("month_of_birth").nullable


def test_varchar_bounds_are_read_not_guessed():
    assert schema()["location"].column("state").max_length == 2
    assert schema()["observation"].column("value_as_string").max_length == 60
    assert schema()["person"].column("person_source_value").max_length == 50
    assert schema()["drug_exposure"].column("sig").max_length is None  # TEXT, unbounded


def test_primary_keys_come_from_the_primary_key_file():
    assert schema()["person"].primary_key == ("person_id",)
    assert schema()["measurement"].primary_key == ("measurement_id",)


def test_foreign_keys_are_parsed_including_the_vocabulary_ones():
    fks = schema().foreign_keys_for("condition_occurrence")
    targets = {(fk.column, fk.ref_table) for fk in fks}
    assert ("person_id", "person") in targets
    assert ("condition_concept_id", "concept") in targets, (
        "concept foreign keys must still be parsed; checks.py decides what to do about them"
    )


def test_concept_id_columns_are_identifiable():
    person = schema()["person"]
    assert person.column("gender_concept_id").is_concept_id
    assert not person.column("year_of_birth").is_concept_id


def test_unknown_column_raises():
    with pytest.raises(KeyError):
        schema()["person"].column("favourite_colour")
