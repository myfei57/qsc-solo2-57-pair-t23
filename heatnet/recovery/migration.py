"""续作前的参数核对（迁移）。

"停机这段时间有人改过参数"在这里被当成一次**迁移**：投运链建立在一组当时生效的
假设上（曲线输入、联锁门限、现场实测状态），重启后续做必须用当前值重新核对一遍，
核对不过就不许续。核对永远是现算的，不复用任何"上次已核对"标志——这正是迁移
检查的定义：换了环境就要重跑。

核对三类假设：

- ``input``     投运输入（室外温度、负荷需求）是否还是落盘时的那一组；
- ``config``    生效配置里参与联锁的门限是否被改动；
- ``field``     已完成阶段对应的现场状态是否仍成立（设备可能被人手动拨过）。

任一类不符都会进 ``mismatches``，由决议器据此判退回。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from ..config import PlatformConfig


@dataclass(frozen=True)
class MigrationCheck:
    """一次参数核对的结果（迁移报告）。"""

    ok: bool
    current: dict
    recorded: dict
    mismatches: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "kind": "migration",
            "mismatches": list(self.mismatches),
            "current": self.current,
            "recorded": self.recorded,
        }


def _digest(payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def input_fingerprint(outdoor_c: float, demand_pct: float) -> dict:
    """投运输入指纹：室外温度与负荷需求。"""

    payload = {"outdoor_c": round(float(outdoor_c), 4), "demand_pct": round(float(demand_pct), 4)}
    return {"digest": _digest(payload), "values": payload}


def config_fingerprint(config: PlatformConfig) -> dict:
    """参与投运联锁的配置门限指纹。

    只纳入投运链真正依赖的门限；无关字段（存储目录等）变化不该误判迁移失败。
    """

    limits: dict[str, dict[str, float]] = {
        "source": {
            "supply_target_c": config.source.supply_target_c,
            "supply_max_c": config.source.supply_max_c,
            "return_min_c": config.source.return_min_c,
            "flow_min_t_h": config.source.flow_min_t_h,
            "flow_max_t_h": config.source.flow_max_t_h,
            "quality_confirm_s": config.source.quality_confirm_s,
            "adjust_rate_c_s": config.source.adjust_rate_c_s,
        },
        "curve": {
            "outdoor_min_c": config.curve.outdoor_min_c,
            "outdoor_max_c": config.curve.outdoor_max_c,
            "stale_after_s": config.curve.stale_after_s,
        },
        "primary": {
            "flow_baseline_t_h": config.primary.flow_baseline_t_h,
            "flow_tolerance_t_h": config.primary.flow_tolerance_t_h,
        },
        "secondary": {
            "makeup_target_kpa": config.secondary.makeup_target_kpa,
            "makeup_min_kpa": config.secondary.makeup_min_kpa,
            "makeup_max_kpa": config.secondary.makeup_max_kpa,
        },
        "pump": {
            "ramp_t_h_s": config.pump.ramp_t_h_s,
            "min_speed_pct": config.pump.min_speed_pct,
            "max_speed_pct": config.pump.max_speed_pct,
        },
    }
    return {"digest": _digest(limits), "values": limits}


def migration_fingerprint(
    config: PlatformConfig, outdoor_c: float, demand_pct: float
) -> dict:
    """投运开始/每个阶段提交时记录的迁移指纹。"""

    return {
        "input": input_fingerprint(outdoor_c, demand_pct)["digest"],
        "config": config_fingerprint(config)["digest"],
        "input_values": input_fingerprint(outdoor_c, demand_pct)["values"],
    }


def verify_migration(
    config: PlatformConfig,
    recorded: dict,
    *,
    outdoor_c: float,
    demand_pct: float,
    field_state: dict,
    completed_stages: list[str],
) -> MigrationCheck:
    """用当前参数重新核对落盘假设。迁移检查：每次重启必跑，不可复用旧结论。"""

    mismatches: list[str] = []
    current_input = input_fingerprint(outdoor_c, demand_pct)
    current_config = config_fingerprint(config)

    recorded_input = str(recorded.get("input", ""))
    recorded_config = str(recorded.get("config", ""))
    if recorded_input and recorded_input != current_input["digest"]:
        old = dict(recorded.get("input_values", {}))
        mismatches.append(
            f"投运输入变化: 记录 {old} → 当前 {current_input['values']}"
        )
    if recorded_config and recorded_config != current_config["digest"]:
        mismatches.append("联锁门限配置被改动")

    field_issues = _verify_field(field_state, completed_stages)
    mismatches.extend(field_issues)

    current = {
        "input": current_input["digest"],
        "config": current_config["digest"],
        "input_values": current_input["values"],
    }
    return MigrationCheck(not mismatches, current, dict(recorded), mismatches)


def _verify_field(field_state: dict, completed_stages: list[str]) -> list[str]:
    """核对已完成阶段对应的现场状态是否仍成立。"""

    issues: list[str] = []
    order = {stage: idx for idx, stage in enumerate(
        ("weather", "quality", "quantity", "primary", "makeup", "cycle")
    )}
    reached = max((order[s] for s in completed_stages if s in order), default=-1)
    if reached < 0:
        return issues

    source_state = str(field_state.get("source_state", ""))
    if source_state == "tripped":
        issues.append("热源现场为停机态，与已完成阶段不符")
    if reached >= order["quality"]:
        # 调质确认要求现场仍保持在确认温度（容差内）；被人拧过会在这里暴露。
        target = field_state.get("source_target_c")
        actual = field_state.get("source_temperature_c")
        if isinstance(target, (int, float)) and isinstance(actual, (int, float)):
            if abs(float(actual) - float(target)) > max(1.0, 0.02 * abs(float(target))):
                issues.append(
                    f"热源供温现场 {float(actual):.1f}℃ 偏离确认目标 {float(target):.1f}℃"
                )
    if reached >= order["quantity"]:
        flow = field_state.get("source_flow_t_h")
        if isinstance(flow, (int, float)) and float(flow) <= 0.0:
            issues.append("热源流量现场为零，与调量已完成不符")
    if reached >= order["makeup"]:
        pressure = field_state.get("pressure_kpa")
        low = field_state.get("makeup_min_kpa")
        high = field_state.get("makeup_max_kpa")
        if isinstance(pressure, (int, float)):
            if isinstance(low, (int, float)) and float(pressure) < float(low):
                issues.append(f"二次网定压 {float(pressure):.0f}kPa 跌破补水门限")
            if isinstance(high, (int, float)) and float(pressure) > float(high):
                issues.append(f"二次网定压 {float(pressure):.0f}kPa 越上限")
    return issues
