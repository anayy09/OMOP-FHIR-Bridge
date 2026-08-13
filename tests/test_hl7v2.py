"""HL7 v2 ADT translation, including the messages it refuses to guess about."""

from __future__ import annotations

from pathlib import Path

import pytest

from omop_fhir_bridge.hl7v2 import (
    parse_message,
    split_messages,
    to_resources,
    translate_directory,
)

FEED = Path(__file__).resolve().parents[1] / "data" / "hl7v2"

A01 = "\n".join(
    [
        r"MSH|^~\&|EPIC|HOSP|BRIDGE|RESEARCH|20260301081500||ADT^A01|MSG1|P|2.5.1",
        "EVN|A01|20260301081500",
        "PID|1||MRN9001^^^HOSP^MR||Okonkwo^Ada^B||19750620|F||2054-5^Black or African American^CDCREC"
        "|12 Elm St^^Ocala^FL^34470^USA||||||||||||2186-5^Not Hispanic or Latino^CDCREC",
        "PV1|1|I|4W^412^01^HOSP|E|||1912345678^Ade^Femi^^^Dr|||MED||||7|||||VN9001"
        "|||||||||||||||||||||||||20260301081500",
    ]
)


def test_encoding_characters_come_from_msh_2():
    message = parse_message(A01)
    assert message.encoding.field_sep == "|"
    assert message.encoding.component == "^"
    assert message.message_type == "ADT^A01"
    assert message.trigger_event == "A01"
    assert message.control_id == "MSG1"


def test_a01_produces_a_patient_and_an_encounter():
    resources, warnings = to_resources(parse_message(A01))
    assert not warnings
    patient, encounter = resources
    assert patient["resourceType"] == "Patient"
    assert patient["id"] == "MRN9001"
    assert patient["gender"] == "female"
    assert patient["birthDate"] == "1975-06-20"
    assert patient["name"][0]["family"] == "Okonkwo"
    assert patient["address"][0]["postalCode"] == "34470"
    races = [
        e["extension"][0]["valueCoding"]["code"]
        for e in patient["extension"]
        if e["url"].endswith("us-core-race")
    ]
    assert races == ["2054-5"]
    assert encounter["class"]["code"] == "IMP"
    assert encounter["period"]["start"] == "2026-03-01T08:15:00+00:00"
    assert encounter["identifier"][0]["value"] == "VN9001"


def test_unsupported_event_is_reported_rather_than_ignored():
    a02 = A01.replace("ADT^A01", "ADT^A02").replace("EVN|A01", "EVN|A02")
    resources, warnings = to_resources(parse_message(a02))
    assert resources == []
    assert "A02 not supported" in warnings[0]


def test_missing_admit_time_refuses_to_invent_a_visit():
    without_admit = "\n".join(
        line for line in A01.splitlines() if not line.startswith("PV1")
    ) + "\nPV1|1|I|||||||||||||||||VN9001"
    resources, warnings = to_resources(parse_message(without_admit))
    assert [r["resourceType"] for r in resources] == ["Patient"]
    assert any("VISIT_OCCURRENCE requires a start date" in w for w in warnings)


def test_unmapped_patient_class_warns_instead_of_defaulting_silently():
    odd_class = A01.replace("PV1|1|I|", "PV1|1|N|")
    _resources, warnings = to_resources(parse_message(odd_class))
    assert any("no v3 ActCode" in w for w in warnings)


def test_message_without_pid_maps_nothing():
    no_pid = "\n".join(line for line in A01.splitlines() if not line.startswith("PID"))
    resources, warnings = to_resources(parse_message(no_pid))
    assert resources == []
    assert "no PID" in warnings[0]


def test_message_must_start_with_msh():
    with pytest.raises(ValueError):
        parse_message("PID|1||MRN1")


def test_split_handles_a_concatenated_file():
    assert len(split_messages(A01 + "\n" + A01)) == 2


def test_committed_feed_translates_and_a03_supplies_the_discharge_time():
    """The A01 and the A03 describe one visit. They must collapse onto one Encounter, with the
    discharge time arriving on the later message."""
    result = translate_directory(FEED)
    assert result.messages == 6
    assert result.by_event == {"A01": 2, "A02": 1, "A03": 1, "A04": 1, "A08": 1}
    encounters = [
        e["resource"]
        for e in result.bundle["entry"]
        if e["resource"]["resourceType"] == "Encounter"
    ]
    inpatient = next(e for e in encounters if e["identifier"][0]["value"] == "VN0000441")
    assert inpatient["period"]["start"] == "2026-03-01T08:15:00+00:00"
    assert inpatient["period"]["end"] == "2026-03-04T14:30:00+00:00"
    assert inpatient["status"] == "finished"


def test_a08_update_wins_over_the_earlier_registration():
    result = translate_directory(FEED)
    patients = {
        e["resource"]["id"]: e["resource"]
        for e in result.bundle["entry"]
        if e["resource"]["resourceType"] == "Patient"
    }
    updated = patients["MRN000208"]
    assert updated["birthDate"] == "1991-11-28", "the A08 correction must survive"
    assert updated["name"][0]["given"] == ["Terrence"]


def test_translated_bundle_loads_through_the_same_mapper(tmp_path, concept_map):
    """The point of translating to FHIR is that ADT reaches OMOP through one mapper, not two."""
    import json

    import duckdb

    from omop_fhir_bridge.ddl import create_tables
    from omop_fhir_bridge.etl import Loader
    from omop_fhir_bridge.fhir_source import FhirCorpus

    result = translate_directory(FEED)
    (tmp_path / "adt.json").write_text(json.dumps(result.bundle), encoding="utf-8")
    con = duckdb.connect(str(tmp_path / "adt.duckdb"))
    create_tables(con)
    load = Loader(con, concept_map, source_name="adt").load(FhirCorpus.load(tmp_path))
    assert load.row_counts["person"] == 3
    assert load.row_counts["visit_occurrence"] == 2
    con.close()
