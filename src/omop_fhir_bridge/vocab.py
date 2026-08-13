"""Terminology resolution, and the accounting that keeps it honest.

The OHDSI vocabulary (CONCEPT, CONCEPT_RELATIONSHIP) is licence-gated and cannot be redistributed,
so this repository ships a **concept map covering exactly the codes in its own sample data**, built
by ``scripts/build_concept_map.py`` against a public OHDSI vocabulary service and committed with a
per-row provenance stamp. Nothing is inferred, and no clinical concept_id is written that the
resolver did not return.

Two consequences, both deliberate:

* A code with no committed mapping resolves to ``concept_id = 0`` — never to a plausible neighbour.
  The source code and the source concept, when known, are preserved on the row, so the mapping can
  be redone later without re-reading FHIR.
* Every lookup is counted. ``Coverage`` reports mapped share per domain and per vocabulary, and the
  loader writes it into the run report. An OMOP database whose mapped share is unknown is an OMOP
  database whose zero counts cannot be interpreted: a domain that is 4% mapped will read as an
  absence of disease rather than an absence of mapping.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .constants import NO_MATCHING_CONCEPT, VOCABULARY_BY_SYSTEM

DEFAULT_MAP = Path(__file__).with_name("vocab_data") / "concept_map.csv"

FIELDNAMES = [
    "source_system",
    "source_code",
    "source_display",
    "source_concept_id",
    "target_concept_id",
    "target_concept_name",
    "target_domain_id",
    "target_vocabulary_id",
    "target_standard_concept",
    "resolution",
    "resolved_at",
    "resolver",
]


@dataclass(frozen=True)
class Mapping:
    """One resolved source code. ``concept_id == 0`` means "looked, found nothing"."""

    source_system: str
    source_code: str
    source_concept_id: int
    concept_id: int
    concept_name: str
    domain_id: str
    vocabulary_id: str
    resolution: str

    @property
    def mapped(self) -> bool:
        return self.concept_id != NO_MATCHING_CONCEPT


def unmapped(system: str, code: str) -> Mapping:
    return Mapping(
        source_system=system,
        source_code=code,
        source_concept_id=NO_MATCHING_CONCEPT,
        concept_id=NO_MATCHING_CONCEPT,
        concept_name="",
        domain_id="",
        vocabulary_id=VOCABULARY_BY_SYSTEM.get(system, ""),
        resolution="unresolved",
    )


@dataclass
class Coverage:
    """Mapped-share accounting, sliced the two ways that actually get asked about."""

    by_domain: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: {"mapped": 0, "unmapped": 0})
    )
    by_vocabulary: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: {"mapped": 0, "unmapped": 0})
    )
    unmapped_codes: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))

    def record(self, target_domain: str, mapping: Mapping) -> None:
        bucket = "mapped" if mapping.mapped else "unmapped"
        self.by_domain[target_domain][bucket] += 1
        self.by_vocabulary[mapping.vocabulary_id or "(unknown)"][bucket] += 1
        if not mapping.mapped:
            self.unmapped_codes[(mapping.vocabulary_id or mapping.source_system, mapping.source_code)] += 1

    @staticmethod
    def _share(counts: dict[str, int]) -> float:
        total = counts["mapped"] + counts["unmapped"]
        return counts["mapped"] / total if total else 0.0

    def domain_share(self, domain: str) -> float:
        return self._share(self.by_domain[domain])

    def overall_share(self) -> float:
        mapped = sum(c["mapped"] for c in self.by_domain.values())
        unmapped_n = sum(c["unmapped"] for c in self.by_domain.values())
        return mapped / (mapped + unmapped_n) if (mapped + unmapped_n) else 0.0

    def as_dict(self) -> dict:
        return {
            "overall_mapped_share": round(self.overall_share(), 4),
            "by_domain": {
                d: {**c, "mapped_share": round(self._share(c), 4)}
                for d, c in sorted(self.by_domain.items())
            },
            "by_vocabulary": {
                v: {**c, "mapped_share": round(self._share(c), 4)}
                for v, c in sorted(self.by_vocabulary.items())
            },
            "distinct_unmapped_codes": len(self.unmapped_codes),
            "top_unmapped_codes": [
                {"vocabulary": v, "code": c, "occurrences": n}
                for (v, c), n in sorted(self.unmapped_codes.items(), key=lambda kv: (-kv[1], kv[0]))[:25]
            ],
        }


class ConceptMap:
    """Read-only source-code -> standard-concept lookup over the committed map."""

    def __init__(self, rows: list[dict[str, str]], *, provenance: dict | None = None):
        self._by_system_code: dict[tuple[str, str], Mapping] = {}
        self._by_vocab_code: dict[tuple[str, str], Mapping] = {}
        self._system_by_code: dict[str, str] = {}
        self._vocabulary_by_concept_id: dict[int, str] = {}
        self.provenance = provenance or {}
        for r in rows:
            m = Mapping(
                source_system=r["source_system"],
                source_code=r["source_code"],
                source_concept_id=int(r["source_concept_id"] or 0),
                concept_id=int(r["target_concept_id"] or 0),
                concept_name=r["target_concept_name"],
                domain_id=r["target_domain_id"],
                vocabulary_id=r["target_vocabulary_id"],
                resolution=r["resolution"],
            )
            self._by_system_code[(m.source_system, m.source_code)] = m
            self._system_by_code.setdefault(m.source_code, m.source_system)
            if m.vocabulary_id:
                self._by_vocab_code[(m.vocabulary_id, m.source_code)] = m
                for cid in (m.concept_id, m.source_concept_id):
                    if cid:
                        self._vocabulary_by_concept_id.setdefault(cid, m.vocabulary_id)
        self.coverage = Coverage()

    @classmethod
    def load(cls, path: Path | str = DEFAULT_MAP) -> ConceptMap:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"concept map not found at {path}; run `python scripts/build_concept_map.py`"
            )
        rows, provenance = [], {}
        with path.open(newline="", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#"):
                    key, _, value = line.lstrip("# ").partition(":")
                    provenance[key.strip()] = value.strip()
                    continue
                break
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(row for row in fh if not row.startswith("#"))
            rows = list(reader)
        return cls(rows, provenance=provenance)

    def __len__(self) -> int:
        return len(self._by_system_code)

    def lookup(self, system: str | None, code: str | None, *, domain_hint: str = "") -> Mapping:
        """Resolve one coding. Records the outcome in ``self.coverage``."""
        if not code:
            m = unmapped(system or "", "")
            self.coverage.record(domain_hint, m)
            return m
        m = self._by_system_code.get((system or "", code))
        if m is None:
            vocab = VOCABULARY_BY_SYSTEM.get(system or "")
            if vocab:
                m = self._by_vocab_code.get((vocab, code))
        if m is None:
            m = unmapped(system or "", code)
        self.coverage.record(domain_hint, m)
        return m

    # --- reverse lookups, used by the OMOP -> FHIR direction ---------------
    def system_for_source_code(self, code: str | None) -> str | None:
        """Which code system a retained ``*_source_value`` came from.

        The export direction needs this to emit a FHIR ``Coding.system``. OMOP stores the code but
        not the system, so this is recovered from the same vocabulary the load direction used --
        which is what a real OMOP deployment does with its CONCEPT table.
        """
        return self._system_by_code.get(code) if code else None

    def vocabulary_for_concept_id(self, concept_id: int | None) -> str | None:
        return self._vocabulary_by_concept_id.get(concept_id) if concept_id else None

    def declared_concept_ids(self) -> set[int]:
        """Every concept_id this map can justify -- used by the ``concept_id_declared`` check."""
        out: set[int] = set()
        for m in self._by_system_code.values():
            out.add(m.concept_id)
            out.add(m.source_concept_id)
        out.discard(0)
        return out

    def lookup_coding(self, codeable_concept: dict | None, *, domain_hint: str = "") -> Mapping:
        """Resolve the first coding of a FHIR CodeableConcept in a preferred-system order."""
        codings = (codeable_concept or {}).get("coding") or []
        preferred = [c for c in codings if c.get("system") in VOCABULARY_BY_SYSTEM]
        for coding in preferred + codings:
            m = self.lookup(coding.get("system"), coding.get("code"), domain_hint=domain_hint)
            if m.mapped:
                return m
        if codings:
            first = codings[0]
            return unmapped(first.get("system", ""), first.get("code", ""))
        return unmapped("", "")


def write_map(path: Path, rows: list[dict], provenance: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        for key, value in provenance.items():
            fh.write(f"# {key}: {value}\n")
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in sorted(rows, key=lambda r: (r["source_system"], r["source_code"])):
            writer.writerow(row)
