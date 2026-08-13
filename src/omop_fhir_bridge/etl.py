"""FHIR R4 -> OMOP CDM v5.4.

Design decisions worth knowing before reading the code, because each one is a place where an OMOP
ETL can be wrong while looking right:

**Domain routing.** A FHIR ``Observation`` is not an OMOP observation. Whether a row belongs in
MEASUREMENT or OBSERVATION is a property of the *concept*, so routing follows the resolved concept's
``domain_id`` first, falls back to the FHIR category (``laboratory`` and ``vital-signs`` are
measurements, ``survey`` and ``social-history`` are observations) when the concept is unmapped, and
falls back to "has a numeric value" last. The precedence is reported per run so the fallback share
is visible instead of assumed.

**Components are rows.** A Synthea blood pressure carries no top-level value: systolic and diastolic
live in ``Observation.component``. A mapper that reads only ``valueQuantity`` loses every blood
pressure in the corpus and reports a clean run, so components are emitted as one row each.

**Required end dates.** ``DRUG_EXPOSURE.drug_exposure_end_date`` is NOT NULL, and a Synthea
``MedicationRequest`` has no end. The convention here is end = start, which is a *stated* convention
rather than a silent one: it means drug era logic downstream would see zero-day exposures, and that
is written down in ``docs/limits.md`` instead of being discovered later.

**Nothing is invented.** Unmapped codes become ``concept_id = 0`` with the source code retained.
Unknown genders, races, visit classes do the same. The run report carries the resulting mapped share.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from . import constants as K
from .fhir_source import FhirCorpus
from .ids import IdMinter
from .vocab import ConceptMap

CDM_TABLES = (
    "location",
    "care_site",
    "provider",
    "person",
    "observation_period",
    "visit_occurrence",
    "condition_occurrence",
    "procedure_occurrence",
    "drug_exposure",
    "measurement",
    "observation",
    "death",
    "cdm_source",
)

# Resource types this bridge deliberately does not map. Counted and reported, never silently
# dropped -- see docs/limits.md for why each one is out of scope.
UNMAPPED_RESOURCE_TYPES = (
    "Claim",
    "ExplanationOfBenefit",
    "DocumentReference",
    "DiagnosticReport",
    "Provenance",
    "CareTeam",
    "CarePlan",
    "Device",
    "SupplyDelivery",
    "ImagingStudy",
    "Media",
    "AllergyIntolerance",
    "PractitionerRole",
    "Location",
    "Goal",
)

MEASUREMENT_CATEGORIES = {"laboratory", "vital-signs", "exam", "procedure"}
OBSERVATION_CATEGORIES = {"survey", "social-history", "smartdata", "therapy"}

US_STATE_ABBREVIATIONS = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "puerto rico": "PR", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY",
}


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def parse_date(value: Any) -> date | None:
    parsed = parse_datetime(value)
    return parsed.date() if parsed else None


def _period(resource: dict, key: str = "period") -> tuple[datetime | None, datetime | None]:
    period = resource.get(key) or {}
    return parse_datetime(period.get("start")), parse_datetime(period.get("end"))


def _event_datetime(resource: dict) -> datetime | None:
    for key in ("effectiveDateTime", "occurrenceDateTime", "authoredOn", "recordedDate", "issued"):
        found = parse_datetime(resource.get(key))
        if found:
            return found
    start, _ = _period(resource, "effectivePeriod")
    if start:
        return start
    start, _ = _period(resource, "performedPeriod")
    if start:
        return start
    return parse_datetime(resource.get("performedDateTime"))


def _categories(resource: dict) -> set[str]:
    out: set[str] = set()
    categories = resource.get("category")
    if isinstance(categories, dict):
        categories = [categories]
    for cat in categories or []:
        for coding in cat.get("coding") or []:
            if coding.get("code"):
                out.add(coding["code"])
        if cat.get("text"):
            out.add(cat["text"])
    return out


@dataclass
class LoadResult:
    row_counts: dict[str, int] = field(default_factory=dict)
    source_resource_counts: dict[str, int] = field(default_factory=dict)
    rows_by_source_type: dict[str, int] = field(default_factory=dict)
    skipped_resource_counts: dict[str, int] = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)
    unresolved_references: dict[str, int] = field(default_factory=dict)
    observation_routing: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    concept_map_size: int = 0
    concept_map_provenance: dict = field(default_factory=dict)
    surrogate_keys_minted: int = 0

    def as_dict(self) -> dict:
        return {
            "row_counts": self.row_counts,
            "total_omop_rows": sum(self.row_counts.values()),
            "source_resource_counts": self.source_resource_counts,
            "total_source_resources": sum(self.source_resource_counts.values()),
            "rows_by_source_resource_type": self.rows_by_source_type,
            "skipped_resource_counts": self.skipped_resource_counts,
            "terminology_coverage": self.coverage,
            "unresolved_references": self.unresolved_references,
            "observation_routing": self.observation_routing,
            "concept_map_size": self.concept_map_size,
            "concept_map_provenance": self.concept_map_provenance,
            "surrogate_keys_minted": self.surrogate_keys_minted,
            "warnings": self.warnings,
        }


class Loader:
    """Maps a :class:`FhirCorpus` into OMOP tables on an open DuckDB connection."""

    def __init__(self, con, concept_map: ConceptMap, *, source_name: str = "Synthea FHIR R4"):
        self.con = con
        self.vocab = concept_map
        self.source_name = source_name
        self.ids = IdMinter()
        self._rows: dict[str, list[dict]] = defaultdict(list)
        self._rows_by_source_type: Counter = Counter()
        self._routing: Counter = Counter()
        self._person_events: dict[int, list[date]] = defaultdict(list)
        self._location_keys: dict[tuple, int] = {}
        self._warnings: list[str] = []

    # ------------------------------------------------------------------ util
    def _add(self, table: str, row: dict, source_type: str | None = None) -> None:
        self._rows[table].append(row)
        if source_type:
            self._rows_by_source_type[source_type] += 1

    def _note_event(self, person_id: int | None, when: date | None) -> None:
        if person_id and when:
            self._person_events[person_id].append(when)

    def _state(self, raw: str | None) -> str | None:
        if not raw:
            return None
        if len(raw) == 2:
            return raw.upper()
        abbrev = US_STATE_ABBREVIATIONS.get(raw.strip().lower())
        if abbrev is None:
            self._warnings.append(
                f"state {raw!r} has no USPS abbreviation; LOCATION.state left null "
                "(CDM declares varchar(2); truncating would fabricate a state)"
            )
        return abbrev

    def _location_for_address(self, address: dict | None, source_value: str | None) -> int | None:
        if not address:
            return None
        line = (address.get("line") or [None])[0]
        key = (
            line,
            address.get("city"),
            address.get("state"),
            address.get("postalCode"),
            address.get("country"),
        )
        existing = self._location_keys.get(key)
        if existing is not None:
            return existing
        location_id = self.ids.mint("location", f"address|{key}")
        self._location_keys[key] = location_id
        self._add(
            "location",
            {
                "location_id": location_id,
                "address_1": line,
                "city": address.get("city"),
                "state": self._state(address.get("state")),
                "zip": (address.get("postalCode") or None),
                "location_source_value": (source_value or "")[:50] or None,
                "country_source_value": address.get("country"),
                "latitude": self._geo(address, "latitude"),
                "longitude": self._geo(address, "longitude"),
            },
        )
        return location_id

    @staticmethod
    def _geo(address: dict, axis: str) -> float | None:
        for ext in address.get("extension") or []:
            if ext.get("url", "").endswith("geolocation"):
                for sub in ext.get("extension") or []:
                    if sub.get("url") == axis:
                        return sub.get("valueDecimal")
        return None

    # ------------------------------------------------------------------ load
    def load(self, corpus: FhirCorpus) -> LoadResult:
        self.corpus = corpus
        self._load_care_sites(corpus)
        self._load_providers(corpus)
        person_ids = self._load_people(corpus)
        self._load_visits(corpus)
        self._load_conditions(corpus)
        self._load_procedures(corpus)
        self._load_drugs(corpus)
        self._load_immunizations(corpus)
        self._load_observations(corpus)
        self._load_deaths(corpus)
        self._load_observation_periods(person_ids)
        self._load_cdm_source()
        return self._flush(corpus)

    # --- reference data ---------------------------------------------------
    def _load_care_sites(self, corpus: FhirCorpus) -> None:
        for org in corpus.resources("Organization"):
            care_site_id = self.ids.mint("care_site", f"Organization/{org.get('id')}")
            address = (org.get("address") or [None])[0]
            self._add(
                "care_site",
                {
                    "care_site_id": care_site_id,
                    "care_site_name": (org.get("name") or "")[:255] or None,
                    "location_id": self._location_for_address(address, org.get("name")),
                    "care_site_source_value": (org.get("id") or "")[:50] or None,
                },
                source_type="Organization",
            )

    def _load_providers(self, corpus: FhirCorpus) -> None:
        for prac in corpus.resources("Practitioner"):
            provider_id = self.ids.mint("provider", f"Practitioner/{prac.get('id')}")
            name = (prac.get("name") or [{}])[0]
            full = " ".join(
                [*(name.get("prefix") or []), *(name.get("given") or []), name.get("family", "")]
            ).strip()
            npi = next(
                (
                    i.get("value")
                    for i in prac.get("identifier") or []
                    if (i.get("system") or "").endswith("us-npi")
                ),
                None,
            )
            self._add(
                "provider",
                {
                    "provider_id": provider_id,
                    "provider_name": full[:255] or None,
                    "npi": (npi or "")[:20] or None,
                    "provider_source_value": (prac.get("id") or "")[:50] or None,
                    "gender_concept_id": K.GENDER_BY_FHIR_CODE.get(
                        prac.get("gender", ""), K.NO_MATCHING_CONCEPT
                    ),
                    "gender_source_value": prac.get("gender"),
                },
                source_type="Practitioner",
            )

    # --- person ----------------------------------------------------------
    def _load_people(self, corpus: FhirCorpus) -> list[int]:
        person_ids = []
        for patient in corpus.resources("Patient"):
            fhir_id = patient.get("id") or ""
            person_id = self.ids.mint("person", f"Patient/{fhir_id}")
            person_ids.append(person_id)
            birth = parse_datetime(patient.get("birthDate"))
            if birth is None:
                self._warnings.append(f"Patient/{fhir_id} has no birthDate; PERSON requires one")
                continue
            race_code = self._omb_code(patient, K.EXT_US_CORE_RACE)
            eth_code = self._omb_code(patient, K.EXT_US_CORE_ETHNICITY)
            address = (patient.get("address") or [None])[0]
            self._add(
                "person",
                {
                    "person_id": person_id,
                    "gender_concept_id": K.GENDER_BY_FHIR_CODE.get(
                        patient.get("gender", ""), K.NO_MATCHING_CONCEPT
                    ),
                    "year_of_birth": birth.year,
                    "month_of_birth": birth.month,
                    "day_of_birth": birth.day,
                    "birth_datetime": birth,
                    "race_concept_id": K.RACE_BY_OMB_CODE.get(race_code, K.NO_MATCHING_CONCEPT),
                    "ethnicity_concept_id": K.ETHNICITY_BY_OMB_CODE.get(
                        eth_code, K.NO_MATCHING_CONCEPT
                    ),
                    "location_id": self._location_for_address(address, fhir_id),
                    "person_source_value": fhir_id[:50] or None,
                    "gender_source_value": patient.get("gender"),
                    "race_source_value": race_code,
                    "ethnicity_source_value": eth_code,
                },
                source_type="Patient",
            )
        return person_ids

    @staticmethod
    def _omb_code(patient: dict, url: str) -> str | None:
        for ext in patient.get("extension") or []:
            if ext.get("url") != url:
                continue
            for sub in ext.get("extension") or []:
                if sub.get("url") == "ombCategory":
                    return (sub.get("valueCoding") or {}).get("code")
        return None

    def _person_id_for(self, resource: dict, key: str = "subject") -> int | None:
        fhir_id = self.corpus.resolve_id(resource.get(key) or resource.get("patient"))
        return self.ids.get("person", f"Patient/{fhir_id}") if fhir_id else None

    def _visit_id_for(self, resource: dict) -> int | None:
        for key in ("encounter", "context"):
            fhir_id = self.corpus.resolve_id(resource.get(key))
            if fhir_id:
                return self.ids.get("visit_occurrence", f"Encounter/{fhir_id}")
        return None

    def _provider_id_for(self, resource: dict) -> int | None:
        for participant in resource.get("participant") or []:
            fhir_id = self.corpus.resolve_id(participant.get("individual"))
            if fhir_id:
                return self.ids.get("provider", f"Practitioner/{fhir_id}")
        for key in ("performer", "requester", "recorder", "asserter"):
            value = resource.get(key)
            candidates = value if isinstance(value, list) else [value]
            for candidate in candidates:
                if not candidate:
                    continue
                ref = candidate.get("actor") if "actor" in candidate else candidate
                fhir_id = self.corpus.resolve_id(ref)
                if fhir_id:
                    found = self.ids.get("provider", f"Practitioner/{fhir_id}")
                    if found:
                        return found
        return None

    # --- visits ----------------------------------------------------------
    def _load_visits(self, corpus: FhirCorpus) -> None:
        by_person: dict[int, list[tuple[datetime, int]]] = defaultdict(list)
        for enc in corpus.resources("Encounter"):
            person_id = self._person_id_for(enc)
            start, end = _period(enc)
            if person_id is None or start is None:
                self._warnings.append(
                    f"Encounter/{enc.get('id')} skipped: "
                    f"{'no resolvable subject' if person_id is None else 'no period.start'}"
                )
                continue
            visit_id = self.ids.mint("visit_occurrence", f"Encounter/{enc.get('id')}")
            act_code = (enc.get("class") or {}).get("code")
            source_type_code = None
            for type_cc in enc.get("type") or []:
                for coding in type_cc.get("coding") or []:
                    source_type_code = coding.get("code")
                    break
            care_site_fhir = corpus.resolve_id(enc.get("serviceProvider"))
            self._add(
                "visit_occurrence",
                {
                    "visit_occurrence_id": visit_id,
                    "person_id": person_id,
                    "visit_concept_id": K.VISIT_BY_ACT_CODE.get(
                        act_code or "", K.NO_MATCHING_CONCEPT
                    ),
                    "visit_start_date": start.date(),
                    "visit_start_datetime": start,
                    # CDM requires an end date. An encounter still open at extract time gets its
                    # start date, and the convention is recorded rather than smoothed over.
                    "visit_end_date": (end or start).date(),
                    "visit_end_datetime": end or start,
                    "visit_type_concept_id": K.TYPE_EHR_ENCOUNTER,
                    "provider_id": self._provider_id_for(enc),
                    "care_site_id": (
                        self.ids.get("care_site", f"Organization/{care_site_fhir}")
                        if care_site_fhir
                        else None
                    ),
                    "visit_source_value": (source_type_code or act_code or "")[:50] or None,
                },
                source_type="Encounter",
            )
            self._note_event(person_id, start.date())
            self._note_event(person_id, (end or start).date())
            by_person[person_id].append((start, visit_id))
        self._link_preceding_visits(by_person)

    def _link_preceding_visits(self, by_person: dict[int, list[tuple[datetime, int]]]) -> None:
        """PRECEDING_VISIT_OCCURRENCE_ID is derived, so it is computed rather than left null."""
        preceding: dict[int, int] = {}
        for visits in by_person.values():
            ordered = sorted(visits)
            for (_prev_start, prev_id), (_start, visit_id) in zip(
                ordered, ordered[1:], strict=False
            ):
                preceding[visit_id] = prev_id
        for row in self._rows["visit_occurrence"]:
            row["preceding_visit_occurrence_id"] = preceding.get(row["visit_occurrence_id"])

    # --- clinical events -------------------------------------------------
    def _load_conditions(self, corpus: FhirCorpus) -> None:
        for cond in corpus.resources("Condition"):
            person_id = self._person_id_for(cond)
            onset = parse_datetime(cond.get("onsetDateTime")) or parse_datetime(
                cond.get("recordedDate")
            )
            if person_id is None or onset is None:
                continue
            mapping = self.vocab.lookup_coding(cond.get("code"), domain_hint="Condition")
            abatement = parse_datetime(cond.get("abatementDateTime"))
            self._add(
                "condition_occurrence",
                {
                    "condition_occurrence_id": self.ids.mint(
                        "condition_occurrence", f"Condition/{cond.get('id')}"
                    ),
                    "person_id": person_id,
                    "condition_concept_id": mapping.concept_id,
                    "condition_start_date": onset.date(),
                    "condition_start_datetime": onset,
                    "condition_end_date": abatement.date() if abatement else None,
                    "condition_end_datetime": abatement,
                    "condition_type_concept_id": K.TYPE_EHR,
                    "provider_id": self._provider_id_for(cond),
                    "visit_occurrence_id": self._visit_id_for(cond),
                    "condition_source_value": mapping.source_code[:50] or None,
                    "condition_source_concept_id": mapping.source_concept_id,
                    "condition_status_source_value": self._clinical_status(cond),
                },
                source_type="Condition",
            )
            self._note_event(person_id, onset.date())
            if abatement:
                self._note_event(person_id, abatement.date())

    @staticmethod
    def _clinical_status(resource: dict) -> str | None:
        for coding in (resource.get("clinicalStatus") or {}).get("coding") or []:
            if coding.get("code"):
                return coding["code"][:50]
        return None

    def _load_procedures(self, corpus: FhirCorpus) -> None:
        for proc in corpus.resources("Procedure"):
            person_id = self._person_id_for(proc)
            start, end = _period(proc, "performedPeriod")
            start = start or parse_datetime(proc.get("performedDateTime"))
            if person_id is None or start is None:
                continue
            mapping = self.vocab.lookup_coding(proc.get("code"), domain_hint="Procedure")
            self._add(
                "procedure_occurrence",
                {
                    "procedure_occurrence_id": self.ids.mint(
                        "procedure_occurrence", f"Procedure/{proc.get('id')}"
                    ),
                    "person_id": person_id,
                    "procedure_concept_id": mapping.concept_id,
                    "procedure_date": start.date(),
                    "procedure_datetime": start,
                    "procedure_end_date": end.date() if end else None,
                    "procedure_end_datetime": end,
                    "procedure_type_concept_id": K.TYPE_EHR,
                    "provider_id": self._provider_id_for(proc),
                    "visit_occurrence_id": self._visit_id_for(proc),
                    "procedure_source_value": mapping.source_code[:50] or None,
                    "procedure_source_concept_id": mapping.source_concept_id,
                },
                source_type="Procedure",
            )
            self._note_event(person_id, start.date())

    def _load_drugs(self, corpus: FhirCorpus) -> None:
        for req in corpus.resources("MedicationRequest"):
            person_id = self._person_id_for(req)
            authored = parse_datetime(req.get("authoredOn"))
            if person_id is None or authored is None:
                continue
            mapping = self.vocab.lookup_coding(self._medication_code(req), domain_hint="Drug")
            dispense = req.get("dispenseRequest") or {}
            quantity = (dispense.get("quantity") or {}).get("value")
            refills = dispense.get("numberOfRepeatsAllowed")
            duration = (dispense.get("expectedSupplyDuration") or {}).get("value")
            sig = next(
                (d.get("text") for d in req.get("dosageInstruction") or [] if d.get("text")), None
            )
            self._add(
                "drug_exposure",
                {
                    "drug_exposure_id": self.ids.mint(
                        "drug_exposure", f"MedicationRequest/{req.get('id')}"
                    ),
                    "person_id": person_id,
                    "drug_concept_id": mapping.concept_id,
                    "drug_exposure_start_date": authored.date(),
                    "drug_exposure_start_datetime": authored,
                    # See module docstring: end = start is a stated convention, not a measurement.
                    "drug_exposure_end_date": authored.date(),
                    "drug_exposure_end_datetime": authored,
                    "drug_type_concept_id": K.TYPE_EHR_PRESCRIPTION,
                    "refills": int(refills) if isinstance(refills, (int, float)) else None,
                    "quantity": quantity,
                    "days_supply": int(duration) if isinstance(duration, (int, float)) else None,
                    "sig": sig,
                    "provider_id": self._provider_id_for(req),
                    "visit_occurrence_id": self._visit_id_for(req),
                    "drug_source_value": mapping.source_code[:50] or None,
                    "drug_source_concept_id": mapping.source_concept_id,
                },
                source_type="MedicationRequest",
            )
            self._note_event(person_id, authored.date())

    def _medication_code(self, request: dict) -> dict | None:
        """``medication[x]`` is a choice type and Synthea uses both arms.

        Most prescriptions carry ``medicationCodeableConcept``, but some point at a separate
        ``Medication`` resource through ``medicationReference``. A loader that reads only the first
        arm gives those rows ``drug_concept_id = 0`` and no source code, and nothing complains --
        the FHIR R4B model validation on the export side is what surfaced it, because a
        MedicationRequest with neither arm populated is not a legal resource.
        """
        direct = request.get("medicationCodeableConcept")
        if direct:
            return direct
        medication = self.corpus.resolve(request.get("medicationReference"))
        if medication is None:
            return None
        return medication.get("code")

    def _load_immunizations(self, corpus: FhirCorpus) -> None:
        for imm in corpus.resources("Immunization"):
            person_id = self._person_id_for(imm)
            when = parse_datetime(imm.get("occurrenceDateTime"))
            if person_id is None or when is None:
                continue
            mapping = self.vocab.lookup_coding(imm.get("vaccineCode"), domain_hint="Drug")
            self._add(
                "drug_exposure",
                {
                    "drug_exposure_id": self.ids.mint(
                        "drug_exposure", f"Immunization/{imm.get('id')}"
                    ),
                    "person_id": person_id,
                    "drug_concept_id": mapping.concept_id,
                    "drug_exposure_start_date": when.date(),
                    "drug_exposure_start_datetime": when,
                    "drug_exposure_end_date": when.date(),
                    "drug_exposure_end_datetime": when,
                    "drug_type_concept_id": K.TYPE_EHR,
                    "provider_id": self._provider_id_for(imm),
                    "visit_occurrence_id": self._visit_id_for(imm),
                    "drug_source_value": mapping.source_code[:50] or None,
                    "drug_source_concept_id": mapping.source_concept_id,
                },
                source_type="Immunization",
            )
            self._note_event(person_id, when.date())

    # --- observations and measurements -----------------------------------
    def _load_observations(self, corpus: FhirCorpus) -> None:
        for obs in corpus.resources("Observation"):
            person_id = self._person_id_for(obs)
            when = _event_datetime(obs)
            if person_id is None or when is None:
                continue
            components = obs.get("component") or []
            if components and not self._has_value(obs):
                for index, component in enumerate(components):
                    self._emit_observation(
                        obs, person_id, when, component, suffix=f"#component{index}"
                    )
            else:
                self._emit_observation(obs, person_id, when, obs)

    @staticmethod
    def _has_value(node: dict) -> bool:
        return any(
            key in node
            for key in ("valueQuantity", "valueCodeableConcept", "valueString", "valueBoolean",
                        "valueInteger")
        )

    def _emit_observation(
        self, obs: dict, person_id: int, when: datetime, node: dict, suffix: str = ""
    ) -> None:
        mapping = self.vocab.lookup_coding(node.get("code"), domain_hint="Measurement/Observation")
        target, basis = self._route(mapping, obs, node)
        self._routing[basis] += 1
        source_key = f"Observation/{obs.get('id')}{suffix}"
        quantity = node.get("valueQuantity") or {}
        unit = self.vocab.lookup(
            quantity.get("system") or K.SYSTEM_UCUM, quantity.get("code"), domain_hint="Unit"
        ) if quantity else None
        value_concept = (
            self.vocab.lookup_coding(node.get("valueCodeableConcept"), domain_hint="Meas Value")
            if node.get("valueCodeableConcept")
            else None
        )
        value_string = node.get("valueString")
        if value_string is None and node.get("valueBoolean") is not None:
            value_string = str(node["valueBoolean"]).lower()
        reference_range = (node.get("referenceRange") or [{}])[0]
        common = {
            "person_id": person_id,
            "provider_id": self._provider_id_for(obs),
            "visit_occurrence_id": self._visit_id_for(obs),
            "value_as_number": quantity.get("value") if quantity else None,
            "value_as_concept_id": value_concept.concept_id if value_concept else None,
            "unit_concept_id": unit.concept_id if unit else None,
            "unit_source_value": (quantity.get("code") or quantity.get("unit") or "")[:50] or None
            if quantity
            else None,
        }
        if target == "measurement":
            self._add(
                "measurement",
                {
                    "measurement_id": self.ids.mint("measurement", source_key),
                    "measurement_concept_id": mapping.concept_id,
                    "measurement_date": when.date(),
                    "measurement_datetime": when,
                    "measurement_time": when.strftime("%H:%M:%S"),
                    "measurement_type_concept_id": K.TYPE_EHR,
                    "range_low": (reference_range.get("low") or {}).get("value"),
                    "range_high": (reference_range.get("high") or {}).get("value"),
                    "measurement_source_value": mapping.source_code[:50] or None,
                    "measurement_source_concept_id": mapping.source_concept_id,
                    "value_source_value": self._value_source_value(node),
                    **common,
                },
                source_type="Observation",
            )
        else:
            self._add(
                "observation",
                {
                    "observation_id": self.ids.mint("observation", source_key),
                    "observation_concept_id": mapping.concept_id,
                    "observation_date": when.date(),
                    "observation_datetime": when,
                    "observation_type_concept_id": K.TYPE_EHR,
                    # OBSERVATION.value_as_string is varchar(60); longer answers are truncated
                    # here so the DDL-derived length check stays meaningful.
                    "value_as_string": (value_string or "")[:60] or None,
                    "observation_source_value": mapping.source_code[:50] or None,
                    "observation_source_concept_id": mapping.source_concept_id,
                    "value_source_value": self._value_source_value(node),
                    **common,
                },
                source_type="Observation",
            )
        self._note_event(person_id, when.date())

    @staticmethod
    def _value_source_value(node: dict) -> str | None:
        quantity = node.get("valueQuantity") or {}
        if quantity.get("value") is not None:
            return f"{quantity['value']} {quantity.get('code') or ''}".strip()[:50]
        if node.get("valueString"):
            return node["valueString"][:50]
        for coding in (node.get("valueCodeableConcept") or {}).get("coding") or []:
            if coding.get("code"):
                return coding["code"][:50]
        if node.get("valueBoolean") is not None:
            return str(node["valueBoolean"]).lower()
        return None

    def _route(self, mapping, obs: dict, node: dict) -> tuple[str, str]:
        """Decide MEASUREMENT vs OBSERVATION, returning the basis so it can be reported."""
        if mapping.mapped and mapping.domain_id:
            if mapping.domain_id == "Measurement":
                return "measurement", "concept_domain"
            if mapping.domain_id in {"Observation", "Meas Value", "Metadata"}:
                return "observation", "concept_domain"
            if mapping.domain_id in {"Condition", "Procedure", "Device", "Drug", "Spec Anatomic Site"}:
                # A concept whose domain is not Measurement/Observation still has to land
                # somewhere; OBSERVATION is the CDM's designated home for it.
                return "observation", "concept_domain_other"
        categories = _categories(obs)
        if categories & MEASUREMENT_CATEGORIES:
            return "measurement", "fhir_category"
        if categories & OBSERVATION_CATEGORIES:
            return "observation", "fhir_category"
        if (node.get("valueQuantity") or {}).get("value") is not None:
            return "measurement", "numeric_value_fallback"
        return "observation", "default_fallback"

    # --- derived and metadata --------------------------------------------
    def _load_deaths(self, corpus: FhirCorpus) -> None:
        for patient in corpus.resources("Patient"):
            deceased = parse_datetime(patient.get("deceasedDateTime"))
            if not deceased:
                continue
            person_id = self.ids.get("person", f"Patient/{patient.get('id')}")
            if person_id is None:
                continue
            self._add(
                "death",
                {
                    "person_id": person_id,
                    "death_date": deceased.date(),
                    "death_datetime": deceased,
                    "death_type_concept_id": K.TYPE_EHR,
                },
                source_type="Patient",
            )
            self._note_event(person_id, deceased.date())

    def _load_observation_periods(self, person_ids: list[int]) -> None:
        """Derived, not observed. OBSERVATION_PERIOD is the span of events this ETL actually saw,
        which is a lower bound on the span the source system could observe the patient."""
        for person_id in person_ids:
            events = self._person_events.get(person_id)
            if not events:
                continue
            self._add(
                "observation_period",
                {
                    "observation_period_id": self.ids.mint(
                        "observation_period", f"person/{person_id}"
                    ),
                    "person_id": person_id,
                    "observation_period_start_date": min(events),
                    "observation_period_end_date": max(events),
                    "period_type_concept_id": K.TYPE_EHR,
                },
            )

    def _load_cdm_source(self) -> None:
        today = datetime.now().date()
        self._add(
            "cdm_source",
            {
                "cdm_source_name": self.source_name,
                "cdm_source_abbreviation": "OFB",
                "cdm_holder": "OMOP-FHIR-Bridge",
                "source_description": (
                    "FHIR R4 resources mapped by OMOP-FHIR-Bridge. Terminology resolved from a "
                    "committed concept map; unmapped codes are concept_id 0 with source values "
                    "retained."
                ),
                "source_documentation_reference": "https://github.com/anayy09/OMOP-FHIR-Bridge",
                "cdm_etl_reference": "https://github.com/anayy09/OMOP-FHIR-Bridge/blob/main/docs/mapping-spec.md",
                "source_release_date": today,
                "cdm_release_date": today,
                "cdm_version": "v5.4",
                "cdm_version_concept_id": K.CDM_VERSION_CONCEPT_ID,
                "vocabulary_version": (
                    self.vocab.provenance.get("vocabulary_source", "see concept_map.csv header")
                )[:20],
            },
        )

    # --- write -----------------------------------------------------------
    def _flush(self, corpus: FhirCorpus) -> LoadResult:
        self.con.execute(
            """
            CREATE TABLE IF NOT EXISTS bridge_source_map (
                omop_table varchar,
                source_reference varchar,
                surrogate_key integer
            )
            """
        )
        row_counts: dict[str, int] = {}
        for table in CDM_TABLES:
            rows = self._rows.get(table) or []
            if not rows:
                row_counts[table] = 0
                continue
            columns = sorted({key for row in rows for key in row})
            placeholders = ",".join(["?"] * len(columns))
            self.con.executemany(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                [[row.get(c) for c in columns] for row in rows],
            )
            row_counts[table] = len(rows)
        self.con.executemany(
            "INSERT INTO bridge_source_map VALUES (?,?,?)", self.ids.rows()
        )

        skipped = {
            rtype: len(corpus.resources(rtype))
            for rtype in UNMAPPED_RESOURCE_TYPES
            if corpus.resources(rtype)
        }
        if self.ids.exceeds_int32():
            self._warnings.append(
                "surrogate keys exceeded int32; CDM v5.4 declares integer primary keys"
            )
        return LoadResult(
            row_counts=row_counts,
            source_resource_counts=corpus.counts(),
            rows_by_source_type=dict(sorted(self._rows_by_source_type.items())),
            skipped_resource_counts=dict(sorted(skipped.items())),
            coverage=self.vocab.coverage.as_dict(),
            unresolved_references=dict(corpus.unresolved_references),
            observation_routing=dict(sorted(self._routing.items())),
            warnings=sorted(set(self._warnings)),
            concept_map_size=len(self.vocab),
            concept_map_provenance=self.vocab.provenance,
            surrogate_keys_minted=len(self.ids.rows()),
        )
