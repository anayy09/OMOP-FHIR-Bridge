"""Terminology resolution, and the accounting that makes an unmapped share visible."""

from __future__ import annotations

from omop_fhir_bridge.constants import SYSTEM_LOINC, SYSTEM_SNOMED
from omop_fhir_bridge.vocab import ConceptMap


def test_committed_map_loads_with_its_provenance(concept_map):
    assert len(concept_map) > 100
    provenance = concept_map.provenance
    assert provenance["vocabulary_service"].startswith("http")
    assert provenance["generated_by"] == "scripts/build_concept_map.py"
    assert "licence" in provenance["licence_note"].lower()


def test_hit_returns_a_standard_concept_with_its_domain(concept_map):
    mapping = ConceptMap.load().lookup(SYSTEM_LOINC, "8867-4")
    assert mapping.mapped
    assert mapping.domain_id == "Measurement"
    assert mapping.vocabulary_id == "LOINC"
    assert mapping.resolution in {"standard", "mapped"}


def test_miss_is_zero_and_never_a_neighbour():
    vocab = ConceptMap.load()
    mapping = vocab.lookup(SYSTEM_LOINC, "00000-0")
    assert mapping.concept_id == 0
    assert mapping.source_concept_id == 0
    assert mapping.resolution == "unresolved"
    assert mapping.source_code == "00000-0"


def test_coverage_is_recorded_per_lookup():
    vocab = ConceptMap.load()
    vocab.lookup(SYSTEM_LOINC, "8867-4", domain_hint="Measurement")
    vocab.lookup(SYSTEM_LOINC, "00000-0", domain_hint="Measurement")
    coverage = vocab.coverage.as_dict()
    assert coverage["by_domain"]["Measurement"] == {
        "mapped": 1,
        "unmapped": 1,
        "mapped_share": 0.5,
    }
    assert coverage["overall_mapped_share"] == 0.5
    assert coverage["distinct_unmapped_codes"] == 1


def test_codeable_concept_prefers_a_resolvable_coding():
    vocab = ConceptMap.load()
    mapping = vocab.lookup_coding(
        {
            "coding": [
                {"system": "http://example.org/local", "code": "LOCAL-1"},
                {"system": SYSTEM_LOINC, "code": "8867-4"},
            ]
        }
    )
    assert mapping.mapped, "an unknown local coding must not stop a resolvable one being used"


def test_empty_codeable_concept_is_a_miss_not_a_crash():
    vocab = ConceptMap.load()
    assert not vocab.lookup_coding(None).mapped
    assert not vocab.lookup_coding({}).mapped
    assert not vocab.lookup_coding({"coding": []}).mapped


def test_reverse_lookups_support_the_export_direction(concept_map):
    assert concept_map.system_for_source_code("8867-4") == SYSTEM_LOINC
    assert concept_map.system_for_source_code("10509002") == SYSTEM_SNOMED
    assert concept_map.system_for_source_code("nothing-like-this") is None
    vaccine = concept_map.lookup("http://hl7.org/fhir/sid/cvx", "08")
    assert concept_map.vocabulary_for_concept_id(vaccine.concept_id) == "CVX"


def test_declared_concept_ids_exclude_zero(concept_map):
    declared = concept_map.declared_concept_ids()
    assert 0 not in declared
    assert len(declared) > 100
