"""FHIR -> OMOP -> FHIR, measured field by field.

The question this answers is the one an interoperability reviewer actually asks: *what does the
round trip lose?* "It maps" is not an answer, and neither is a green test suite -- a mapper can drop
half of every resource and still pass its own tests.

So every exported resource is joined back to the FHIR resource it came from and compared on a
declared field list. The output separates three different kinds of outcome, because they mean
different things:

* **retained** -- the value survived OMOP and came back equal.
* **transformed** -- the value survived but not identically, with the reason recorded (a datetime
  offset the CDM cannot store, an encounter class collapsed onto a shared Visit concept).
* **dropped** -- OMOP has nowhere to put it.

Resource-level coverage is reported separately from field-level fidelity. A resource type that is
never mapped scores no field losses at all, which would flatter the mapper if the two were mixed.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from .etl import parse_datetime
from .export import ID_SYSTEM

# Field paths compared per resource type. Each entry is (label, extractor).
Extractor = Callable[[dict], object]


def _path(*keys: str) -> Extractor:
    def get(resource: dict):
        node: object = resource
        for key in keys:
            if isinstance(node, list):
                node = node[0] if node else None
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        if isinstance(node, list):
            node = node[0] if node else None
        return node

    return get


def _first_coding_code(*keys: str) -> Extractor:
    def get(resource: dict):
        node: object = resource
        for key in keys:
            if isinstance(node, list):
                node = node[0] if node else None
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        if isinstance(node, list):
            node = node[0] if node else None
        if not isinstance(node, dict):
            return None
        for coding in node.get("coding") or []:
            if coding.get("code"):
                return str(coding["code"])
        return None

    return get


def _reference_id(*keys: str) -> Extractor:
    def get(resource: dict):
        ref = _path(*keys)(resource)
        if not isinstance(ref, str):
            return None
        if ref.startswith("urn:uuid:"):
            return ref.removeprefix("urn:uuid:")
        if "?" in ref:
            return ref.split("|")[-1]
        return ref.rsplit("/", 1)[-1]

    return get


def _omb_category(url: str) -> Extractor:
    def get(resource: dict):
        for ext in resource.get("extension") or []:
            if ext.get("url") != url:
                continue
            for sub in ext.get("extension") or []:
                if sub.get("url") == "ombCategory":
                    return (sub.get("valueCoding") or {}).get("code")
        return None

    return get


EXT_RACE = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race"
EXT_ETHNICITY = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-ethnicity"

COMPARISONS: dict[str, list[tuple[str, Extractor]]] = {
    "Organization": [
        ("name", _path("name")),
        ("address.city", _path("address", "city")),
        ("address.state", _path("address", "state")),
        ("address.postalCode", _path("address", "postalCode")),
    ],
    "Practitioner": [
        # PROVIDER.provider_name is a single string, so family/given cannot come back apart. These
        # two lines exist to make that loss appear in the table rather than be described in prose.
        ("name.family", _path("name", "family")),
        ("name.given", _path("name", "given")),
        ("gender", _path("gender")),
    ],
    "Patient": [
        ("gender", _path("gender")),
        ("birthDate", _path("birthDate")),
        ("deceasedDateTime", _path("deceasedDateTime")),
        ("race.ombCategory", _omb_category(EXT_RACE)),
        ("ethnicity.ombCategory", _omb_category(EXT_ETHNICITY)),
        ("address.city", _path("address", "city")),
        ("address.postalCode", _path("address", "postalCode")),
        ("address.state", _path("address", "state")),
    ],
    "Encounter": [
        ("class.code", _path("class", "code")),
        ("period.start", _path("period", "start")),
        ("period.end", _path("period", "end")),
        ("type.coding.code", _first_coding_code("type")),
        ("subject", _reference_id("subject", "reference")),
        ("serviceProvider", _reference_id("serviceProvider", "reference")),
    ],
    "Condition": [
        ("code.coding.code", _first_coding_code("code")),
        ("onsetDateTime", _path("onsetDateTime")),
        ("abatementDateTime", _path("abatementDateTime")),
        ("clinicalStatus.coding.code", _first_coding_code("clinicalStatus")),
        ("subject", _reference_id("subject", "reference")),
        ("encounter", _reference_id("encounter", "reference")),
    ],
    "Procedure": [
        ("code.coding.code", _first_coding_code("code")),
        ("performedPeriod.start", _path("performedPeriod", "start")),
        ("subject", _reference_id("subject", "reference")),
        ("encounter", _reference_id("encounter", "reference")),
    ],
    "Observation": [
        ("code.coding.code", _first_coding_code("code")),
        ("effectiveDateTime", _path("effectiveDateTime")),
        ("valueQuantity.value", _path("valueQuantity", "value")),
        ("valueQuantity.code", _path("valueQuantity", "code")),
        ("subject", _reference_id("subject", "reference")),
        ("encounter", _reference_id("encounter", "reference")),
    ],
    "MedicationRequest": [
        ("medication.coding.code", _first_coding_code("medicationCodeableConcept")),
        ("authoredOn", _path("authoredOn")),
        ("subject", _reference_id("subject", "reference")),
        ("encounter", _reference_id("encounter", "reference")),
    ],
    "Immunization": [
        ("vaccineCode.coding.code", _first_coding_code("vaccineCode")),
        ("occurrenceDateTime", _path("occurrenceDateTime")),
        ("patient", _reference_id("patient", "reference")),
        ("encounter", _reference_id("encounter", "reference")),
    ],
}

# Source resource type -> the type it comes back as, when they differ.
TYPE_TRANSFORMATIONS = {"Immunization": "Immunization"}

# Losses that are properties of the CDM rather than of any single row. Measured where measurable,
# stated where structural.
STRUCTURAL_LOSSES = [
    (
        "timezone offset",
        "FHIR instants carry a UTC offset; OMOP's TIMESTAMP columns do not, so the offset is "
        "dropped on load. The sample corpus is entirely +00:00, so no wall-clock shift is "
        "observable here -- but a feed mixing offsets would lose them.",
    ),
    (
        "Coding.display",
        "OMOP keeps the code in *_source_value and the concept name in the vocabulary, but has no "
        "column for the source system's own display string, so it is not recoverable.",
    ),
    (
        "Observation.category",
        "laboratory / vital-signs / survey drives domain routing on load and is then gone: "
        "MEASUREMENT versus OBSERVATION is the only trace left of it.",
    ),
    (
        "resource identity",
        "Nothing in the CDM records which FHIR resource a row came from. This bridge keeps a "
        "bridge_source_map lineage table so ids can be put back; without it the round trip could "
        "not even be joined.",
    ),
    (
        "narrative and provenance",
        "Resource.text, meta.profile, Provenance and DocumentReference have no CDM home.",
    ),
]


@dataclass
class FieldTally:
    compared: int = 0
    retained: int = 0
    transformed: int = 0
    dropped: int = 0
    reasons: Counter = field(default_factory=Counter)
    examples: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "compared": self.compared,
            "retained": self.retained,
            "transformed": self.transformed,
            "dropped": self.dropped,
            "retention": round(self.retained / self.compared, 4) if self.compared else None,
            "reasons": dict(self.reasons.most_common()),
            "examples": self.examples[:3],
        }


def _normalise(value):
    """Make two representations of the same value comparable without hiding real differences."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 6)
    text = str(value).strip()
    parsed = parse_datetime(text) if len(text) >= 10 and text[4] == "-" else None
    if parsed is not None:
        return parsed.replace(microsecond=0).isoformat()
    try:
        return round(float(text), 6)
    except ValueError:
        return text


def _same_instant(original, exported) -> bool:
    a, b = parse_datetime(str(original)), parse_datetime(str(exported))
    if a is None or b is None:
        return False
    return a.replace(microsecond=0) == b.replace(microsecond=0)


class RoundTrip:
    def __init__(self, corpus, exported: dict[str, list[dict]]):
        self.corpus = corpus
        self.exported = exported

    def _original_for(self, resource: dict) -> tuple[dict | None, dict | None, str]:
        """Return (original resource, node to compare, join outcome).

        Component-derived observations carry ``<id>#componentN`` lineage, so the node compared is the
        component rather than the resource -- otherwise every blood pressure would read as a loss.
        """
        value = next(
            (
                i.get("value")
                for i in resource.get("identifier") or []
                if i.get("system") == ID_SYSTEM
            ),
            None,
        )
        if not value:
            return None, None, "no_lineage_identifier"
        base, _, component = value.partition("#")
        for rtype in ("Patient", "Encounter", "Condition", "Procedure", "Observation",
                      "MedicationRequest", "Immunization", "Organization", "Practitioner"):
            original = self.corpus.by_id.get((rtype, base))
            if original is not None:
                break
        else:
            return None, None, "original_not_found"
        node = original
        if component.startswith("component"):
            index = int(component.removeprefix("component"))
            components = original.get("component") or []
            if index < len(components):
                node = components[index]
        return original, node, "joined"

    @staticmethod
    def _reason(label: str, source_type: str, exported_type: str, before) -> str:
        """Name the transformation. "value changed" is not a finding; the mechanism is."""
        if source_type != exported_type:
            if before is None:
                return (
                    f"element belongs to {exported_type}, not {source_type}: the row came back as a "
                    f"different resource type"
                )
            return f"resource type changed: {source_type} -> {exported_type}"
        if label == "class.code":
            return "encounter class collapsed onto a shared Visit concept"
        if label == "address.state":
            return "state normalised to its USPS abbreviation to fit LOCATION.state varchar(2)"
        return "value changed"

    def run(self) -> dict:
        fields: dict[str, dict[str, FieldTally]] = defaultdict(lambda: defaultdict(FieldTally))
        joins: Counter = Counter()
        type_pairs: Counter = Counter()

        for rtype, items in self.exported.items():
            comparisons = COMPARISONS.get(rtype, [])
            for exported in items:
                original, node, outcome = self._original_for(exported)
                joins[outcome] += 1
                if original is None or node is None:
                    continue
                source_type = original.get("resourceType", "?")
                type_pairs[f"{source_type} -> {rtype}"] += 1
                for label, extract in comparisons:
                    # Codes and values of a component-derived row live on the component; identity,
                    # timing and references still live on the parent resource.
                    scope = node if (node is not original and label.startswith(
                        ("code", "valueQuantity"))) else original
                    before, after = extract(scope), extract(exported)
                    tally = fields[rtype][label]
                    if before is None and after is None:
                        continue
                    tally.compared += 1
                    if _normalise(before) == _normalise(after):
                        tally.retained += 1
                    elif before is not None and after is not None and _same_instant(before, after):
                        tally.transformed += 1
                        tally.reasons["datetime normalised (offset dropped by the CDM)"] += 1
                    elif after is None:
                        tally.dropped += 1
                        tally.reasons["not represented in the CDM"] += 1
                        if len(tally.examples) < 3:
                            tally.examples.append({"before": before, "after": None})
                    else:
                        tally.transformed += 1
                        tally.reasons[self._reason(label, source_type, rtype, before)] += 1
                        if len(tally.examples) < 3:
                            tally.examples.append({"before": before, "after": after})

        totals = Counter()
        for by_field in fields.values():
            for tally in by_field.values():
                totals["compared"] += tally.compared
                totals["retained"] += tally.retained
                totals["transformed"] += tally.transformed
                totals["dropped"] += tally.dropped

        return {
            "joins": dict(joins),
            "resource_type_pairs": dict(sorted(type_pairs.items())),
            "fields": {
                rtype: {label: tally.as_dict() for label, tally in sorted(by_field.items())}
                for rtype, by_field in sorted(fields.items())
            },
            "totals": {
                **dict(totals),
                "retention": round(totals["retained"] / totals["compared"], 4)
                if totals["compared"]
                else None,
                "retention_including_transformed": round(
                    (totals["retained"] + totals["transformed"]) / totals["compared"], 4
                )
                if totals["compared"]
                else None,
            },
            "structural_losses": [
                {"item": name, "explanation": text} for name, text in STRUCTURAL_LOSSES
            ],
        }


def resource_coverage(corpus, load_result, exported: dict[str, list[dict]]) -> list[dict]:
    """Per source resource type: how many arrived, how many produced OMOP rows, how many came back."""
    rows = []
    exported_counts = {k: len(v) for k, v in exported.items()}
    for rtype, count in sorted(corpus.counts().items()):
        rows.append(
            {
                "resource_type": rtype,
                "in_source": count,
                "omop_rows": load_result.rows_by_source_type.get(rtype, 0),
                "exported": exported_counts.get(rtype, 0),
                "mapped": rtype in load_result.rows_by_source_type,
            }
        )
    return rows
