"""Reading a FHIR R4 corpus, and resolving the three kinds of reference it actually contains.

Synthea's transaction bundles are useful precisely because they are not tidy. Within one bundle a
patient is referenced as ``urn:uuid:...``; the practitioner who performed an encounter is referenced
as a **conditional reference** — ``Practitioner?identifier=http://hl7.org/fhir/sid/us-npi|999...`` —
which points into a *different* file; and organisations arrive the same way. Any mapper that only
handles ``ResourceType/id`` silently drops every provider and care site, and the OMOP output looks
complete because those columns are nullable.

So references are resolved against an index built over the whole corpus, by id, by fullUrl and by
business identifier, and anything still unresolved is counted and reported rather than dropped.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

_SKIP_FILES = {"MANIFEST.json"}


@dataclass
class FhirCorpus:
    """An in-memory index over a directory of FHIR bundles or NDJSON files.

    In-memory is a real limit and is documented in ``docs/limits.md``: this is a batch mapper sized
    for corpora that fit in RAM, not a streaming pipeline.
    """

    files: list[Path] = field(default_factory=list)
    by_type: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    by_id: dict[tuple[str, str], dict] = field(default_factory=dict)
    by_full_url: dict[str, dict] = field(default_factory=dict)
    by_identifier: dict[tuple[str, str, str], dict] = field(default_factory=dict)
    unresolved_references: Counter = field(default_factory=Counter)

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, source: Path | str) -> FhirCorpus:
        source = Path(source)
        paths = sorted(
            p
            for p in ([source] if source.is_file() else source.rglob("*"))
            if p.suffix.lower() in {".json", ".ndjson"} and p.name not in _SKIP_FILES
        )
        corpus = cls(files=paths)
        for path in paths:
            if path.suffix.lower() == ".ndjson":
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        corpus._add(json.loads(line))
            else:
                doc = json.loads(path.read_text(encoding="utf-8"))
                if doc.get("resourceType") == "Bundle":
                    for entry in doc.get("entry") or []:
                        resource = entry.get("resource")
                        if resource:
                            corpus._add(resource, full_url=entry.get("fullUrl"))
                else:
                    corpus._add(doc)
        return corpus

    def _add(self, resource: dict, full_url: str | None = None) -> None:
        rtype = resource.get("resourceType")
        if not rtype:
            return
        self.by_type[rtype].append(resource)
        rid = resource.get("id")
        if rid:
            self.by_id[(rtype, rid)] = resource
            # A transaction bundle's fullUrl is urn:uuid:<id>; index that shape even when the
            # entry did not carry one, since intra-bundle references use it.
            self.by_full_url.setdefault(f"urn:uuid:{rid}", resource)
        if full_url:
            self.by_full_url[full_url] = resource
        for ident in resource.get("identifier") or []:
            system, value = ident.get("system"), ident.get("value")
            if value:
                self.by_identifier[(rtype, system or "", value)] = resource

    # ------------------------------------------------------------- accessors
    def resources(self, rtype: str) -> list[dict]:
        return self.by_type.get(rtype, [])

    def counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in sorted(self.by_type.items())}

    def total(self) -> int:
        return sum(len(v) for v in self.by_type.values())

    def iter_all(self) -> Iterator[dict]:
        for rtype in sorted(self.by_type):
            yield from self.by_type[rtype]

    # ------------------------------------------------------------ references
    def resolve(self, reference: dict | str | None) -> dict | None:
        """Resolve a FHIR Reference to a resource in this corpus, or None."""
        ref = reference.get("reference") if isinstance(reference, dict) else reference
        if not ref:
            return None
        if ref.startswith("urn:uuid:") or ref.startswith("urn:oid:"):
            found = self.by_full_url.get(ref)
            if found is None:
                self.unresolved_references[ref.split(":")[0] + ":*"] += 1
            return found
        if "?" in ref:  # conditional reference: Type?identifier=system|value
            rtype, _, query = ref.partition("?")
            for clause in query.split("&"):
                key, _, value = clause.partition("=")
                if key != "identifier":
                    continue
                system, _, ident = value.rpartition("|")
                found = self.by_identifier.get((rtype, system, ident))
                if found is None:
                    # Some feeds omit the system on one side of the join.
                    for (t, _s, v), res in self.by_identifier.items():
                        if t == rtype and v == ident:
                            return res
                    self.unresolved_references[f"{rtype}?identifier"] += 1
                return found
            self.unresolved_references[f"{rtype}?other"] += 1
            return None
        if ref.startswith("#"):
            return None  # contained resources are not indexed
        parts = ref.split("/")
        if len(parts) >= 2:
            rtype, rid = parts[-2], parts[-1]
            found = self.by_id.get((rtype, rid))
            if found is None:
                self.unresolved_references[f"{rtype}/id"] += 1
            return found
        self.unresolved_references["malformed"] += 1
        return None

    def resolve_id(self, reference: dict | str | None) -> str | None:
        """Resolve to the referenced resource's logical id, without needing the resource body."""
        resource = self.resolve(reference)
        if resource is not None:
            return resource.get("id")
        ref = reference.get("reference") if isinstance(reference, dict) else reference
        if not ref:
            return None
        if ref.startswith("urn:uuid:"):
            return ref.removeprefix("urn:uuid:")
        if "?" not in ref and "/" in ref:
            return ref.rsplit("/", 1)[-1]
        return None
