"""Fixtures: one small hand-built FHIR corpus that exercises every branch worth testing.

The corpus is deliberately hand-written rather than sampled from Synthea, so each resource is in it
for a reason: a conditional practitioner reference, a component-only blood pressure, an unmapped
code, a deceased patient, and a medication reached through ``medicationReference``. The codes are
real and their concept ids come from the committed concept map, so nothing here asserts a
terminology fact the repository cannot back up.
"""

from __future__ import annotations

import json

import duckdb
import pytest

from omop_fhir_bridge.ddl import create_tables
from omop_fhir_bridge.etl import Loader
from omop_fhir_bridge.fhir_source import FhirCorpus
from omop_fhir_bridge.vocab import ConceptMap

NPI = "9999912345"

PATIENT_ID = "11111111-1111-4111-8111-111111111111"
ENCOUNTER_ID = "22222222-2222-4222-8222-222222222222"
ORG_ID = "33333333-3333-4333-8333-333333333333"
PRACTITIONER_ID = "44444444-4444-4444-8444-444444444444"
MEDICATION_ID = "55555555-5555-4555-8555-555555555555"


def _entry(resource: dict) -> dict:
    return {"fullUrl": f"urn:uuid:{resource['id']}", "resource": resource}


@pytest.fixture(scope="session")
def concept_map() -> ConceptMap:
    return ConceptMap.load()


@pytest.fixture
def mini_bundle() -> dict:
    patient = {
        "resourceType": "Patient",
        "id": PATIENT_ID,
        "identifier": [{"system": "urn:test:mrn", "value": "MRN-1"}],
        "gender": "male",
        "birthDate": "1980-05-15",
        "deceasedDateTime": "2024-03-02T11:00:00+00:00",
        "extension": [
            {
                "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race",
                "extension": [
                    {"url": "ombCategory", "valueCoding": {"code": "2106-3", "display": "White"}}
                ],
            },
            {
                "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-ethnicity",
                "extension": [{"url": "ombCategory", "valueCoding": {"code": "2186-5"}}],
            },
        ],
        "address": [
            {"line": ["1 Test Way"], "city": "Gainesville", "state": "FL", "postalCode": "32601"}
        ],
    }
    organization = {
        "resourceType": "Organization",
        "id": ORG_ID,
        "identifier": [{"system": "urn:test:org", "value": "ORG-1"}],
        "name": "Test Community Hospital",
        "address": [{"city": "Gainesville", "state": "FL", "postalCode": "32601"}],
    }
    practitioner = {
        "resourceType": "Practitioner",
        "id": PRACTITIONER_ID,
        "identifier": [{"system": "http://hl7.org/fhir/sid/us-npi", "value": NPI}],
        "name": [{"family": "Reyes", "given": ["Ana"], "prefix": ["Dr."]}],
        "gender": "female",
    }
    encounter = {
        "resourceType": "Encounter",
        "id": ENCOUNTER_ID,
        "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "IMP"},
        "type": [{"coding": [{"system": "http://snomed.info/sct", "code": "10509002"}]}],
        "subject": {"reference": f"urn:uuid:{PATIENT_ID}"},
        # A conditional reference into another file, which is how Synthea points at practitioners.
        "participant": [
            {
                "individual": {
                    "reference": f"Practitioner?identifier=http://hl7.org/fhir/sid/us-npi|{NPI}"
                }
            }
        ],
        "serviceProvider": {"reference": f"urn:uuid:{ORG_ID}"},
        "period": {"start": "2024-02-01T09:00:00+00:00", "end": "2024-02-03T15:30:00+00:00"},
    }
    condition = {
        "resourceType": "Condition",
        "id": "c0000000-0000-4000-8000-000000000001",
        "clinicalStatus": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                    "code": "resolved",
                }
            ]
        },
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": "10509002"}]},
        "subject": {"reference": f"urn:uuid:{PATIENT_ID}"},
        "encounter": {"reference": f"urn:uuid:{ENCOUNTER_ID}"},
        "onsetDateTime": "2024-02-01T09:15:00+00:00",
        "abatementDateTime": "2024-02-20T09:15:00+00:00",
    }
    heart_rate = {
        "resourceType": "Observation",
        "id": "o0000000-0000-4000-8000-000000000001",
        "status": "final",
        "category": [
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": "vital-signs",
                    }
                ]
            }
        ],
        "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
        "subject": {"reference": f"urn:uuid:{PATIENT_ID}"},
        "encounter": {"reference": f"urn:uuid:{ENCOUNTER_ID}"},
        "effectiveDateTime": "2024-02-01T10:00:00+00:00",
        "valueQuantity": {
            "value": 72,
            "unit": "/min",
            "system": "http://unitsofmeasure.org",
            "code": "/min",
        },
    }
    blood_pressure = {
        "resourceType": "Observation",
        "id": "o0000000-0000-4000-8000-000000000002",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
        "subject": {"reference": f"urn:uuid:{PATIENT_ID}"},
        "encounter": {"reference": f"urn:uuid:{ENCOUNTER_ID}"},
        "effectiveDateTime": "2024-02-01T10:05:00+00:00",
        # No top-level value: systolic and diastolic live in components.
        "component": [
            {
                "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
                "valueQuantity": {
                    "value": 128,
                    "code": "mm[Hg]",
                    "system": "http://unitsofmeasure.org",
                },
            },
            {
                "code": {"coding": [{"system": "http://loinc.org", "code": "8462-4"}]},
                "valueQuantity": {
                    "value": 76,
                    "code": "mm[Hg]",
                    "system": "http://unitsofmeasure.org",
                },
            },
        ],
    }
    survey = {
        "resourceType": "Observation",
        "id": "o0000000-0000-4000-8000-000000000003",
        "status": "final",
        "category": [
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": "survey",
                    }
                ]
            }
        ],
        # A code no committed mapping covers: must land as concept_id 0 with the code retained.
        "code": {"coding": [{"system": "http://loinc.org", "code": "99999-9"}]},
        "subject": {"reference": f"urn:uuid:{PATIENT_ID}"},
        "effectiveDateTime": "2024-02-02T08:00:00+00:00",
        "valueString": "never smoked",
    }
    medication = {
        "resourceType": "Medication",
        "id": MEDICATION_ID,
        "code": {
            "coding": [
                {
                    "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                    "code": "1049625",
                }
            ]
        },
    }
    medication_request = {
        "resourceType": "MedicationRequest",
        "id": "m0000000-0000-4000-8000-000000000001",
        "status": "completed",
        "intent": "order",
        # Reached through medicationReference, not medicationCodeableConcept.
        "medicationReference": {"reference": f"urn:uuid:{MEDICATION_ID}"},
        "subject": {"reference": f"urn:uuid:{PATIENT_ID}"},
        "encounter": {"reference": f"urn:uuid:{ENCOUNTER_ID}"},
        "authoredOn": "2024-02-02T09:00:00+00:00",
        "dosageInstruction": [{"text": "Take one tablet every 6 hours"}],
        "dispenseRequest": {"quantity": {"value": 20}, "numberOfRepeatsAllowed": 1},
    }
    immunization = {
        "resourceType": "Immunization",
        "id": "i0000000-0000-4000-8000-000000000001",
        "status": "completed",
        "vaccineCode": {"coding": [{"system": "http://hl7.org/fhir/sid/cvx", "code": "08"}]},
        "patient": {"reference": f"urn:uuid:{PATIENT_ID}"},
        "encounter": {"reference": f"urn:uuid:{ENCOUNTER_ID}"},
        "occurrenceDateTime": "2024-02-03T11:00:00+00:00",
    }
    claim = {  # deliberately out of scope; must be counted, not silently dropped
        "resourceType": "Claim",
        "id": "cl000000-0000-4000-8000-000000000001",
        "status": "active",
        "use": "claim",
        "patient": {"reference": f"urn:uuid:{PATIENT_ID}"},
    }
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            _entry(r)
            for r in (
                organization,
                practitioner,
                patient,
                encounter,
                condition,
                heart_rate,
                blood_pressure,
                survey,
                medication,
                medication_request,
                immunization,
                claim,
            )
        ],
    }


@pytest.fixture
def corpus(tmp_path, mini_bundle) -> FhirCorpus:
    path = tmp_path / "mini.json"
    path.write_text(json.dumps(mini_bundle), encoding="utf-8")
    return FhirCorpus.load(tmp_path)


@pytest.fixture
def loaded(tmp_path, corpus, concept_map):
    con = duckdb.connect(str(tmp_path / "omop.duckdb"))
    create_tables(con)
    result = Loader(con, concept_map, source_name="test").load(corpus)
    yield con, result, corpus
    con.close()


def rows(con, sql: str) -> list[dict]:
    cursor = con.execute(sql)
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
