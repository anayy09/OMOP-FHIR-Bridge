"""The reverse direction: valid FHIR out, and a round-trip whose losses are the declared ones."""

from __future__ import annotations

import re

from conftest import PATIENT_ID

from omop_fhir_bridge.export import ID_SYSTEM, Exporter, validate_structural
from omop_fhir_bridge.roundtrip import RoundTrip, resource_coverage

DATETIME_WITH_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _export(loaded, concept_map, **kwargs):
    con, _result, _corpus = loaded
    return Exporter(con, concept_map, **kwargs).export()


def test_every_exported_resource_is_structurally_valid(loaded, concept_map):
    summary = validate_structural(_export(loaded, concept_map))
    assert summary["passed"] == summary["total"], summary["errors"]
    assert summary["total"] > 10


def test_datetimes_carry_an_offset(loaded, concept_map):
    """Regression test. FHIR's dateTime regex requires a timezone whenever a time is present, and
    the CDM stores none -- the first version of the exporter emitted naive timestamps and every
    resource with a time was invalid."""
    resources = _export(loaded, concept_map)
    checked = 0
    for items in resources.values():
        for resource in items:
            for value in resource.values():
                if isinstance(value, str) and DATETIME_WITH_TIME.match(value):
                    assert re.search(r"([+-]\d{2}:\d{2}|Z)$", value), value
                    checked += 1
    assert checked > 5, "the fixture must contain timestamps for this test to mean anything"


def test_assumed_offset_is_configurable_and_reported(loaded, concept_map):
    resources = _export(loaded, concept_map, assume_offset="-05:00")
    patient = resources["Patient"][0]
    assert patient["deceasedDateTime"].endswith("-05:00")
    con, _result, _corpus = loaded
    assert "-05:00" in Exporter(con, concept_map, assume_offset="-05:00").fabricated_elements[
        "dateTime offset"
    ]


def test_resource_ids_are_legal_fhir_ids(loaded, concept_map):
    """Component-derived rows have lineage keys like `<uuid>#component0`, and `#` is not in the FHIR
    id character class."""
    for items in _export(loaded, concept_map).values():
        for resource in items:
            assert re.fullmatch(r"[A-Za-z0-9\-.]{1,64}", resource["id"]), resource["id"]


def test_lineage_identifier_preserves_the_original_id(loaded, concept_map):
    patient = _export(loaded, concept_map)["Patient"][0]
    lineage = [i for i in patient["identifier"] if i["system"] == ID_SYSTEM]
    assert lineage and lineage[0]["value"] == PATIENT_ID


def test_measurement_and_observation_both_return_as_observation(loaded, concept_map):
    resources = _export(loaded, concept_map)
    codes = {
        c["code"]
        for observation in resources["Observation"]
        for c in (observation.get("code") or {}).get("coding", [])
    }
    assert {"8867-4", "8480-6", "8462-4", "99999-9"} <= codes


def test_cvx_drug_returns_as_an_immunization_and_rxnorm_as_a_request(loaded, concept_map):
    """The split is decided by the drug concept's vocabulary, not by remembering the source type."""
    resources = _export(loaded, concept_map)
    assert [
        c["code"]
        for i in resources["Immunization"]
        for c in (i.get("vaccineCode") or {}).get("coding", [])
    ] == ["08"]
    assert [
        c["code"]
        for m in resources["MedicationRequest"]
        for c in (m.get("medicationCodeableConcept") or {}).get("coding", [])
    ] == ["1049625"]


def test_code_systems_are_recovered_from_the_vocabulary(loaded, concept_map):
    condition = _export(loaded, concept_map)["Condition"][0]
    coding = condition["code"]["coding"][0]
    assert coding["system"] == "http://snomed.info/sct"
    assert coding["code"] == "10509002"


def test_round_trip_retains_the_fields_omop_models(loaded, concept_map):
    con, _result, corpus = loaded
    resources = Exporter(con, concept_map).export()
    comparison = RoundTrip(corpus, resources).run()
    assert comparison["joins"].get("original_not_found", 0) == 0
    assert comparison["joins"].get("no_lineage_identifier", 0) == 0
    totals = comparison["totals"]
    assert totals["compared"] > 40
    # Everything compared must be accounted for in exactly one bucket.
    assert totals["retained"] + totals["transformed"] + totals["dropped"] == totals["compared"]
    for field in ("gender", "birthDate", "race.ombCategory", "deceasedDateTime"):
        tally = comparison["fields"]["Patient"][field]
        assert tally["retained"] == tally["compared"], (field, tally)


def test_round_trip_reports_the_practitioner_name_loss(loaded, concept_map):
    """PROVIDER.provider_name is one string, so family and given cannot come back apart. The report
    has to say so with a number rather than in prose."""
    con, _result, corpus = loaded
    comparison = RoundTrip(corpus, Exporter(con, concept_map).export()).run()
    for field in ("name.family", "name.given"):
        tally = comparison["fields"]["Practitioner"][field]
        assert tally["dropped"] == tally["compared"] > 0
        assert "not represented in the CDM" in tally["reasons"]


def test_component_observations_are_compared_against_their_component(loaded, concept_map):
    con, _result, corpus = loaded
    comparison = RoundTrip(corpus, Exporter(con, concept_map).export()).run()
    value = comparison["fields"]["Observation"]["valueQuantity.value"]
    assert value["retained"] == value["compared"], (
        "component values must join to the component, not the parent resource"
    )


def test_structural_losses_are_declared(loaded, concept_map):
    con, _result, corpus = loaded
    comparison = RoundTrip(corpus, Exporter(con, concept_map).export()).run()
    items = {loss["item"] for loss in comparison["structural_losses"]}
    assert {"timezone offset", "Coding.display", "resource identity"} <= items


def test_resource_coverage_separates_mapped_from_unmapped_types(loaded, concept_map):
    con, result, corpus = loaded
    rows = resource_coverage(corpus, result, Exporter(con, concept_map).export())
    by_type = {r["resource_type"]: r for r in rows}
    assert by_type["Claim"]["mapped"] is False
    assert by_type["Claim"]["omop_rows"] == 0
    assert by_type["Observation"]["omop_rows"] == 4, "3 observations, one split into 2 components"
    assert by_type["Patient"]["mapped"] is True
