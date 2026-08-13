# Mapping specification

FHIR R4 → OMOP CDM v5.4, field by field, with the conventions named where the two models disagree.
Anything marked **convention** is a choice this bridge made because the CDM required a value the
source does not carry; anything marked **derived** is computed rather than read.

Column names below are exactly those in the vendored OHDSI DDL (`vendor/omop-cdm-5.4/duckdb/`).

## Patient → PERSON

| FHIR | OMOP | Notes |
|---|---|---|
| `Patient.id` | `person_source_value` | also the `bridge_source_map` key |
| `Patient.gender` | `gender_concept_id`, `gender_source_value` | `male`→8507, `female`→8532; `other`/`unknown`→0 |
| `Patient.birthDate` | `year_of_birth`, `month_of_birth`, `day_of_birth`, `birth_datetime` | a patient with no `birthDate` is skipped and warned about: `year_of_birth` is NOT NULL |
| `us-core-race` → `ombCategory` | `race_concept_id`, `race_source_value` | five OMB categories; anything else → 0 |
| `us-core-ethnicity` → `ombCategory` | `ethnicity_concept_id`, `ethnicity_source_value` | `2135-2`→38003563, `2186-5`→38003564 |
| `Patient.address[0]` | `location_id` → LOCATION | deduplicated by address tuple |
| `Patient.deceasedDateTime` | DEATH row | `death_type_concept_id` = 32817 (EHR) |

`Patient.name`, `telecom`, `maritalStatus`, `communication` and `multipleBirth` have no CDM home.

## Address → LOCATION

| FHIR | OMOP | Notes |
|---|---|---|
| `address.line[0]` | `address_1` | |
| `address.city` | `city` | |
| `address.state` | `state` | **convention**: normalised through a USPS abbreviation table because the CDM declares `varchar(2)`. An unrecognised state is left NULL rather than truncated — truncating "Massachusetts" to "Ma" invents a value, and the generated length check would catch it anyway |
| `address.postalCode` | `zip` | |
| `address.country` | `country_source_value` | `country_concept_id` is left NULL; resolving it needs the vocabulary |
| `geolocation` extension | `latitude`, `longitude` | |

## Organization → CARE_SITE · Practitioner → PROVIDER

| FHIR | OMOP | Notes |
|---|---|---|
| `Organization.name` | `care_site_name` | |
| `Organization.address` | `location_id` | |
| `Practitioner.name` | `provider_name` | prefix + given + family joined into one string. **Not reversible** — see the round-trip report |
| `Practitioner.identifier` (us-npi) | `npi` | |
| `Practitioner.gender` | `gender_concept_id`, `gender_source_value` | |

`specialty_concept_id` is left NULL: `PractitionerRole.specialty` is out of scope.

## Encounter → VISIT_OCCURRENCE

| FHIR | OMOP | Notes |
|---|---|---|
| `Encounter.class.code` | `visit_concept_id` | `IMP`/`ACUTE`/`NONAC`→9201, `AMB`/`OBSENC`/`SS`/`VR`→9202, `EMER`→9203, `HH`→581476. Anything else → 0, never defaulted to outpatient |
| `Encounter.type[0].coding[0].code` | `visit_source_value` | falls back to the class code |
| `Encounter.period.start` | `visit_start_date`, `visit_start_datetime` | an encounter with no start is skipped and warned about |
| `Encounter.period.end` | `visit_end_date`, `visit_end_datetime` | **convention**: an encounter still open gets its start date, because `visit_end_date` is NOT NULL |
| `Encounter.participant[].individual` | `provider_id` | resolves conditional references (`Practitioner?identifier=system\|value`) |
| `Encounter.serviceProvider` | `care_site_id` | |
| — | `preceding_visit_occurrence_id` | **derived**: previous visit for the same person by start time |
| — | `visit_type_concept_id` | 32827, EHR encounter record |

`Encounter.status`, `participant.type`, `reasonCode`, `hospitalization` and `location` are not mapped.

## Condition → CONDITION_OCCURRENCE

| FHIR | OMOP | Notes |
|---|---|---|
| `Condition.code` | `condition_concept_id`, `condition_source_value`, `condition_source_concept_id` | |
| `Condition.onsetDateTime` | `condition_start_date/_datetime` | falls back to `recordedDate`; no date means the row is skipped |
| `Condition.abatementDateTime` | `condition_end_date/_datetime` | nullable, so left NULL when absent |
| `Condition.clinicalStatus` | `condition_status_source_value` | `condition_status_concept_id` stays NULL: that column means admission/discharge diagnosis, not clinical status, and conflating them is a common mapping error |
| `Condition.encounter` | `visit_occurrence_id` | |

`verificationStatus`, `category` and `severity` are not mapped.

## Procedure → PROCEDURE_OCCURRENCE

`Procedure.code` → `procedure_concept_id` / source columns; `performedPeriod.start` (or
`performedDateTime`) → `procedure_date/_datetime`; `performedPeriod.end` →
`procedure_end_date/_datetime`; `encounter` → `visit_occurrence_id`; `procedure_type_concept_id` =
32817.

## MedicationRequest → DRUG_EXPOSURE

| FHIR | OMOP | Notes |
|---|---|---|
| `medicationCodeableConcept` **or** `medicationReference` → `Medication.code` | `drug_concept_id`, `drug_source_value` | both arms of the choice type are read; Synthea uses both, and handling only the first silently produces unmapped drugs |
| `authoredOn` | `drug_exposure_start_date/_datetime` | |
| — | `drug_exposure_end_date/_datetime` | **convention**: equal to the start. A prescription request has no end, and the column is NOT NULL. Downstream drug-era logic would see zero-day exposures |
| `dispenseRequest.quantity.value` | `quantity` | |
| `dispenseRequest.numberOfRepeatsAllowed` | `refills` | |
| `dispenseRequest.expectedSupplyDuration.value` | `days_supply` | |
| `dosageInstruction[].text` | `sig` | |
| — | `drug_type_concept_id` | 32838, EHR prescription |

`route_concept_id` and `dose_unit_source_value` are not populated; Synthea does not carry structured
route or dose on the request.

## Immunization → DRUG_EXPOSURE

`vaccineCode` (CVX) → `drug_concept_id`; `occurrenceDateTime` → start and end;
`drug_type_concept_id` = 32817. Note the asymmetry: OMOP has no vaccine table, so on export a
drug row is recognised as an immunization only through its concept's vocabulary being CVX. An unmapped
vaccine code cannot be recognised and returns as a `MedicationRequest`.

## Observation → MEASUREMENT or OBSERVATION

Routing precedence, with the basis for each row counted in the load report:

1. **Resolved concept's `domain_id`** — `Measurement` → MEASUREMENT; `Observation`, `Meas Value`,
   `Metadata` → OBSERVATION; any other domain → OBSERVATION, which is the CDM's designated home for
   a concept that does not belong to the table it arrived in.
2. **FHIR `category`** when the concept is unmapped — `laboratory`, `vital-signs`, `exam`,
   `procedure` → MEASUREMENT; `survey`, `social-history`, `therapy` → OBSERVATION.
3. **Numeric value present** → MEASUREMENT, else OBSERVATION.

| FHIR | OMOP | Notes |
|---|---|---|
| `Observation.code` | `*_concept_id`, `*_source_value`, `*_source_concept_id` | |
| `effectiveDateTime` | `*_date`, `*_datetime` | `effectivePeriod.start` and `issued` are fallbacks |
| `valueQuantity.value` | `value_as_number` | |
| `valueQuantity.code` (UCUM) | `unit_concept_id`, `unit_source_value` | |
| `valueCodeableConcept` | `value_as_concept_id` | |
| `valueString` / `valueBoolean` | `value_as_string` (OBSERVATION only) | truncated to the DDL's `varchar(60)` |
| `referenceRange[0].low/high` | `range_low`, `range_high` (MEASUREMENT only) | |
| `component[]` | one row per component | the component's own code and value are used; the row's lineage key is `<id>#componentN` |
| — | `*_type_concept_id` | 32817 |
| — | `measurement_time` | derived from the timestamp |

## Derived tables

**OBSERVATION_PERIOD** — one row per person spanning the earliest to latest event *this ETL saw*
(visits, conditions, procedures, drugs, measurements, observations, death). It is a lower bound on
observability, not an enrolment period, and a person with no events gets no row and a warning.

**CDM_SOURCE** — one row: `cdm_version` `v5.4`, `cdm_version_concept_id` 756265,
`vocabulary_version` taken from the concept map's provenance header.

**bridge_source_map** — not part of the CDM. `(omop_table, source_reference, surrogate_key)` for every
key minted, which is the only thing that makes the export's resource identity and the round-trip join
possible.

## HL7 v2 ADT → FHIR R4

Translated to FHIR first, then loaded by everything above.

| HL7 v2 | FHIR | Notes |
|---|---|---|
| `PID-3` | `Patient.identifier`, `Patient.id` | sanitised to the FHIR id character class; a message with no identifier is refused rather than given a generated one |
| `PID-5` | `Patient.name` | |
| `PID-7` | `Patient.birthDate` | |
| `PID-8` | `Patient.gender` | `M`/`F`/`O`/`U`; anything else warns and becomes `unknown` |
| `PID-10`, `PID-22` | `us-core-race`, `us-core-ethnicity` | CDC/OMB category code taken from the first component |
| `PID-11` | `Patient.address` | |
| `PID-29`, `PID-30` | `Patient.deceasedDateTime` / `deceasedBoolean` | a death indicator with no date becomes the boolean, and warns |
| `PV1-2` | `Encounter.class` | `I`→IMP, `O`/`P`/`R`→AMB, `E`→EMER, `B`→IMP; anything else warns and resolves to concept 0 |
| `PV1-4` | `Encounter.type` | v2-0007 admission type |
| `PV1-19` | `Encounter.identifier`, `Encounter.id` | |
| `PV1-44`, `PV1-45` | `Encounter.period` | no admit time means no encounter, and a warning |
| `MSH-9` trigger | — | A01, A03, A04, A08 supported; others counted and warned |

Repeated messages about the same patient or visit collapse onto one resource with last-write-wins per
element, which is what an A08 update means. An A03 discharge therefore supplies the `period.end` of
the encounter its A01 created.
