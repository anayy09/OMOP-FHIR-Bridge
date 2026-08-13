# OMOP-FHIR-Bridge

[![CI](https://github.com/anayy09/OMOP-FHIR-Bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/anayy09/OMOP-FHIR-Bridge/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![OMOP CDM v5.4](https://img.shields.io/badge/OMOP%20CDM-v5.4-0b7285)](vendor/README.md)
[![FHIR R4](https://img.shields.io/badge/FHIR-R4-c92a2a)](docs/reports/fhir-server-validation.md)

Bidirectional **FHIR R4 ⇄ OMOP CDM v5.4** mapping, with the conformance checks generated from
OHDSI's own DDL and the round-trip losses measured rather than described.

The claim a mapper usually makes is "it maps". That is not checkable, so this one reports numbers
instead, and regenerates them on every push:

| | |
|---|---|
| FHIR resources read | **2,552** (Synthea R4 sample + an HL7 v2 ADT feed) |
| OMOP rows written | **2,109** across 13 CDM tables |
| Code lookups resolved to a standard concept | **90.0%** — and the unmapped 10% is itemised by code |
| Conformance checks passing | **216 / 216** error-severity, from the vendored OHDSI DDL |
| Exported resources accepted by a real FHIR server | **1,822 / 1,822**, zero errors, HAPI FHIR 8.0.0 `$validate` |
| Round-trip field comparisons | **8,460** — 93.0% byte-identical, 556 provably unrepresentable |
| Hard-coded concept ids verified against OHDSI | **18 / 18** |

Everything above is produced by `ofb pipeline` and written to [`docs/reports/`](docs/reports/):
[load](docs/reports/load-report.md) ·
[conformance](docs/reports/conformance-report.md) ·
[round trip](docs/reports/roundtrip-report.md) ·
[concept ids](docs/reports/concept-id-verification.md).

## Run it

Nothing to configure and no server needed — the target is DuckDB and the sample data is committed.

```bash
pip install -e .
ofb pipeline                # init-db → hl7v2 → load → export → round-trip → check
```

That regenerates every report and exits non-zero if a conformance gate fails. To validate the export
against a real FHIR server as well:

```bash
docker compose up -d hapi                                    # HAPI FHIR R4, ~1 min to boot
ofb pipeline --fhir-server http://localhost:8080/fhir
```

Individual stages (`ofb load`, `ofb check`, `ofb export`, `ofb roundtrip`, `ofb hl7v2`) all run
standalone; `ofb -h` lists them.

## What it actually does

```
HL7 v2 ADT ─┐
            ├─► FHIR R4 ─► terminology resolution ─► OMOP CDM v5.4 (DuckDB) ─► FHIR R4 ─► HAPI $validate
Synthea R4 ─┘                    │                        │                      │
                                 ▼                        ▼                      ▼
                          coverage report        216 conformance checks    round-trip report
```

**Loading** maps Patient, Encounter, Condition, Procedure, MedicationRequest, Immunization and
Observation onto PERSON, VISIT_OCCURRENCE, CONDITION_OCCURRENCE, PROCEDURE_OCCURRENCE, DRUG_EXPOSURE,
MEASUREMENT and OBSERVATION, plus LOCATION, CARE_SITE, PROVIDER, DEATH, OBSERVATION_PERIOD and
CDM_SOURCE. Field-by-field detail is in [`docs/mapping-spec.md`](docs/mapping-spec.md).

**HL7 v2 goes through FHIR, not around it.** ADT A01/A03/A04/A08 become a FHIR transaction bundle and
then take the same path as any other input, so registration data has one route into the CDM rather
than two implementations of "what is a visit".

**Exporting** goes back the other way and is validated twice: against the FHIR R4B resource models,
and against a running HAPI FHIR server's `$validate`.

## Six decisions worth arguing about

**Surrogate keys are minted, not hashed.** CDM v5.4 declares every primary key `integer` — 32 bits.
Hashing FHIR UUIDs into 31 bits gives about **2.3 expected collisions at 100,000 resources** by the
birthday bound, and a collision merges two patients. Keys are assigned sequentially and every one is
recorded in a `bridge_source_map` lineage table, so there are no collisions and every row traces back
to the resource that produced it.

**A FHIR `Observation` is not an OMOP observation.** MEASUREMENT versus OBSERVATION is a property of
the *concept*, so routing follows the resolved concept's `domain_id`, falls back to the FHIR category
(`laboratory` and `vital-signs` are measurements, `survey` is not), and falls back to "has a numeric
value" last. The basis for every row is counted: **856 by concept domain, 4 by category fallback**, so
the fallback share is visible instead of assumed.

**Components are rows.** A Synthea blood pressure has no top-level value — systolic and diastolic
live in `Observation.component`. Reading only `valueQuantity` loses every blood pressure in the
corpus and reports a clean run.

**An unmapped code becomes `concept_id = 0`, never a plausible neighbour.** 0 is a real OMOP concept
meaning "this ETL looked and found nothing", which is a stronger statement than NULL and a much more
honest one than a guess. The source code stays on the row, and the load report itemises every code
that missed — because *a zero count in a domain that is 71.6% mapped is not evidence of absence*.

**Conformance checks are generated from the specification.** Nullability, `varchar(n)` bounds, primary
keys and foreign keys are parsed out of the vendored OHDSI DDL, not restated in Python, so they cannot
drift from it. Four hand-written checks encode clinical rather than structural truth: an event cannot
precede its patient's birth, an event's visit must belong to the same patient, a death should not
precede the last event, and a person with no observation period cannot enter a cohort. The last two
are warnings, and both fire on this corpus — Synthea records an encounter nine days after a death, and
a registration-only ADT message produces a person with demographics and no events. Neither is papered
over.

**Concept foreign keys get a substitute, not a pass.** Almost every `*_concept_id` has a foreign key to
CONCEPT, and the OHDSI vocabulary is licence-gated so it is not in this repository. Rather than skip
those constraints, `concept_id_declared` asserts that every non-zero concept id written by the loader
came from the committed concept map or the verified constants — i.e. that nothing was invented.

## What the round trip loses

FHIR → OMOP → FHIR, joined resource by resource and compared on a declared field list. 93.0% of
comparisons come back byte-identical, and the interesting part is the rest:

- **556 comparisons cannot survive**: `PROVIDER.provider_name` is one string, so a practitioner's
  family and given names cannot come back apart. 0% retention on both, stated as a number.
- **4 immunizations return as `MedicationRequest`.** DRUG_EXPOSURE does not distinguish a vaccine from
  a prescription; the export recovers the difference from the drug concept's vocabulary, which works
  only when the code mapped. Two COVID-19 CVX codes did not, so those four rows lose their type.
- **Timezone offsets are gone.** FHIR requires an offset on any `dateTime` carrying a time; the CDM's
  `TIMESTAMP` columns store none. The exporter therefore has to *assert* one (`--assume-offset`,
  default `+00:00`) — the first version emitted naive timestamps and every resource with a time was
  invalid FHIR. There is now a regression test.
- **Six elements are fabricated to satisfy FHIR cardinality** — `Encounter.status`,
  `Observation.status`, `MedicationRequest.status` and `.intent`, `Immunization.status`, and that
  offset. All listed in the report; none of them exist in the CDM.
- **`Observation.category`, `Coding.display`, narrative and provenance have no CDM home at all.**
  HAPI agrees about the narrative: it returns 2,768 `dom-6` best-practice warnings and zero errors.

Resource-level coverage is reported separately from field-level fidelity, because a resource type
that is never mapped records no field losses and would otherwise flatter the mapper.

## Provenance

Every external artifact is vendored or generated by a script that records where it came from.

| What | Source | How it is tracked |
|---|---|---|
| OMOP CDM v5.4 DDL | [OHDSI/CommonDataModel](https://github.com/OHDSI/CommonDataModel) (Apache-2.0) | `vendor/README.md` — commit, URL, SHA-256 per file, refreshed by `scripts/fetch_vendor.py` |
| Synthetic patient data | [Synthea](https://github.com/synthetichealth/synthea) R4 sample (Apache-2.0) | `data/fhir/SOURCE.md` + `MANIFEST.json` with per-file SHA-256, unmodified upstream files |
| Concept mappings | OHDSI standardised vocabularies via a public WebAPI | `vocab_data/concept_map.csv` — per-row resolution path, service and date, built by `scripts/build_concept_map.py` |
| Hard-coded concept ids | Same service | `docs/reports/concept-id-verification.md`, checked in CI by `scripts/verify_constants.py` |
| FHIR validation | [HAPI FHIR](https://github.com/hapifhir/hapi-fhir-jpaserver-starter) 8.0.0 (Apache-2.0) | `docker-compose.yml`, run in its own CI job |
| HL7 v2 ADT messages | Authored for this repository | `data/hl7v2/SOURCE.md` — synthetic, no real patient data |

The vocabulary itself is **not** redistributed: only mappings for the codes present in this
repository's own sample data, with the licence position recorded in the CSV header.

## Limits

Read [`docs/limits.md`](docs/limits.md) before trusting this with anything real. The short version:
the loader is in-memory and batch-only; DiagnosticReport, Claim, AllergyIntolerance,
MedicationAdministration and Device are out of scope and counted rather than mapped; drug exposure end
dates equal their start dates by stated convention; OBSERVATION_PERIOD is derived from the events this
ETL saw, not from enrolment; and terminology coverage is only as good as a concept map built for one
sample corpus.

## Layout

```
src/omop_fhir_bridge/
  ddl.py           parses the vendored OHDSI DDL into a schema model
  vocab.py         concept map, lookups, mapped-share accounting
  ids.py           sequential surrogate keys + lineage
  fhir_source.py   corpus index and reference resolution (incl. conditional references)
  etl.py           FHIR R4 -> OMOP CDM v5.4
  export.py        OMOP CDM v5.4 -> FHIR R4
  checks.py        DDL-generated conformance checks + clinical checks
  roundtrip.py     field-level fidelity measurement
  hl7v2.py         HL7 v2 ADT -> FHIR R4
  reports.py       every committed report
  cli.py           `ofb`
tests/             74 tests; each check has a test that corrupts data until it fires
scripts/           vendor fetch, concept-map build, constant verification
docs/              mapping spec, limits, generated reports
```

## Licence

MIT — see [LICENSE](LICENSE). Vendored and referenced third-party material keeps its own licence, as
recorded above.
