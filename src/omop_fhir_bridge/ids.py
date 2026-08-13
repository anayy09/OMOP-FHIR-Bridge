"""Surrogate key minting, and why it is not a hash.

OMOP CDM v5.4 declares every primary key as ``integer`` — 32-bit signed. FHIR identifies resources
with UUIDs. The tempting one-liner is to hash the UUID into 31 bits, and it is wrong: by the
birthday bound, ``n(n-1)/2m`` collisions are expected, so at 100,000 resources against
m = 2^31 that is about **2.3 expected collisions**, and a collision means two patients silently
becoming one person_id. At a million resources it is 233.

So keys are minted sequentially per table in first-seen order, and every one is recorded in
``bridge_source_map`` with the FHIR reference it came from. That buys three things a hash does not:
no collisions by construction, a row-level audit trail back to the source resource, and the ability
to re-run the ETL and get the same keys as long as the input is fed in the same order (the loader
sorts its input files to guarantee that).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IdMinter:
    """Assigns dense sequential surrogate keys per OMOP table and remembers the mapping."""

    _next: dict[str, int] = field(default_factory=dict)
    _assigned: dict[tuple[str, str], int] = field(default_factory=dict)

    def mint(self, table: str, source_key: str) -> int:
        """Return the surrogate key for ``source_key``, assigning one on first sight."""
        cache_key = (table, source_key)
        existing = self._assigned.get(cache_key)
        if existing is not None:
            return existing
        nxt = self._next.get(table, 1)
        self._next[table] = nxt + 1
        self._assigned[cache_key] = nxt
        return nxt

    def get(self, table: str, source_key: str) -> int | None:
        return self._assigned.get((table, source_key))

    def known(self, table: str, source_key: str) -> bool:
        return (table, source_key) in self._assigned

    def count(self, table: str) -> int:
        return self._next.get(table, 1) - 1

    def rows(self) -> list[tuple[str, str, int]]:
        """(omop_table, source_reference, surrogate_key) for the lineage table."""
        return [(t, k, v) for (t, k), v in self._assigned.items()]

    def max_key(self) -> int:
        return max((n - 1 for n in self._next.values()), default=0)

    def exceeds_int32(self) -> bool:
        return self.max_key() > 2_147_483_647
