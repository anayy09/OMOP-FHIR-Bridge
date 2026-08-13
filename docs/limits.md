# Limits

What this bridge does not do, and what would have to change for it to. Written so a reader does not
have to discover any of it by running into it.

## Scale and architecture

- **In-memory and batch only.** `FhirCorpus` indexes the whole input so conditional references can be
  resolved across files. That is what makes `Practitioner?identifier=…` work, and it means the input
  has to fit in RAM. A streaming loader would need a two-pass design: reference index first, then map.
- **No incremental load.** Every run is a full rebuild. There is no change-data-capture, no upsert, and
  no deletion handling, so a resource removed at source stays in the CDM.
- **DuckDB by default.** OHDSI publishes the same v5.4 model for PostgreSQL, SQL Server, Snowflake,
  BigQuery and Spark; `scripts/fetch_vendor.py --dialect postgresql` vendors those. Only the DDL parse
  and the connection are dialect-specific, but nothing outside DuckDB has been exercised.
- **Not a FHIR server.** There is no REST API, no subscriptions, no `$export`. Reading from a live
  server is limited to what `fhir_server.py` does for validation.

## Resource types out of scope

Counted in every load report, never silently dropped:

| Resource | Why it is out of scope |
|---|---|
| `DiagnosticReport` | Its results are already mapped through the `Observation` resources it references, so mapping it too would double-count them. Panel-level grouping has no clean CDM home |
| `Claim`, `ExplanationOfBenefit` | Claims and costs belong in COST and PAYER_PLAN_PERIOD, which need a payer model this bridge does not have |
| `AllergyIntolerance` | Belongs in OBSERVATION, but the substance→concept mapping needs the vocabulary's allergen hierarchy to be worth anything |
| `MedicationAdministration` | Would be a second DRUG_EXPOSURE source; reconciling it against `MedicationRequest` without double-counting is real work |
| `Device`, `SupplyDelivery` | DEVICE_EXPOSURE, not implemented |
| `ImagingStudy` | No CDM home in v5.4 beyond a PROCEDURE_OCCURRENCE stub |
| `DocumentReference`, `Provenance`, `Media` | NOTE and NOTE_NLP, not implemented |
| `CarePlan`, `CareTeam`, `Goal` | No CDM representation |
| `PractitionerRole`, `Location` | Would populate `provider.specialty_concept_id` and richer CARE_SITE detail |
| `Encounter.location` | VISIT_DETAIL is not implemented at all |

Also not implemented: CONDITION_ERA, DRUG_ERA, DOSE_ERA, EPISODE, FACT_RELATIONSHIP, COHORT, and the
vocabulary tables themselves.

## Terminology

- **The vocabulary is not redistributed.** `vocab_data/concept_map.csv` covers exactly the codes in
  this repository's sample data — 217 codings. Point it at a different corpus and the mapped share
  drops to whatever that corpus happens to share with this one; rebuild with
  `scripts/build_concept_map.py`, or supply a map derived from your own licensed Athena download via
  `--concept-map`.
- **A public WebAPI instance is not a vocabulary release.** The committed map records the service and
  date it came from. It is a demo instance, its vocabulary snapshot is whatever that instance holds,
  and it can change or disappear. For anything real, resolve against your own vocabulary.
- **`source_only` and `unresolved` rows are not failures to fix by guessing.** 11 codings resolve to a
  non-standard source concept with no `Maps to`, and 4 UCUM annotation-syntax units (`{score}`, `/a`,
  `{#}`, `mL/min/{1.73_m2}`) are not in the vocabulary as literal codes. They stay as
  `concept_id = 0`.
- **No `Maps to value` handling.** A source code that should split into a concept plus a value
  (common for ICD-10-CM and for pre-coordinated SNOMED) maps only to the concept.
- **Domain routing depends on mapping quality.** An unmapped code falls back to FHIR category, and a
  code with neither lands in OBSERVATION with a numeric value. Both fallbacks are counted per run for
  exactly this reason.

## Stated conventions that would bite an analysis

- **`drug_exposure_end_date` = start date.** A `MedicationRequest` has no end and the column is NOT
  NULL. Any drug-era or exposure-duration analysis over this data is meaningless without fixing that
  from a dispense or administration feed.
- **`visit_end_date` = start date for an open encounter.** Same reason.
- **OBSERVATION_PERIOD is derived from observed events**, not from enrolment or coverage. It cannot
  distinguish "not sick" from "not observed", which is the failure mode it looks most like.
- **Timezone offsets are asserted on export.** The CDM stores none, so `--assume-offset` (default
  `+00:00`) decides. If the source system was not UTC, every exported timestamp is wrong by that
  offset while remaining valid FHIR.
- **Six FHIR elements are emitted from constants** because FHIR requires them and the CDM has no
  column: `Encounter.status`, `Observation.status`, `MedicationRequest.status` and `.intent`,
  `Immunization.status`, plus the offset above. They carry no information from the data.
- **`condition_status_concept_id` is always NULL.** `Condition.clinicalStatus` is kept as a source
  value instead, because that CDM column means admission/discharge diagnosis and the two are routinely
  conflated.

## Validation

- **`fhir.resources` R4B models stand in for R4 structural validation.** The two are identical for
  every resource emitted here, and R4B is what the library maintains. The R4 check that counts is
  HAPI's `$validate`, which is why it exists as a separate job.
- **HAPI validates against base R4, not US Core.** Profile conformance (`us-core-patient` and
  friends) is not asserted anywhere. The Synthea input claims US Core profiles; the export claims
  nothing.
- **No terminology server.** HAPI here cannot check that a SNOMED code exists, only that the resource
  is well formed. `dom-6` narrative warnings (2,768 of them) are best-practice recommendations, not
  errors, and they correspond to a real loss: the CDM has nowhere to put `Resource.text`.
- **The conformance checks are not the OHDSI Data Quality Dashboard.** DQD runs over 3,000 checks
  across plausibility, conformance and completeness. This runs 216, generated from the DDL plus five
  clinical assertions. It is a gate, not a certification.

## Testing

- The test corpus is hand-built and small: one patient, one visit, and one resource per branch worth
  exercising. It is designed to make each decision testable, not to be representative.
- Every check has a test that corrupts a database until the check fires. Two do not fail on this
  engine at all — DuckDB enforces the DDL's NOT NULL clauses, so `tests/test_checks.py` has to rebuild
  a table without constraints to test that path.
- No property-based or fuzz testing of the FHIR parser, and no performance test.

## Not evaluated at all

Bulk FHIR (`$export` NDJSON at scale), SMART on FHIR authorisation, CDS Hooks, US Core / USCDI profile
validation, TEFCA participation, consent, de-identification, and any form of patient matching beyond
trusting the source identifier.
