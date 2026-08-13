"""OMOP concept identifiers this bridge hard-codes, and the vocabulary facts they assert.

Nothing here is asserted from memory. ``scripts/verify_constants.py`` resolves every identifier
in ``VERIFIABLE`` against a live OHDSI vocabulary service and fails if the returned concept name,
domain or standard flag disagrees with what is written below. The committed result of that run is
``docs/reports/concept-id-verification.md``.

Clinical codes (SNOMED, LOINC, RxNorm, CVX, UCUM) are deliberately *not* here. Those are resolved
through the concept map in ``vocab.py``, because they are vocabulary data rather than structural
constants, and because inventing a clinical concept_id is the single easiest way to make an OMOP
database quietly wrong.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# The one identifier every OMOP ETL needs and the one most often misused.
# 0 is a real row in CONCEPT ("No matching concept"). It means "this ETL looked and did not find
# a mapping", which is a different and much more honest statement than NULL or a guess.
# ---------------------------------------------------------------------------
NO_MATCHING_CONCEPT = 0

# --- Person demographics ---------------------------------------------------
# FHIR AdministrativeGender -> OMOP Gender. "other" and "unknown" have no standard Gender concept,
# so they resolve to 0 and keep their source value.
GENDER_BY_FHIR_CODE = {"male": 8507, "female": 8532}

# US Core race / ethnicity OMB category codes -> OMOP Race and Ethnicity concepts.
RACE_BY_OMB_CODE = {
    "2106-3": 8527,  # White
    "2054-5": 8516,  # Black or African American
    "2028-9": 8515,  # Asian
    "1002-5": 8657,  # American Indian or Alaska Native
    "2076-8": 8557,  # Native Hawaiian or Other Pacific Islander
}
ETHNICITY_BY_OMB_CODE = {
    "2135-2": 38003563,  # Hispanic or Latino
    "2186-5": 38003564,  # Not Hispanic or Latino
}

# --- Visits ----------------------------------------------------------------
# HL7 v3 ActCode encounter class -> OMOP Visit. Anything absent resolves to 0 rather than being
# forced into "Outpatient Visit", which is the usual way visit counts get silently inflated.
VISIT_BY_ACT_CODE = {
    "IMP": 9201,  # Inpatient Visit
    "ACUTE": 9201,
    "NONAC": 9201,
    "AMB": 9202,  # Outpatient Visit
    "OBSENC": 9202,
    "SS": 9202,
    "VR": 9202,
    "EMER": 9203,  # Emergency Room Visit
    "HH": 581476,  # Home Visit
}

# --- Type concepts ---------------------------------------------------------
# Provenance of the record, not provenance of the clinical event. Everything the bridge writes came
# out of an EHR-shaped FHIR feed, so these are the honest values.
TYPE_EHR = 32817  # EHR
TYPE_EHR_ENCOUNTER = 32827  # EHR encounter record
TYPE_EHR_PRESCRIPTION = 32838  # EHR prescription

# --- Metadata --------------------------------------------------------------
CDM_VERSION_CONCEPT_ID = 756265  # OMOP CDM Version 5.4.0

# ---------------------------------------------------------------------------
# Verification contract: concept_id -> (concept_name, domain_id, standard_concept)
# ---------------------------------------------------------------------------
VERIFIABLE: dict[int, tuple[str, str, str]] = {
    0: ("No matching concept", "Metadata", "N"),
    8507: ("MALE", "Gender", "S"),
    8532: ("FEMALE", "Gender", "S"),
    8527: ("White", "Race", "S"),
    8516: ("Black or African American", "Race", "S"),
    8515: ("Asian", "Race", "S"),
    8657: ("American Indian or Alaska Native", "Race", "S"),
    8557: ("Native Hawaiian or Other Pacific Islander", "Race", "S"),
    38003563: ("Hispanic or Latino", "Ethnicity", "S"),
    38003564: ("Not Hispanic or Latino", "Ethnicity", "S"),
    9201: ("Inpatient Visit", "Visit", "S"),
    9202: ("Outpatient Visit", "Visit", "S"),
    9203: ("Emergency Room Visit", "Visit", "S"),
    581476: ("Home Visit", "Visit", "S"),
    32817: ("EHR", "Type Concept", "S"),
    32827: ("EHR encounter record", "Type Concept", "S"),
    32838: ("EHR prescription", "Type Concept", "S"),
    756265: ("OMOP CDM Version 5.4.0", "Metadata", "S"),
}

# --- FHIR system URIs ------------------------------------------------------
SYSTEM_SNOMED = "http://snomed.info/sct"
SYSTEM_LOINC = "http://loinc.org"
SYSTEM_RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
SYSTEM_CVX = "http://hl7.org/fhir/sid/cvx"
SYSTEM_UCUM = "http://unitsofmeasure.org"
SYSTEM_ICD10CM = "http://hl7.org/fhir/sid/icd-10-cm"

# FHIR code system URI -> OMOP vocabulary_id. Used to disambiguate when one code string exists in
# several vocabularies, which it very often does.
VOCABULARY_BY_SYSTEM = {
    SYSTEM_SNOMED: "SNOMED",
    SYSTEM_LOINC: "LOINC",
    SYSTEM_RXNORM: "RxNorm",
    SYSTEM_CVX: "CVX",
    SYSTEM_UCUM: "UCUM",
    SYSTEM_ICD10CM: "ICD10CM",
}

EXT_US_CORE_RACE = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race"
EXT_US_CORE_ETHNICITY = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-ethnicity"
