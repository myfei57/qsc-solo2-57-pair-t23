"""可续作的投运执行器。

执行器把 :mod:`heatnet.sequence.startup` 的同一条调用链拆到
:data:`heatnet.recovery.decision.STAGE_ORDER` 的六个阶段里，在阶段边界与步骤
边界落盘，并遵循以下不变量：

- 续作/退回只认 :func:`decide_resume` 给出的、已落盘的决议，执行器自己不另判；
- 已落盘完成的步骤在同一次续做里**跳过**，绝不再做第二遍；阶段按原顺序推进，
  不跳阶段、不乱序；
- 退回区间的阶段进度先清空再整体重跑（幂等动作允许安全重放）；
- 参数核对（迁移）在任何设备动作之前完成，核对不过决议为 rollback/abort，
  abort 时执行器不动设备。

测试与掉电演练通过 ``halt`` 钩子在"步骤已落盘、下一步未开始"处中断，模拟停电；
生产路径不传该钩子。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..errors import PlantError
from ..primary.network import PrimaryNetwork
from ..pump.pump import Pump
from ..secondary.loop import SecondaryLoop
from ..secondary.makeup import makeup_then_pressurise
from ..secondary.cycle import run_cycle
from ..source.source import HeatSource
from ..source.weather import WeatherPush, push_weather
from ..store.state import DurableState
from ..temp.curve import HeatCurve
from ..valve.valve import Valve
from .decision import (
    MAKEUP,
    PRIMARY,
    QUALITY,
    QUANTITY,
    STAGE_ORDER,
    WEATHER,
    CYCLE,
    ResumeDecision,
    decide_resume,
)
from .migration import migration_fingerprint, verify_migration
from .progress import StartupProgress

MAX_SETTLE_STEPS = 1200

HaltHook = Callable[[str, str], None]


@dataclass
class StartupContext:
    """执行一次投运所需的设备与环境。"""

    source: HeatSource
    curve: HeatCurve
    network: PrimaryNetwork
    loop: SecondaryLoop
    pump: Pump
    valve: Valve
    durable: DurableState
    config: object
    now: float
    outdoor_c: float
    demand_pct: float
    dt: float = 1.0


def field_snapshot(ctx: StartupContext) -> dict:
    """读出供迁移核对与重启重建使用的现场状态。"""

    return {
        "source_state": ctx.source.state(),
        "source_temperature_c": round(ctx.source.temperature_c(), 3),
        "source_target_c": round(ctx.source.target_c(), 3),
        "source_flow_t_h": round(ctx.source.flow_t_h(), 3),
        "source_hold_s": round(ctx.source.quality_hold_s(), 3),
        "curve_outdoor_c": round(ctx.curve.outdoor_c(), 3),
        "curve_supply_c": round(ctx.curve.supply_c(), 3),
        "curve_epoch": ctx.curve.epoch(),
        "curve_updated_at": round(ctx.curve.updated_at(), 3),
        "network_supply_c": round(ctx.network.supply_c(), 3),
        "network_return_c": round(ctx.network.return_c(), 3),
        "network_flow_t_h": round(ctx.network.flow_t_h(), 3),
        "loop_supply_c": round(ctx.loop.supply_c(), 3),
        "loop_return_c": round(ctx.loop.return_c(), 3),
        "pressure_kpa": round(ctx.loop.pressure_kpa(), 3),
        "makeup_min_kpa": ctx.loop.limits.makeup_min_kpa,
        "makeup_max_kpa": ctx.loop.limits.makeup_max_kpa,
        "pump_running": ctx.pump.running(),
    }


def plan_resume(
    ctx: StartupContext,
    progress_store: StartupProgress,
    *,
    use_recorded_input: bool = True,
) -> ResumeDecision:
    """重启后只做判定不动设备：迁移核对 → 按阶段出续作决议。

    默认沿用落盘的投运输入做核对（现场操作员只是"继续"，不该因为没重复输入而误判
    迁移）；需要用一组新输入核对时传 ``use_recorded_input=False``。
    """

    progress = progress_store.load()
    if not progress:
        recorded: dict = {}
        completed: list[str] = []
    else:
        recorded = dict(progress.get("fingerprint", {}))
        completed = [str(s) for s in progress.get("completed_stages", [])]
        if use_recorded_input:
            params = dict(progress.get("params", {}))
            ctx.outdoor_c = float(params.get("outdoor_c", ctx.outdoor_c))
            ctx.demand_pct = float(params.get("demand_pct", ctx.demand_pct))

    # 现场只读一次：迁移核对与决议里附带的现场快照必须是同一份，
    # 否则两次读取之间设备变化会让"核对依据"和"展示现场"对不上。
    field_now = field_snapshot(ctx)
    migration = verify_migration(
        ctx.config,
        recorded,
        outdoor_c=ctx.outdoor_c,
        demand_pct=ctx.demand_pct,
        field_state=field_now,
        completed_stages=completed,
    )
    return decide_resume(progress, migration.as_dict(), field_now)


def run_startup(
    ctx: StartupContext,
    progress_store: StartupProgress,
    decision: ResumeDecision,
    halt: HaltHook | None = None,
) -> dict:
    """按已出的决议推进投运，返回与原 startup_plant 同口径的报告。

    决议为 abort 时拒绝动设备；rollback 先清退回区间；随后从目标阶段按序执行。
    """

    if not decision.can_run():
        raise PlantError(f"续作被禁止：{decision.reason}", component="recovery")

    steps: list[dict] = []
    violations: list[str] = []
    fingerprint = migration_fingerprint(ctx.config, ctx.outdoor_c, ctx.demand_pct)

    if not progress_store.load():
        progress_store.begin(
            {"outdoor_c": ctx.outdoor_c, "demand_pct": ctx.demand_pct}, fingerprint, ctx.now
        )
    elif decision.decision == "rollback":
        progress_store.rollback_to(decision.target_stage, fingerprint, ctx.now)

    start_index = STAGE_ORDER.index(decision.target_stage)
    failure_stage = ""
    for stage in STAGE_ORDER[start_index:]:
        done = set(progress_store.done_steps(stage))
        report = _run_stage(ctx, progress_store, stage, done, fingerprint, halt)
        steps.extend(report["steps"])
        violations.extend(report["violations"])
        if not report["ok"]:
            failure_stage = stage
            break

    current = progress_store.load().get("current_stage", "")
    return {
        "ok": not violations and not failure_stage,
        "steps": steps,
        "violations": violations,
        "stage": "done" if not failure_stage else failure_stage,
        "resumed": decision.decision,
        "decision": decision.as_dict(),
        "current_stage": current,
        "flow_t_h": round(ctx.source.flow_t_h(), 2),
        "supply_c": round(ctx.source.temperature_c(), 2),
        "return_c": round(ctx.loop.return_c(), 2),
    }


def _run_stage(
    ctx: StartupContext,
    store: StartupProgress,
    stage: str,
    done: set[str],
    fingerprint: dict,
    halt: HaltHook | None,
) -> dict:
    if stage == WEATHER:
        return _stage_weather(ctx, store, done, fingerprint, halt)
    if stage == QUALITY:
        return _stage_quality(ctx, store, done, fingerprint, halt)
    if stage == QUANTITY:
        return _stage_quantity(ctx, store, done, fingerprint, halt)
    if stage == PRIMARY:
        return _stage_primary(ctx, store, done, fingerprint, halt)
    if stage == MAKEUP:
        return _stage_makeup(ctx, store, done, fingerprint, halt)
    return _stage_cycle(ctx, store, done, fingerprint, halt)


def _already(done: set[str], step: str) -> bool:
    """该步骤是否在同一次续做里已经完成（完成即不许再做第二遍）。"""

    return step in done


def _mark(
    store: StartupProgress,
    stage: str,
    step: str,
    done: set[str],
    halt: HaltHook | None,
    ctx: StartupContext,
) -> None:
    """落盘一个已完成步骤并按需在断点处中断。调用前必须已确认本步骤未做过。"""

    store.mark_step(stage, step, field_snapshot(ctx))
    done.add(step)
    if halt is not None:
        halt(stage, step)


def _stage_weather(ctx, store, done, fingerprint, halt) -> dict:
    steps, violations = [], []
    if not (_already(done, "apply_forecast") and _already(done, "fire_forecast")):
        # 气象推送内部"先曲线后点火"是一个整体，两个步骤在同一原子推送里完成；
        # 退回重做时一起重放（均为幂等设定），续做时只要有任一标记缺失就重推一次。
        weather = push_weather(ctx.source, ctx.curve, WeatherPush(ctx.outdoor_c, ctx.now), ctx.now)
        steps.extend(weather["steps"])
        if not weather["ok"]:
            violations.extend(weather["violations"])
            return {"ok": False, "steps": steps, "violations": violations}
        for step in ("apply_forecast", "fire_forecast"):
            _mark(store, WEATHER, step, done, halt, ctx)
    store.commit_stage(WEATHER, fingerprint, ctx.now, field_snapshot(ctx))
    return {"ok": True, "steps": steps, "violations": violations}


def _stage_quality(ctx, store, done, fingerprint, halt) -> dict:
    steps, violations = [], []
    if not _already(done, "confirm_quality"):
        for _ in range(MAX_SETTLE_STEPS):
            ctx.source.settle_quality(ctx.curve.supply_c(), ctx.dt)
            if ctx.source.quality_confirmed():
                break
        _mark(store, QUALITY, "confirm_quality", done, halt, ctx)
        steps.append(
            {
                "component": "source",
                "action": "confirm_quality",
                "detail": {
                    "temperature_c": round(ctx.source.temperature_c(), 2),
                    "hold_s": round(ctx.source.quality_hold_s(), 3),
                    "confirmed": ctx.source.quality_confirmed(),
                },
            }
        )
    if not ctx.source.quality_confirmed():
        violations.append("quality_not_confirmed")
        return {"ok": False, "steps": steps, "violations": violations}
    store.commit_stage(QUALITY, fingerprint, ctx.now, field_snapshot(ctx))
    return {"ok": True, "steps": steps, "violations": violations}


def _stage_quantity(ctx, store, done, fingerprint, halt) -> dict:
    steps, violations = [], []
    if not _already(done, "set_flow"):
        flow = ctx.network.baseline_t_h() * max(0.0, min(1.0, ctx.demand_pct / 100.0))
        try:
            ctx.source.set_flow(flow)
        except PlantError as error:
            violations.append(error.message)
            return {"ok": False, "steps": steps, "violations": violations}
        _mark(store, QUANTITY, "set_flow", done, halt, ctx)
        steps.append(
            {
                "component": "source",
                "action": "set_flow",
                "detail": {"flow_t_h": round(ctx.source.flow_t_h(), 2)},
            }
        )
    store.commit_stage(QUANTITY, fingerprint, ctx.now, field_snapshot(ctx))
    return {"ok": True, "steps": steps, "violations": violations}


def _stage_primary(ctx, store, done, fingerprint, halt) -> dict:
    steps, violations = [], []
    if not _already(done, "follow_source"):
        ctx.network.set_supply(ctx.source.temperature_c())
        ctx.network.set_return(ctx.loop.return_c())
        ctx.network.set_flow(ctx.source.flow_t_h())
        _mark(store, PRIMARY, "follow_source", done, halt, ctx)
        steps.append(
            {
                "component": "primary",
                "action": "follow_source",
                "detail": {
                    "flow_t_h": round(ctx.source.flow_t_h(), 2),
                    "supply_c": round(ctx.source.temperature_c(), 2),
                },
            }
        )
    store.commit_stage(PRIMARY, fingerprint, ctx.now, field_snapshot(ctx))
    return {"ok": True, "steps": steps, "violations": violations}


def _stage_makeup(ctx, store, done, fingerprint, halt) -> dict:
    steps, violations = [], []
    if not _already(done, "makeup"):
        makeup = makeup_then_pressurise(ctx.loop, ctx.dt)
        steps.extend(makeup["steps"])
        if not makeup["ok"]:
            violations.extend(makeup["violations"])
            return {"ok": False, "steps": steps, "violations": violations}
        _mark(store, MAKEUP, "makeup", done, halt, ctx)
    store.commit_stage(MAKEUP, fingerprint, ctx.now, field_snapshot(ctx))
    return {"ok": True, "steps": steps, "violations": violations}


def _stage_cycle(ctx, store, done, fingerprint, halt) -> dict:
    steps, violations = [], []
    if not _already(done, "circulate"):
        cycle = run_cycle(ctx.loop, ctx.pump, ctx.valve, ctx.durable, ctx.dt)
        steps.extend(cycle["steps"])
        if not cycle["ok"]:
            violations.extend(cycle["violations"])
            return {"ok": False, "steps": steps, "violations": violations}
        _mark(store, CYCLE, "circulate", done, halt, ctx)
    store.commit_stage(CYCLE, fingerprint, ctx.now, field_snapshot(ctx))
    return {"ok": True, "steps": steps, "violations": violations}
