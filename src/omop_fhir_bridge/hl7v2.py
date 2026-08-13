"""HL7 v2 ADT -> FHIR R4, so the v2 feed reaches OMOP through exactly one mapper.

The alternative design is to map ADT straight into OMOP, and it is the wrong one: it doubles the
number of places that decide what a visit is. Here an ADT message becomes a FHIR transaction bundle
and then goes through the same loader, the same terminology resolution and the same conformance
checks as any other FHIR input. Registration data has one path into the CDM, not two.

Scope is deliberately narrow and stated: A01 (admit), A03 (discharge), A04 (register outpatient) and
A08 (update). Those four carry the demographics and the visit, which is what OMOP's PERSON and
VISIT_OCCURRENCE need. Orders and results (ORM, ORU) are not handled -- see docs/limits.md.

Parsing is hand-rolled against the v2.5.1 segment layout rather than pulled from a library, because
the encoding rules are four characters of MSH-2 and the alternative is a dependency that mostly
does the same thing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

VISIT_CLASS_BY_PATIENT_CLASS = {
    "I": "IMP",
    "O": "AMB",
    "E": "EMER",
    "R": "AMB",
    "B": "IMP",
    "P": "AMB",
}

GENDER_BY_ADMINISTRATIVE_SEX = {"M": "male", "F": "female", "O": "other", "U": "unknown"}

# CDC race / ethnicity codes arrive in PID-10 / PID-22 as CWE triplets; the OMB category code is
# the first component, which is the same value US Core carries in ombCategory.
EXT_RACE = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race"
EXT_ETHNICITY = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-ethnicity"
OMB_SYSTEM = "urn:oid:2.16.840.1.113883.6.238"

_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _fhir_id(raw: str) -> str:
    """FHIR ids are restricted to [A-Za-z0-9-.]{1,64}; MRNs are not."""
    cleaned = _ID_SAFE.sub("-", raw).strip("-") or "unknown"
    return cleaned[:64]


def _timestamp(value: str) -> str | None:
    """HL7 TS (YYYY[MM[DD[HH[MM[SS]]]]][+/-ZZZZ]) -> FHIR dateTime."""
    if not value:
        return None
    text = value.strip()
    offset = ""
    match = re.search(r"([+-]\d{4})$", text)
    if match:
        offset = f"{match.group(1)[:3]}:{match.group(1)[3:]}"
        text = text[: match.start()]
    text = text.split(".")[0]
    digits = re.sub(r"\D", "", text)
    if len(digits) < 4:
        return None
    year, month, day = digits[0:4], digits[4:6] or "01", digits[6:8] or "01"
    if len(digits) <= 8:
        return f"{year}-{month}-{day}"
    hour, minute, second = digits[8:10] or "00", digits[10:12] or "00", digits[12:14] or "00"
    return f"{year}-{month}-{day}T{hour}:{minute}:{second}{offset or '+00:00'}"


@dataclass
class Encoding:
    field_sep: str = "|"
    component: str = "^"
    repetition: str = "~"
    escape: str = "\\"
    subcomponent: str = "&"

    @classmethod
    def from_msh(cls, msh_line: str) -> Encoding:
        field_sep = msh_line[3] if len(msh_line) > 3 else "|"
        chars = msh_line.split(field_sep)[1] if field_sep in msh_line else "^~\\&"
        chars = (chars + "^~\\&")[:4]
        return cls(field_sep, chars[0], chars[1], chars[2], chars[3])


@dataclass
class Segment:
    name: str
    fields: list[str]
    encoding: Encoding

    def raw(self, index: int) -> str:
        """1-indexed field access following HL7 convention (MSH-1 is the field separator)."""
        position = index if self.name != "MSH" else index - 1
        if position < 1 or position >= len(self.fields):
            return ""
        return self.fields[position]

    def component(self, index: int, component: int = 1) -> str:
        parts = self.raw(index).split(self.encoding.component)
        return parts[component - 1].strip() if len(parts) >= component else ""

    def repetitions(self, index: int) -> list[str]:
        raw = self.raw(index)
        return [r for r in raw.split(self.encoding.repetition) if r] if raw else []


@dataclass
class Message:
    segments: list[Segment]
    encoding: Encoding
    warnings: list[str] = field(default_factory=list)

    def segment(self, name: str) -> Segment | None:
        return next((s for s in self.segments if s.name == name), None)

    @property
    def message_type(self) -> str:
        msh = self.segment("MSH")
        if msh is None:
            return ""
        return f"{msh.component(9, 1)}^{msh.component(9, 2)}".strip("^")

    @property
    def trigger_event(self) -> str:
        msh = self.segment("MSH")
        return msh.component(9, 2) if msh else ""

    @property
    def control_id(self) -> str:
        msh = self.segment("MSH")
        return msh.raw(10) if msh else ""


def parse_message(text: str) -> Message:
    lines = [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]
    if not lines or not lines[0].startswith("MSH"):
        raise ValueError("HL7 v2 message must start with an MSH segment")
    encoding = Encoding.from_msh(lines[0])
    segments = [
        Segment(name=line[:3], fields=line.split(encoding.field_sep), encoding=encoding)
        for line in lines
    ]
    return Message(segments=segments, encoding=encoding)


def split_messages(text: str) -> list[str]:
    """Split a file that concatenates several messages, on each MSH boundary."""
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = re.split(r"\n(?=MSH\|)", normalised)
    return [p.strip() for p in parts if p.strip()]


SUPPORTED_EVENTS = {"A01", "A03", "A04", "A08"}


def to_resources(message: Message) -> tuple[list[dict], list[str]]:
    """One ADT message -> [Patient, Encounter?]. Unsupported events yield nothing but a warning."""
    warnings: list[str] = []
    event = message.trigger_event
    if event not in SUPPORTED_EVENTS:
        return [], [f"{message.message_type or 'message'} {event or '(no event)'} not supported"]

    pid = message.segment("PID")
    if pid is None:
        return [], ["message has no PID segment; nothing to map"]

    mrn = pid.component(3, 1) or pid.component(2, 1)
    if not mrn:
        return [], ["PID-3 carries no patient identifier; refusing to invent one"]
    assigning_authority = pid.component(3, 4)
    patient_id = _fhir_id(mrn)

    patient: dict = {
        "resourceType": "Patient",
        "id": patient_id,
        "identifier": [
            {
                "system": f"urn:hl7v2:{assigning_authority}" if assigning_authority else "urn:hl7v2:mrn",
                "value": mrn,
                "type": {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                            "code": pid.component(3, 5) or "MR",
                        }
                    ]
                },
            }
        ],
    }
    family, given = pid.component(5, 1), pid.component(5, 2)
    if family or given:
        patient["name"] = [{"family": family or None, "given": [given] if given else None}]
        patient["name"][0] = {k: v for k, v in patient["name"][0].items() if v}
    sex = pid.component(8, 1).upper()
    if sex:
        gender = GENDER_BY_ADMINISTRATIVE_SEX.get(sex)
        if gender is None:
            warnings.append(f"PID-8 sex {sex!r} has no FHIR AdministrativeGender; left unknown")
        patient["gender"] = gender or "unknown"
    birth = _timestamp(pid.raw(7))
    if birth:
        patient["birthDate"] = birth[:10]
    if pid.component(30, 1).upper() == "Y" or pid.raw(29):
        death = _timestamp(pid.raw(29))
        patient["deceasedDateTime"] = death or None
        if not death:
            patient["deceasedBoolean"] = True
            patient.pop("deceasedDateTime", None)
            warnings.append("PID-30 says deceased but PID-29 carries no date")

    address_field = pid.raw(11)
    if address_field:
        street, city, state = pid.component(11, 1), pid.component(11, 3), pid.component(11, 4)
        postal, country = pid.component(11, 5), pid.component(11, 6)
        address = {
            "line": [street] if street else None,
            "city": city or None,
            "state": state or None,
            "postalCode": postal or None,
            "country": country or None,
        }
        patient["address"] = [{k: v for k, v in address.items() if v}]

    extensions = []
    for field_index, url in ((10, EXT_RACE), (22, EXT_ETHNICITY)):
        code = pid.component(field_index, 1)
        if code:
            extensions.append(
                {
                    "url": url,
                    "extension": [
                        {"url": "ombCategory", "valueCoding": {"system": OMB_SYSTEM, "code": code}}
                    ],
                }
            )
    if extensions:
        patient["extension"] = extensions

    resources = [patient]

    pv1 = message.segment("PV1")
    if pv1 is not None:
        patient_class = pv1.component(2, 1).upper()
        visit_number = pv1.component(19, 1) or f"{mrn}-{message.control_id}"
        start = _timestamp(pv1.raw(44))
        end = _timestamp(pv1.raw(45))
        if start is None:
            warnings.append(
                f"PV1-44 admit datetime missing on {event}; encounter skipped because "
                "VISIT_OCCURRENCE requires a start date"
            )
        else:
            act_code = VISIT_CLASS_BY_PATIENT_CLASS.get(patient_class)
            if act_code is None and patient_class:
                warnings.append(
                    f"PV1-2 patient class {patient_class!r} has no v3 ActCode; visit will resolve "
                    "to concept 0 rather than being guessed"
                )
            encounter: dict = {
                "resourceType": "Encounter",
                "id": _fhir_id(visit_number),
                "identifier": [{"system": "urn:hl7v2:visit-number", "value": visit_number}],
                "status": "finished" if (event == "A03" or end) else "in-progress",
                "class": {
                    "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                    "code": act_code or patient_class or "AMB",
                },
                "subject": {"reference": f"Patient/{patient_id}"},
                "period": {"start": start, **({"end": end} if end else {})},
            }
            admission_type = pv1.component(4, 1)
            if admission_type:
                encounter["type"] = [
                    {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/v2-0007",
                                "code": admission_type,
                            }
                        ]
                    }
                ]
            resources.append(encounter)
    return resources, warnings


@dataclass
class TranslationResult:
    bundle: dict
    messages: int = 0
    by_event: dict[str, int] = field(default_factory=dict)
    resources: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "messages": self.messages,
            "by_event": self.by_event,
            "resources": self.resources,
            "warnings": self.warnings,
        }


def translate_directory(source: Path | str) -> TranslationResult:
    """Every ``.hl7`` file under ``source`` -> one FHIR transaction bundle.

    Repeated messages about the same patient or visit collapse onto one resource, last write
    winning per element, which is what an A08 update means.
    """
    source = Path(source)
    paths = sorted(
        p for p in ([source] if source.is_file() else source.rglob("*"))
        if p.suffix.lower() in {".hl7", ".txt"}
    )
    by_key: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    events: dict[str, int] = {}
    warnings: list[str] = []
    count = 0

    for path in paths:
        for raw in split_messages(path.read_text(encoding="utf-8")):
            try:
                message = parse_message(raw)
            except ValueError as exc:
                warnings.append(f"{path.name}: {exc}")
                continue
            count += 1
            events[message.trigger_event or "(none)"] = (
                events.get(message.trigger_event or "(none)", 0) + 1
            )
            resources, message_warnings = to_resources(message)
            warnings.extend(f"{path.name}: {w}" for w in message_warnings)
            for resource in resources:
                key = (resource["resourceType"], resource["id"])
                if key in by_key:
                    existing = by_key[key]
                    for field_name, value in resource.items():
                        if field_name == "period" and "period" in existing:
                            existing["period"] = {**existing["period"], **value}
                        else:
                            existing[field_name] = value
                else:
                    by_key[key] = resource
                    order.append(key)

    entries = [
        {"fullUrl": f"urn:uuid:{by_key[k]['id']}", "resource": by_key[k]} for k in order
    ]
    counts: dict[str, int] = {}
    for rtype, _rid in order:
        counts[rtype] = counts.get(rtype, 0) + 1
    return TranslationResult(
        bundle={"resourceType": "Bundle", "type": "transaction", "entry": entries},
        messages=count,
        by_event=dict(sorted(events.items())),
        resources=dict(sorted(counts.items())),
        warnings=warnings,
    )
