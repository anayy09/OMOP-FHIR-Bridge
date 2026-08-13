"""OMOP CDM v5.4 -> FHIR R4.

The reverse direction is where a mapper's honesty gets tested, because the CDM does not hold
everything FHIR asks for. Three things happen here and all three are reported rather than hidden:

**Required elements the CDM cannot supply.** ``Observation.status``, ``Encounter.status``,
``MedicationRequest.status`` and ``.intent`` are mandatory in FHIR and are simply not OMOP columns.
They are emitted with fixed values and listed in ``FABRICATED_ELEMENTS``, which the round-trip report
prints. A reader should know exactly which fields came out of a constant rather than out of the data.

**Code systems recovered from the vocabulary.** OMOP keeps ``condition_source_value`` (the code) but
not the system it came from, so ``Coding.system`` is recovered through the concept map -- the same
thing a real OMOP site does with its CONCEPT table.

**Resource identity is lineage, not CDM.** Nothing in the CDM says which FHIR resource a row came
from, so the exporter reads ``bridge_source_map`` to put the original id back in ``identifier``.
That table is this bridge's own, not part of the standard, and the fact that it has to exist is
itself one of the round-trip losses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import constants as K
from .vocab import ConceptMap

ID_SYSTEM = "https://github.com/anayy09/OMOP-FHIR-Bridge/fhir-id"
OMOP_SYSTEM = "https://github.com/anayy09/OMOP-FHIR-Bridge/omop-key"

GENDER_BY_CONCEPT = {v: k for k, v in K.GENDER_BY_FHIR_CODE.items()}
# The load direction maps IMP, ACUTE and NONAC all onto 9201, so the reverse map has to pick one.
# That collapse is a real information loss and is reported by the round-trip comparison.
ACT_CODE_BY_VISIT_CONCEPT = {9201: "IMP", 9202: "AMB", 9203: "EMER", 581476: "HH"}
RACE_OMB_BY_CONCEPT = {v: k for k, v in K.RACE_BY_OMB_CODE.items()}
ETHNICITY_OMB_BY_CONCEPT = {v: k for k, v in K.ETHNICITY_BY_OMB_CODE.items()}

DEFAULT_ASSUMED_OFFSET = "+00:00"

FABRICATED_ELEMENTS = {
    "Encounter.status": "finished (FHIR requires it; the CDM has no encounter status column)",
    "Observation.status": "final (FHIR requires it; the CDM has no result status column)",
    "MedicationRequest.status": "completed (FHIR requires it; not represented in DRUG_EXPOSURE)",
    "MedicationRequest.intent": "order (FHIR requires it; not represented in DRUG_EXPOSURE)",
    "Immunization.status": "completed (FHIR requires it; not represented in DRUG_EXPOSURE)",
    "dateTime offset": (
        f"{DEFAULT_ASSUMED_OFFSET} assumed — FHIR requires an offset on any dateTime carrying a "
        "time, and the CDM's TIMESTAMP columns store none, so the exporter has to assert one "
        "(override with --assume-offset)"
    ),
}

EXPORTED_TYPES = (
    "Organization",
    "Practitioner",
    "Patient",
    "Encounter",
    "Condition",
    "Procedure",
    "Observation",
    "MedicationRequest",
    "Immunization",
)


def _iso(value, offset: str = DEFAULT_ASSUMED_OFFSET) -> str | None:
    """Render an OMOP date or timestamp as a FHIR ``dateTime``.

    The offset matters and is the reason this helper exists. FHIR's ``dateTime`` regex *requires* a
    timezone whenever a time is present, and the CDM has no column to store one -- so a naive
    ``2020-04-07T03:42:37`` is not a legal FHIR value at all. The R4B model validation caught this
    on the first run. A date with no time needs no offset and gets none.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat() + offset
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


@dataclass
class ExportResult:
    counts: dict[str, int] = field(default_factory=dict)
    structural_validation: dict = field(default_factory=dict)
    server_validation: dict = field(default_factory=dict)
    fabricated_elements: dict = field(default_factory=lambda: dict(FABRICATED_ELEMENTS))

    def as_dict(self) -> dict:
        return {
            "counts": self.counts,
            "total": sum(self.counts.values()),
            "structural_validation": self.structural_validation,
            "server_validation": self.server_validation,
            "fabricated_elements": self.fabricated_elements,
        }


class Exporter:
    def __init__(
        self, con, concept_map: ConceptMap, *, assume_offset: str = DEFAULT_ASSUMED_OFFSET
    ):
        self.con = con
        self.vocab = concept_map
        self.assume_offset = assume_offset
        self._lineage = self._load_lineage()

    def _dt(self, value) -> str | None:
        return _iso(value, self.assume_offset)

    @property
    def fabricated_elements(self) -> dict:
        elements = dict(FABRICATED_ELEMENTS)
        elements["dateTime offset"] = elements["dateTime offset"].replace(
            DEFAULT_ASSUMED_OFFSET, self.assume_offset, 1
        )
        return elements

    def _load_lineage(self) -> dict[tuple[str, int], str]:
        rows = self.con.execute(
            "SELECT omop_table, surrogate_key, source_reference FROM bridge_source_map"
        ).fetchall()
        return {(r[0], int(r[1])): r[2] for r in rows}

    def _fhir_id(self, table: str, key: int) -> str | None:
        """The lineage key for this row: "Patient/<id>" and "Observation/<id>#component0" both
        reduce to the part after the slash, which is what the source system called it."""
        reference = self._lineage.get((table, int(key)))
        if not reference:
            return None
        return reference.split("/", 1)[1] if "/" in reference else reference

    @staticmethod
    def _resource_id(lineage_key: str | None, fallback: str) -> str:
        """FHIR ids are restricted to ``[A-Za-z0-9-.]{1,64}``.

        Component-derived rows carry ``<uuid>#component0`` as their lineage key, and ``#`` is not in
        that character class -- the R4B model validation rejected it. The separator is swapped for
        the resource id while the identifier keeps the exact lineage key, so the join back to the
        original component is unaffected.
        """
        if not lineage_key:
            return fallback[:64]
        return lineage_key.replace("#", "-")[:64]

    def _identifiers(self, table: str, key: int) -> list[dict]:
        out = [{"system": OMOP_SYSTEM, "value": f"{table}/{key}"}]
        fhir_id = self._fhir_id(table, key)
        if fhir_id:
            out.insert(0, {"system": ID_SYSTEM, "value": fhir_id})
        return out

    def _query(self, sql: str) -> list[dict]:
        cursor = self.con.execute(sql)
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def _coding(self, code: str | None, concept_id: int | None = None) -> dict | None:
        if not code:
            return None
        system = self.vocab.system_for_source_code(code)
        coding: dict = {"code": code}
        if system:
            coding["system"] = system
        return {"coding": [coding]}

    def _subject(self, person_id: int) -> dict:
        fhir_id = self._fhir_id("person", person_id)
        return {"reference": f"Patient/{fhir_id or person_id}"}

    def _encounter_ref(self, visit_occurrence_id: int | None) -> dict | None:
        if not visit_occurrence_id:
            return None
        fhir_id = self._fhir_id("visit_occurrence", visit_occurrence_id)
        return {"reference": f"Encounter/{fhir_id or visit_occurrence_id}"}

    # ------------------------------------------------------------- resources
    def patients(self) -> list[dict]:
        rows = self._query(
            """
            SELECT p.*, l.address_1, l.city, l.state, l.zip, l.country_source_value,
                   d.death_datetime, d.death_date
            FROM person p
            LEFT JOIN location l USING (location_id)
            LEFT JOIN death d USING (person_id)
            ORDER BY p.person_id
            """
        )
        out = []
        for r in rows:
            patient: dict = {
                "resourceType": "Patient",
                "id": self._resource_id(self._fhir_id("person", r["person_id"]), str(r["person_id"])),
                "identifier": self._identifiers("person", r["person_id"]),
                "gender": GENDER_BY_CONCEPT.get(r["gender_concept_id"], r["gender_source_value"])
                or "unknown",
            }
            if r["birth_datetime"]:
                patient["birthDate"] = self._dt(r["birth_datetime"])[:10]
            elif r["year_of_birth"]:
                patient["birthDate"] = (
                    f"{r['year_of_birth']:04d}-{r['month_of_birth'] or 1:02d}"
                    f"-{r['day_of_birth'] or 1:02d}"
                )
            if r["death_datetime"] or r["death_date"]:
                patient["deceasedDateTime"] = self._dt(r["death_datetime"] or r["death_date"])
            extensions = []
            race = RACE_OMB_BY_CONCEPT.get(r["race_concept_id"]) or r["race_source_value"]
            if race:
                extensions.append(
                    {
                        "url": K.EXT_US_CORE_RACE,
                        "extension": [
                            {
                                "url": "ombCategory",
                                "valueCoding": {
                                    "system": "urn:oid:2.16.840.1.113883.6.238",
                                    "code": race,
                                },
                            }
                        ],
                    }
                )
            ethnicity = (
                ETHNICITY_OMB_BY_CONCEPT.get(r["ethnicity_concept_id"])
                or r["ethnicity_source_value"]
            )
            if ethnicity:
                extensions.append(
                    {
                        "url": K.EXT_US_CORE_ETHNICITY,
                        "extension": [
                            {
                                "url": "ombCategory",
                                "valueCoding": {
                                    "system": "urn:oid:2.16.840.1.113883.6.238",
                                    "code": ethnicity,
                                },
                            }
                        ],
                    }
                )
            if extensions:
                patient["extension"] = extensions
            if any(r[k] for k in ("address_1", "city", "state", "zip")):
                address = {
                    "line": [r["address_1"]] if r["address_1"] else None,
                    "city": r["city"],
                    "state": r["state"],
                    "postalCode": r["zip"],
                    "country": r["country_source_value"],
                }
                patient["address"] = [{k: v for k, v in address.items() if v}]
            out.append(patient)
        return out

    def encounters(self) -> list[dict]:
        rows = self._query(
            """
            SELECT v.*, c.care_site_source_value, p.provider_source_value
            FROM visit_occurrence v
            LEFT JOIN care_site c USING (care_site_id)
            LEFT JOIN provider p USING (provider_id)
            ORDER BY v.visit_occurrence_id
            """
        )
        out = []
        for r in rows:
            encounter: dict = {
                "resourceType": "Encounter",
                "id": self._resource_id(
                    self._fhir_id("visit_occurrence", r["visit_occurrence_id"]),
                    str(r["visit_occurrence_id"]),
                ),
                "identifier": self._identifiers("visit_occurrence", r["visit_occurrence_id"]),
                "status": "finished",
                "class": {
                    "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                    "code": ACT_CODE_BY_VISIT_CONCEPT.get(r["visit_concept_id"], "AMB"),
                },
                "subject": self._subject(r["person_id"]),
                "period": {
                    "start": self._dt(r["visit_start_datetime"] or r["visit_start_date"]),
                    "end": self._dt(r["visit_end_datetime"] or r["visit_end_date"]),
                },
            }
            type_cc = self._coding(r["visit_source_value"])
            if type_cc:
                encounter["type"] = [type_cc]
            if r["care_site_source_value"]:
                encounter["serviceProvider"] = {
                    "reference": f"Organization/{r['care_site_source_value']}"
                }
            if r["provider_source_value"]:
                encounter["participant"] = [
                    {
                        "individual": {
                            "reference": f"Practitioner/{r['provider_source_value']}"
                        }
                    }
                ]
            out.append(encounter)
        return out

    def conditions(self) -> list[dict]:
        out = []
        for r in self._query("SELECT * FROM condition_occurrence ORDER BY condition_occurrence_id"):
            condition: dict = {
                "resourceType": "Condition",
                "id": self._resource_id(
                    self._fhir_id("condition_occurrence", r["condition_occurrence_id"]),
                    str(r["condition_occurrence_id"]),
                ),
                "identifier": self._identifiers(
                    "condition_occurrence", r["condition_occurrence_id"]
                ),
                "subject": self._subject(r["person_id"]),
                "onsetDateTime": self._dt(r["condition_start_datetime"] or r["condition_start_date"]),
            }
            code = self._coding(r["condition_source_value"], r["condition_concept_id"])
            if code:
                condition["code"] = code
            if r["condition_end_datetime"] or r["condition_end_date"]:
                condition["abatementDateTime"] = self._dt(
                    r["condition_end_datetime"] or r["condition_end_date"]
                )
            if r["condition_status_source_value"]:
                condition["clinicalStatus"] = {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                            "code": r["condition_status_source_value"],
                        }
                    ]
                }
            encounter = self._encounter_ref(r["visit_occurrence_id"])
            if encounter:
                condition["encounter"] = encounter
            out.append(condition)
        return out

    def procedures(self) -> list[dict]:
        out = []
        for r in self._query("SELECT * FROM procedure_occurrence ORDER BY procedure_occurrence_id"):
            procedure: dict = {
                "resourceType": "Procedure",
                "id": self._resource_id(
                    self._fhir_id("procedure_occurrence", r["procedure_occurrence_id"]),
                    str(r["procedure_occurrence_id"]),
                ),
                "identifier": self._identifiers(
                    "procedure_occurrence", r["procedure_occurrence_id"]
                ),
                "status": "completed",
                "subject": self._subject(r["person_id"]),
                "performedPeriod": {
                    "start": self._dt(r["procedure_datetime"] or r["procedure_date"]),
                    "end": self._dt(r["procedure_end_datetime"] or r["procedure_end_date"]),
                },
            }
            procedure["performedPeriod"] = {
                k: v for k, v in procedure["performedPeriod"].items() if v
            }
            code = self._coding(r["procedure_source_value"], r["procedure_concept_id"])
            if code:
                procedure["code"] = code
            encounter = self._encounter_ref(r["visit_occurrence_id"])
            if encounter:
                procedure["encounter"] = encounter
            out.append(procedure)
        return out

    def observations(self) -> list[dict]:
        """MEASUREMENT and OBSERVATION both come back as FHIR Observation, which is the correct
        merge: the split between them is an OMOP modelling decision, not a FHIR one."""
        out = []
        for table, id_col, date_col, dt_col, concept_col, source_col in (
            (
                "measurement",
                "measurement_id",
                "measurement_date",
                "measurement_datetime",
                "measurement_concept_id",
                "measurement_source_value",
            ),
            (
                "observation",
                "observation_id",
                "observation_date",
                "observation_datetime",
                "observation_concept_id",
                "observation_source_value",
            ),
        ):
            for r in self._query(f"SELECT * FROM {table} ORDER BY {id_col}"):
                observation: dict = {
                    "resourceType": "Observation",
                    "id": self._resource_id(self._fhir_id(table, r[id_col]), f"{table}-{r[id_col]}"),
                    "identifier": self._identifiers(table, r[id_col]),
                    "status": "final",
                    "subject": self._subject(r["person_id"]),
                    "effectiveDateTime": self._dt(r[dt_col] or r[date_col]),
                }
                code = self._coding(r[source_col], r[concept_col])
                if code:
                    observation["code"] = code
                if r["value_as_number"] is not None:
                    quantity: dict = {"value": float(r["value_as_number"])}
                    if r["unit_source_value"]:
                        quantity["code"] = r["unit_source_value"]
                        quantity["unit"] = r["unit_source_value"]
                        quantity["system"] = K.SYSTEM_UCUM
                    observation["valueQuantity"] = quantity
                elif table == "observation" and r.get("value_as_string"):
                    observation["valueString"] = r["value_as_string"]
                elif r.get("value_source_value"):
                    observation["valueString"] = r["value_source_value"]
                if table == "measurement" and (
                    r["range_low"] is not None or r["range_high"] is not None
                ):
                    reference_range: dict = {}
                    if r["range_low"] is not None:
                        reference_range["low"] = {"value": float(r["range_low"])}
                    if r["range_high"] is not None:
                        reference_range["high"] = {"value": float(r["range_high"])}
                    observation["referenceRange"] = [reference_range]
                encounter = self._encounter_ref(r["visit_occurrence_id"])
                if encounter:
                    observation["encounter"] = encounter
                out.append(observation)
        return out

    def drugs(self) -> tuple[list[dict], list[dict]]:
        """DRUG_EXPOSURE splits back into MedicationRequest and Immunization.

        The split is decided by the drug concept's *vocabulary* (CVX means a vaccine), which is a
        vocabulary fact rather than a memory of the source resource type. Where the concept is
        unmapped the vocabulary is unknown, so the row can only come back as a MedicationRequest --
        a genuine round-trip loss, counted in the report.
        """
        requests, immunizations = [], []
        for r in self._query("SELECT * FROM drug_exposure ORDER BY drug_exposure_id"):
            fhir_id = self._fhir_id("drug_exposure", r["drug_exposure_id"])
            code = self._coding(r["drug_source_value"], r["drug_concept_id"])
            when = self._dt(r["drug_exposure_start_datetime"] or r["drug_exposure_start_date"])
            vocabulary = self.vocab.vocabulary_for_concept_id(r["drug_concept_id"])
            identifiers = self._identifiers("drug_exposure", r["drug_exposure_id"])
            if vocabulary == "CVX":
                immunization: dict = {
                    "resourceType": "Immunization",
                    "id": self._resource_id(fhir_id, f"immunization-{r['drug_exposure_id']}"),
                    "identifier": identifiers,
                    "status": "completed",
                    "patient": self._subject(r["person_id"]),
                    "occurrenceDateTime": when,
                }
                if code:
                    immunization["vaccineCode"] = code
                encounter = self._encounter_ref(r["visit_occurrence_id"])
                if encounter:
                    immunization["encounter"] = encounter
                immunizations.append(immunization)
                continue
            request: dict = {
                "resourceType": "MedicationRequest",
                "id": self._resource_id(fhir_id, f"medicationrequest-{r['drug_exposure_id']}"),
                "identifier": identifiers,
                "status": "completed",
                "intent": "order",
                "subject": self._subject(r["person_id"]),
                "authoredOn": when,
            }
            if code:
                request["medicationCodeableConcept"] = code
            if r["sig"]:
                request["dosageInstruction"] = [{"text": r["sig"]}]
            dispense = {}
            if r["quantity"] is not None:
                dispense["quantity"] = {"value": float(r["quantity"])}
            if r["refills"] is not None:
                dispense["numberOfRepeatsAllowed"] = int(r["refills"])
            if r["days_supply"] is not None:
                dispense["expectedSupplyDuration"] = {
                    "value": int(r["days_supply"]),
                    "unit": "days",
                }
            if dispense:
                request["dispenseRequest"] = dispense
            encounter = self._encounter_ref(r["visit_occurrence_id"])
            if encounter:
                request["encounter"] = encounter
            requests.append(request)
        return requests, immunizations

    def organizations(self) -> list[dict]:
        rows = self._query(
            """
            SELECT c.*, l.address_1, l.city, l.state, l.zip, l.country_source_value
            FROM care_site c LEFT JOIN location l USING (location_id)
            ORDER BY c.care_site_id
            """
        )
        out = []
        for r in rows:
            organization: dict = {
                "resourceType": "Organization",
                "id": self._resource_id(
                    self._fhir_id("care_site", r["care_site_id"]), str(r["care_site_id"])
                ),
                "identifier": self._identifiers("care_site", r["care_site_id"]),
                "active": True,
            }
            if r["care_site_name"]:
                organization["name"] = r["care_site_name"]
            if any(r[k] for k in ("address_1", "city", "state", "zip")):
                address = {
                    "line": [r["address_1"]] if r["address_1"] else None,
                    "city": r["city"],
                    "state": r["state"],
                    "postalCode": r["zip"],
                    "country": r["country_source_value"],
                }
                organization["address"] = [{k: v for k, v in address.items() if v}]
            out.append(organization)
        return out

    def practitioners(self) -> list[dict]:
        out = []
        for r in self._query("SELECT * FROM provider ORDER BY provider_id"):
            practitioner: dict = {
                "resourceType": "Practitioner",
                "id": self._resource_id(
                    self._fhir_id("provider", r["provider_id"]), str(r["provider_id"])
                ),
                "identifier": self._identifiers("provider", r["provider_id"]),
                "active": True,
            }
            if r["npi"]:
                practitioner["identifier"].append(
                    {"system": "http://hl7.org/fhir/sid/us-npi", "value": r["npi"]}
                )
            if r["provider_name"]:
                # PROVIDER.provider_name is one string; FHIR HumanName is structured. The split is
                # not recoverable, so the whole name goes to `text` rather than being guessed apart.
                practitioner["name"] = [{"text": r["provider_name"]}]
            gender = GENDER_BY_CONCEPT.get(r["gender_concept_id"]) or r["gender_source_value"]
            if gender in {"male", "female", "other", "unknown"}:
                practitioner["gender"] = gender
            out.append(practitioner)
        return out

    # ---------------------------------------------------------------- driver
    def export(self) -> dict[str, list[dict]]:
        requests, immunizations = self.drugs()
        return {
            "Organization": self.organizations(),
            "Practitioner": self.practitioners(),
            "Patient": self.patients(),
            "Encounter": self.encounters(),
            "Condition": self.conditions(),
            "Procedure": self.procedures(),
            "Observation": self.observations(),
            "MedicationRequest": requests,
            "Immunization": immunizations,
        }

    def write_ndjson(self, resources: dict[str, list[dict]], out_dir: Path) -> dict[str, int]:
        out_dir.mkdir(parents=True, exist_ok=True)
        counts = {}
        for rtype, items in resources.items():
            path = out_dir / f"{rtype}.ndjson"
            with path.open("w", encoding="utf-8", newline="\n") as fh:
                for item in items:
                    fh.write(json.dumps(item, separators=(",", ":")) + "\n")
            counts[rtype] = len(items)
        return counts


def validate_structural(resources: dict[str, list[dict]]) -> dict:
    """Validate against the FHIR R4B resource models from ``fhir.resources``.

    R4B rather than R4 is a deliberate, stated approximation: the two are identical for every
    resource this bridge emits, and ``fhir.resources`` ships maintained R4B models. Full R4 profile
    validation is done by the ``$validate`` step against a real HAPI FHIR server -- that is the one
    that counts, and it is a separate CI job for exactly that reason.
    """
    import importlib

    summary: dict = {"validator": "fhir.resources R4B models", "by_type": {}, "errors": []}
    for rtype, items in resources.items():
        module = importlib.import_module(f"fhir.resources.R4B.{rtype.lower()}")
        model = getattr(module, rtype)
        ok, reported = 0, 0
        for item in items:
            try:
                model.model_validate(item)
                ok += 1
            except Exception as exc:  # noqa: BLE001 - the message is the finding
                # Cap per resource type rather than overall: a single noisy type would otherwise
                # crowd out every other type's first failure, which is the one worth seeing.
                if reported < 5:
                    reported += 1
                    summary["errors"].append(
                        {"resourceType": rtype, "id": item.get("id"), "error": str(exc)[:400]}
                    )
        summary["by_type"][rtype] = {"validated": len(items), "passed": ok}
    summary["total"] = sum(v["validated"] for v in summary["by_type"].values())
    summary["passed"] = sum(v["passed"] for v in summary["by_type"].values())
    return summary
