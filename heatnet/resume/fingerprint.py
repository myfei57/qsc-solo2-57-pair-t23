"""投运参数指纹与迁移核对。

停电这段时间现场可能有人动过参数。续做之前必须把"当时落盘的指纹"和"现在从
设备／配置读回来的指纹"对一遍——这属于一次**参数迁移核对（migration gate）**：

* 指纹一致（``match``）：现场假设没漂移，核对通过，允许进入续作决议；
* 指纹不一致（``drifted``）：参数被改过。这本身不是故障，但**核对不过就不许续**，
  必须由现场重新确认一遍当前参数（:meth:`MigrationCheck.confirm`），确认后以新
  指纹重新落盘，才放行。绝不允许带着旧假设往下走。

指纹只对"会影响投运动作"的参数取值，按固定键序做稳定哈希；展示值四舍五入只用于
人看，哈希用规范化后的原始浮点，避免显示精度掩盖真实漂移。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

#: 核对状态。
PENDING = "pending"
MATCH = "match"
DRIFTED = "drifted"
CONFIRMED = "confirmed"

# 需要纳入指纹的参数键，顺序固定（决定哈希与展示的口径）。
_FINGERPRINT_KEYS: tuple[str, ...] = (
    "outdoor_c",
    "demand_pct",
    "curve_supply_c",
    "supply_target_c",
    "quality_confirm_s",
    "flow_min_t_h",
    "flow_max_t_h",
    "makeup_target_kpa",
    "makeup_min_kpa",
    "makeup_max_kpa",
)


def normalise_params(params: Mapping[str, Any]) -> dict[str, float]:
    """把参数映射收敛成固定键集合的浮点映射；缺失或非数值一律报错。"""

    result: dict[str, float] = {}
    for key in _FINGERPRINT_KEYS:
        if key not in params:
            raise ValueError(f"投运参数缺少指纹字段: {key!r}")
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"投运参数字段 {key!r} 必须是数值")
        result[key] = float(value)
    return result


def fingerprint(params: Mapping[str, Any]) -> str:
    """对一组投运参数计算稳定指纹。"""

    normalised = normalise_params(params)
    blob = json.dumps(
        {key: normalised[key] for key in _FINGERPRINT_KEYS},
        sort_keys=False,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MigrationCheck:
    """一次参数迁移核对的结论。"""

    status: str
    old_fingerprint: str
    new_fingerprint: str
    changed: tuple[str, ...]
    old_params: dict[str, float]
    new_params: dict[str, float]
    confirmed: bool = False

    @property
    def passed(self) -> bool:
        """核对是否放行（一致，或漂移后已重新确认）。"""

        return self.status in (MATCH, CONFIRMED)

    def require_passed(self) -> None:
        """核对未放行时抛联锁异常，调用方据此拒绝续作。"""

        if not self.passed:
            from ..errors import InterlockError

            if self.changed:
                detail = ",".join(self.changed)
                raise InterlockError(
                    f"停机期间参数被修改（{detail}），必须重新核对确认后才能续作",
                    component="resume",
                )
            raise InterlockError("参数尚未核对，禁止续作", component="resume")

    def confirm(self) -> "MigrationCheck":
        """现场按当前参数重新确认一遍，生成放行结论（以新指纹为准）。"""

        return MigrationCheck(
            status=CONFIRMED,
            old_fingerprint=self.old_fingerprint,
            new_fingerprint=self.new_fingerprint,
            changed=self.changed,
            old_params=self.old_params,
            new_params=self.new_params,
            confirmed=True,
        )

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "passed": self.passed,
            "confirmed": self.confirmed,
            "old_fingerprint": self.old_fingerprint,
            "new_fingerprint": self.new_fingerprint,
            "changed": list(self.changed),
            "old_params": {key: round(value, 2) for key, value in self.old_params.items()},
            "new_params": {key: round(value, 2) for key, value in self.new_params.items()},
        }


def reconcile(
    recorded_params: Mapping[str, Any] | None,
    current_params: Mapping[str, Any],
    *,
    confirmed: bool = False,
) -> MigrationCheck:
    """把落盘参数与当前参数核对一遍。

    没有落盘参数（首次投运）视为待核对，不放行；一致放行；漂移则必须经一次
    :meth:`MigrationCheck.confirm` 重新确认才放行。
    """

    current_normalised = normalise_params(current_params)
    if not recorded_params:
        return MigrationCheck(
            status=PENDING,
            old_fingerprint="",
            new_fingerprint=fingerprint(current_normalised),
            changed=(),
            old_params={},
            new_params=dict(current_normalised),
        )

    old_normalised = normalise_params(recorded_params)
    old_fp = fingerprint(old_normalised)
    new_fp = fingerprint(current_normalised)
    changed = tuple(
        key
        for key in _FINGERPRINT_KEYS
        if old_normalised[key] != current_normalised[key]
    )
    if not changed:
        status = MATCH
    elif confirmed:
        status = CONFIRMED
    else:
        status = DRIFTED
    return MigrationCheck(
        status=status,
        old_fingerprint=old_fp,
        new_fingerprint=new_fp,
        changed=changed,
        old_params=dict(old_normalised),
        new_params=dict(current_normalised),
        confirmed=confirmed and bool(changed),
    )
