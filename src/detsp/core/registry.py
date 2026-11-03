"""归属登记：订单与持仓的 strategy 标签与冲突拦截。

规则（见 DESIGN.md 第 5 节）：
- 键 = (venue, account, symbol)。同一键同一时刻只能被一个策略占用。
- 策略启动/接管前必须 claim；冲突抛 ConflictError，引擎拒绝该任务。
- 释放后其他策略才可接管。
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ConflictError(RuntimeError):
    """归属冲突：目标已被其他策略占用。"""


@dataclass(frozen=True)
class Key:
    venue: str
    account: str
    symbol: str


@dataclass
class OwnershipRegistry:
    _position_owner: dict[Key, str] = field(default_factory=dict)
    _order_owner: dict[str, tuple[Key, str]] = field(default_factory=dict)
    _api_key_owner: dict[tuple[str, str, int], str] = field(default_factory=dict)

    # ----- 持仓/市场归属 -----
    def owner_of(self, key: Key) -> str | None:
        return self._position_owner.get(key)

    def claim(self, key: Key, strategy: str) -> None:
        owner = self._position_owner.get(key)
        if owner is not None and owner != strategy:
            raise ConflictError(
                f"{key} 已被策略 {owner} 占用，{strategy} 不得接管"
            )
        self._position_owner[key] = strategy

    def can_start(self, key: Key, strategy: str) -> bool:
        """不抛错的预检：该策略能否接管该键。"""
        owner = self._position_owner.get(key)
        return owner is None or owner == strategy

    def release(self, key: Key, strategy: str) -> None:
        if self._position_owner.get(key) == strategy:
            del self._position_owner[key]
            # 一并注销该键下的订单登记
            self._order_owner = {
                oid: v for oid, v in self._order_owner.items()
                if not (v[0] == key and v[1] == strategy)
            }

    # ----- 订单归属 -----
    def register_order(self, order_id: str, key: Key, strategy: str) -> None:
        self.claim(key, strategy)
        self._order_owner[order_id] = (key, strategy)

    def order_strategy(self, order_id: str) -> str | None:
        v = self._order_owner.get(order_id)
        return v[1] if v else None

    def owns_order(self, order_id: str, strategy: str) -> bool:
        return self.order_strategy(order_id) == strategy

    def unregister_order(self, order_id: str) -> None:
        self._order_owner.pop(order_id, None)

    # ----- Lighter api_key_index 占用（nonce 隔离）-----
    def claim_api_key(self, venue: str, account: str, key_index: int, strategy: str) -> None:
        k = (venue, account, key_index)
        owner = self._api_key_owner.get(k)
        if owner is not None and owner != strategy:
            raise ConflictError(
                f"{venue}/{account} 的 api_key_index={key_index} 已被 {owner} 占用"
            )
        self._api_key_owner[k] = strategy
