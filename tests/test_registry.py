"""归属登记冲突检测测试。"""

import pytest

from detsp.core.registry import ConflictError, Key, OwnershipRegistry

K = Key(venue="arcus", account="default", symbol="BTC")


def test_claim_and_owner():
    r = OwnershipRegistry()
    r.claim(K, "grid")
    assert r.owner_of(K) == "grid"
    assert r.can_start(K, "grid")
    assert not r.can_start(K, "rotation_hedge")


def test_conflict_raises():
    r = OwnershipRegistry()
    r.claim(K, "grid")
    with pytest.raises(ConflictError):
        r.claim(K, "premium_arb")


def test_same_strategy_reclaim_ok():
    r = OwnershipRegistry()
    r.claim(K, "grid")
    r.claim(K, "grid")  # 同策略重复 claim 不报错
    assert r.owner_of(K) == "grid"


def test_release_frees_key():
    r = OwnershipRegistry()
    r.claim(K, "grid")
    r.release(K, "grid")
    assert r.owner_of(K) is None
    r.claim(K, "rotation_hedge")
    assert r.owner_of(K) == "rotation_hedge"


def test_release_by_non_owner_keeps():
    r = OwnershipRegistry()
    r.claim(K, "grid")
    r.release(K, "premium_arb")
    assert r.owner_of(K) == "grid"


def test_order_ownership():
    r = OwnershipRegistry()
    r.register_order("o1", K, "grid")
    assert r.owns_order("o1", "grid")
    assert not r.owns_order("o1", "rotation_hedge")
    with pytest.raises(ConflictError):
        r.register_order("o2", K, "rotation_hedge")


def test_api_key_index_conflict():
    r = OwnershipRegistry()
    r.claim_api_key("lighter", "default", 5, "grid")
    with pytest.raises(ConflictError):
        r.claim_api_key("lighter", "default", 5, "rotation_hedge")
    r.claim_api_key("lighter", "default", 6, "rotation_hedge")  # 不同 index 可以
