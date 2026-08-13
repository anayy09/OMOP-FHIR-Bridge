# Sample FHIR R4 data — provenance

These files are **unmodified** upstream Synthea output. Nothing here was edited, trimmed or
regenerated, so a reader can diff them against the upstream archive.

- Upstream: **[Synthea](https://github.com/synthetichealth/synthea)** synthetic patient generator,
  Apache-2.0, via the published FHIR R4 sample archive
  `https://synthetichealth.github.io/synthea-sample-data/downloads/latest/synthea_sample_data_fhir_latest.zip`
- Downloaded: 2026-08-12
- Content: 5 of the 108 patient bundles in that archive, plus the two directory files every bundle
  references, chosen so the corpus exercises deceased patients, allergies, immunisations, prescriptions
  reached through `medicationReference`, and component-only observations.
- Per-file byte counts and SHA-256 digests: [`MANIFEST.json`](MANIFEST.json)

**There is no real patient data here.** Synthea patients are statistically generated and correspond to
no living person.

## Files

| File | Role |
|---|---|
| `Ebony669_Ziemann98_*.json` | deceased patient — exercises DEATH and the post-mortem-event warning |
| `Glennie916_Frami345_*.json` | small paediatric record, immunisation-heavy |
| `Kayleigh718_Schimmel440_*.json` | small record with a prescription |
| `Michelina932_Mitchell808_*.json` | procedure-heavy record |
| `Rodrigo242_Hahn503_*.json` | conditions, allergies and procedures |
| `hospitalInformation*.json` | 278 Organization + Location resources; the target of every `Encounter.serviceProvider` |
| `practitionerInformation*.json` | 278 Practitioner + PractitionerRole resources; the target of every conditional `Practitioner?identifier=…` reference |

The two directory files are why this bridge resolves conditional references at all: within a patient
bundle the practitioner and organisation are referenced by business identifier into a *different file*,
and a mapper that only handles `ResourceType/id` drops every provider and care site while reporting a
clean run.

## Using the whole archive

```bash
curl -LO https://synthetichealth.github.io/synthea-sample-data/downloads/latest/synthea_sample_data_fhir_latest.zip
unzip synthea_sample_data_fhir_latest.zip -d /tmp/synthea
python scripts/build_concept_map.py --src /tmp/synthea --out /tmp/concept_map.csv
ofb init-db --db out/full.duckdb --force
ofb load --src /tmp/synthea --db out/full.duckdb --concept-map /tmp/concept_map.csv
```

Rebuilding the concept map first matters: the committed map covers only the codes in these five
bundles, so loading the full archive against it would report a much lower mapped share — correctly,
but for the wrong reason.
