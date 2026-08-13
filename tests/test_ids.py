"""Surrogate keys: dense, stable, collision-free, and traceable back to FHIR."""

from __future__ import annotations

from omop_fhir_bridge.ids import IdMinter


def test_keys_are_stable_within_a_run():
    minter = IdMinter()
    first = minter.mint("person", "Patient/a")
    assert minter.mint("person", "Patient/a") == first
    assert minter.mint("person", "Patient/b") != first


def test_keys_are_dense_and_start_at_one():
    minter = IdMinter()
    keys = [minter.mint("visit_occurrence", f"Encounter/{i}") for i in range(5)]
    assert keys == [1, 2, 3, 4, 5]


def test_tables_have_independent_sequences():
    minter = IdMinter()
    assert minter.mint("person", "Patient/a") == 1
    assert minter.mint("measurement", "Observation/x") == 1


def test_no_collisions_at_a_scale_where_hashing_would_collide():
    """The birthday bound puts ~2.3 expected collisions on a 31-bit hash at 100k keys.

    Sequential minting has none by construction, which is the whole argument for it.
    """
    minter = IdMinter()
    keys = {minter.mint("measurement", f"Observation/{i}") for i in range(100_000)}
    assert len(keys) == 100_000
    assert max(keys) == 100_000
    assert not minter.exceeds_int32()


def test_lineage_is_recorded_for_every_key():
    minter = IdMinter()
    minter.mint("person", "Patient/a")
    minter.mint("measurement", "Observation/x#component0")
    assert sorted(minter.rows()) == [
        ("measurement", "Observation/x#component0", 1),
        ("person", "Patient/a", 1),
    ]


def test_get_does_not_mint():
    minter = IdMinter()
    assert minter.get("person", "Patient/missing") is None
    assert minter.count("person") == 0
