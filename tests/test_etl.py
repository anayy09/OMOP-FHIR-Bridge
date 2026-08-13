"""What the loader does with the awkward cases, which is where an OMOP ETL is right or wrong."""

from __future__ import annotations

from datetime import date

from conftest import ENCOUNTER_ID, PATIENT_ID, rows

from omop_fhir_bridge import constants as K


def test_person_carries_demographics_and_source_values(loaded):
    con, _result, _corpus = loaded
    person = rows(con, "SELECT * FROM person")[0]
    assert person["gender_concept_id"] == K.GENDER_BY_FHIR_CODE["male"]
    assert person["race_concept_id"] == K.RACE_BY_OMB_CODE["2106-3"]
    assert person["ethnicity_concept_id"] == K.ETHNICITY_BY_OMB_CODE["2186-5"]
    assert (person["year_of_birth"], person["month_of_birth"], person["day_of_birth"]) == (
        1980,
        5,
        15,
    )
    assert person["person_source_value"] == PATIENT_ID
    assert person["race_source_value"] == "2106-3"


def test_conditional_practitioner_reference_resolves_to_a_provider(loaded):
    """The failure this guards against is silent: provider_id is nullable, so a mapper that cannot
    resolve `Practitioner?identifier=...` produces a clean-looking database with no providers."""
    con, _result, _corpus = loaded
    visit = rows(con, "SELECT * FROM visit_occurrence")[0]
    assert visit["provider_id"] is not None
    assert visit["care_site_id"] is not None
    provider = rows(con, "SELECT * FROM provider")[0]
    assert provider["npi"] == "9999912345"
    assert provider["provider_name"] == "Dr. Ana Reyes"


def test_visit_maps_class_and_keeps_the_encounter_type_code(loaded):
    con, _result, _corpus = loaded
    visit = rows(con, "SELECT * FROM visit_occurrence")[0]
    assert visit["visit_concept_id"] == K.VISIT_BY_ACT_CODE["IMP"]
    assert visit["visit_type_concept_id"] == K.TYPE_EHR_ENCOUNTER
    assert visit["visit_start_date"] == date(2024, 2, 1)
    assert visit["visit_end_date"] == date(2024, 2, 3)
    assert visit["visit_source_value"] == "10509002"


def test_condition_maps_to_a_standard_concept_and_keeps_its_source_code(loaded):
    con, _result, _corpus = loaded
    condition = rows(con, "SELECT * FROM condition_occurrence")[0]
    assert condition["condition_concept_id"] != 0
    assert condition["condition_source_value"] == "10509002"
    assert condition["condition_source_concept_id"] != 0
    assert condition["condition_end_date"] == date(2024, 2, 20)
    assert condition["condition_status_source_value"] == "resolved"


def test_blood_pressure_components_become_two_measurement_rows(loaded):
    """A component-only Observation has no valueQuantity. Reading only the top level loses it."""
    con, _result, _corpus = loaded
    bp = rows(
        con,
        "SELECT * FROM measurement WHERE measurement_source_value IN ('8480-6','8462-4') "
        "ORDER BY measurement_source_value",
    )
    assert [r["measurement_source_value"] for r in bp] == ["8462-4", "8480-6"]
    assert [float(r["value_as_number"]) for r in bp] == [76.0, 128.0]
    assert all(r["unit_concept_id"] != 0 for r in bp), "mm[Hg] should resolve to a UCUM concept"


def test_domain_routing_sends_labs_to_measurement_and_surveys_to_observation(loaded):
    con, result, _corpus = loaded
    assert any(
        r["measurement_source_value"] == "8867-4" for r in rows(con, "SELECT * FROM measurement")
    )
    survey = rows(con, "SELECT * FROM observation WHERE observation_source_value = '99999-9'")
    assert len(survey) == 1
    assert survey[0]["observation_concept_id"] == K.NO_MATCHING_CONCEPT
    assert survey[0]["value_as_string"] == "never smoked"
    # The basis for each routing decision is reported, not just the outcome.
    assert "concept_domain" in result.observation_routing
    assert "fhir_category" in result.observation_routing


def test_unmapped_code_is_zero_not_a_guess(loaded):
    con, result, _corpus = loaded
    unmapped = rows(con, "SELECT * FROM observation WHERE observation_source_value = '99999-9'")[0]
    assert unmapped["observation_concept_id"] == 0
    assert unmapped["observation_source_concept_id"] == 0
    assert result.coverage["distinct_unmapped_codes"] >= 1
    assert 0.0 < result.coverage["overall_mapped_share"] < 1.0


def test_medication_reference_is_followed(loaded):
    """MedicationRequest.medication[x] is a choice type; Synthea uses both arms."""
    con, _result, _corpus = loaded
    drug = rows(con, "SELECT * FROM drug_exposure WHERE drug_source_value = '1049625'")
    assert len(drug) == 1
    assert drug[0]["drug_concept_id"] != 0
    assert drug[0]["drug_type_concept_id"] == K.TYPE_EHR_PRESCRIPTION
    assert drug[0]["sig"] == "Take one tablet every 6 hours"
    assert int(drug[0]["refills"]) == 1


def test_drug_end_date_equals_start_by_stated_convention(loaded):
    con, _result, _corpus = loaded
    for drug in rows(con, "SELECT * FROM drug_exposure"):
        assert drug["drug_exposure_end_date"] == drug["drug_exposure_start_date"]


def test_immunization_becomes_a_drug_exposure(loaded):
    con, _result, _corpus = loaded
    vaccine = rows(con, "SELECT * FROM drug_exposure WHERE drug_source_value = '08'")
    assert len(vaccine) == 1
    assert vaccine[0]["drug_concept_id"] != 0


def test_death_row_comes_from_deceased_date_time(loaded):
    con, _result, _corpus = loaded
    death = rows(con, "SELECT * FROM death")
    assert len(death) == 1
    assert death[0]["death_date"] == date(2024, 3, 2)


def test_observation_period_spans_the_events_actually_seen(loaded):
    con, _result, _corpus = loaded
    period = rows(con, "SELECT * FROM observation_period")[0]
    assert period["observation_period_start_date"] == date(2024, 2, 1)
    # The death date is an event too, so the period must reach it.
    assert period["observation_period_end_date"] == date(2024, 3, 2)


def test_events_link_to_the_visit(loaded):
    con, _result, _corpus = loaded
    visit_id = rows(con, "SELECT visit_occurrence_id FROM visit_occurrence")[0][
        "visit_occurrence_id"
    ]
    for table in ("condition_occurrence", "drug_exposure"):
        for row in rows(con, f"SELECT * FROM {table}"):
            assert row["visit_occurrence_id"] == visit_id
    assert ENCOUNTER_ID  # the fixture's encounter is the only one


def test_out_of_scope_resources_are_counted_not_dropped(loaded):
    _con, result, _corpus = loaded
    assert result.skipped_resource_counts.get("Claim") == 1
    assert "Claim" not in result.rows_by_source_type


def test_lineage_table_covers_every_minted_key(loaded):
    con, result, _corpus = loaded
    lineage = rows(con, "SELECT * FROM bridge_source_map")
    assert len(lineage) == result.surrogate_keys_minted
    assert any(r["source_reference"].endswith("#component0") for r in lineage)


def test_cdm_source_records_the_version_and_vocabulary(loaded):
    con, _result, _corpus = loaded
    source = rows(con, "SELECT * FROM cdm_source")[0]
    assert source["cdm_version_concept_id"] == K.CDM_VERSION_CONCEPT_ID
    assert source["cdm_version"] == "v5.4"
    assert source["vocabulary_version"]
