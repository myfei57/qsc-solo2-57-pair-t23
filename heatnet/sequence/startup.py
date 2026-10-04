"""投运编排（带续作进度）。

气象推送必须先更新曲线，热源再按新曲线调质；调质确认之后才允许调量，调量完成
一次网才跟随；随后按"先补后定"把压力拉回目标带，最后跑一个循环建立流量。任何
一步失败都停在原地，不带着半截状态继续往下走。

链按**阶段**落进度（weather → quality → quantity → makeup → cycle → done）。
每个阶段开始前先把进度置成"该阶段进行中"，阶段成功后落"阶段边界已确认 + 已完成
步骤"。重启后：

* 已经确认的阶段**整段跳过**，并只重建进入后续阶段所需的内存前提，物理动作不做
  第二遍；
* 阶段内的步骤按键幂等去重，已完成的步骤不会再执行；
* 从哪个阶段起步由 :mod:`heatnet.resume` 的续作决议给定（``resume_from``），
  编排本身不重新裁决，保证"决议、现场、审计"口径一致。
"""

from __future__ import annotations

from ..errors import PlantError
from ..primary.network import PrimaryNetwork
from ..pump.pump import Pump
from ..secondary.cycle import run_cycle
from ..secondary.loop import SecondaryLoop
from ..secondary.makeup import makeup_then_pressurise
from ..source.source import HeatSource
from ..source.weather import WeatherPush, push_weather
from ..store.state import DurableState
from ..temp.curve import HeatCurve
from ..valve.valve import Valve
from ..resume.stages import (
    CYCLE,
    DONE,
    MAKEUP,
    QUALITY,
    QUANTITY,
    STAGE_ORDER,
    WEATHER,
)

MAX_STEPS = 1200


class StartupRunner:
    """按续作决议推进投运链，并逐阶段落进度。

    ``resume_from`` 为起步阶段；``completed`` 为决议认定已完成的步骤键集合。
    ``progress`` 是 :class:`~heatnet.resume.store.StartupProgress`，``chain``
    与 ``record_step`` 由平台装配时提供；不给进度句柄时退化成一次性投运（不落
    续作进度），供不关心续作的调用点使用。
    """

    def __init__(
        self,
        source: HeatSource,
        curve: HeatCurve,
        network: PrimaryNetwork,
        loop: SecondaryLoop,
        pump: Pump,
        valve: Valve,
        durable: DurableState,
        outdoor_c: float,
        now: float,
        demand_pct: float = 60.0,
        dt: float = 1.0,
        *,
        resume_from: str = WEATHER,
        completed: set[str] | None = None,
        progress=None,
        chain: str = "primary",
        op_id: str = "",
        params: dict | None = None,
        clock_now=None,
    ) -> None:
        self.source = source
        self.curve = curve
        self.network = network
        self.loop = loop
        self.pump = pump
        self.valve = valve
        self.durable = durable
        self.outdoor_c = outdoor_c
        self.now = now
        self.demand_pct = demand_pct
        self.dt = dt
        self.resume_from = resume_from
        self.done_steps: set[str] = set(completed or ())
        self.progress = progress
        self.chain = chain
        self.op_id = op_id
        self.params = params or {}
        self._clock_now = clock_now

        self.steps: list[dict] = []
        self.violations: list[str] = []
        # 本次实际执行（非跳过）的步骤键，按顺序。
        self.executed: list[str] = []

    # ---- 进度落盘 -----------------------------------------------------

    def _persist(self, stage: str, running: bool) -> None:
        if self.progress is None:
            return
        self.progress.save(
            self.chain,
            {
                "chain": self.chain,
                "op_id": self.op_id,
                "stage": stage,
                "running": running,
                "completed": sorted(self.done_steps),
                "params": dict(self.params),
                "updated_at": (self._clock_now() if self._clock_now else self.now),
            },
        )

    def _skip(self, key: str, component: str, action: str, detail: dict | None = None) -> None:
        """登记一个被幂等跳过的已完成步骤（只记录，不执行物理动作）。"""

        self.done_steps.add(key)
        self.steps.append(
            {
                "component": component,
                "action": action,
                "detail": {"skipped": "already_completed", **(detail or {})},
            }
        )

    def _do(self, key: str, stage: str, fn) -> dict:
        """执行一个阶段。

        进入先落"阶段进行中"。**只有阶段成功**才把它加进已完成集合并落"边界
        已确认（running=False）"；一旦本阶段产生违例，保持"阶段进行中"并停手——
        这样重启读到的是"该阶段半截中断"，会按阶段策略重做/退回，绝不会被误当成
        已确认边界而跳到下一阶段。
        """

        self._persist(stage, running=True)
        result = fn()
        self.steps.extend(result.get("steps", []))
        self.executed.append(key)
        stage_violations = list(result.get("violations", []))
        self.violations.extend(stage_violations)
        if stage_violations:
            self._persist(stage, running=True)
        else:
            self.done_steps.add(key)
            self._persist(stage, running=False)
        return result

    # ---- 各阶段 -------------------------------------------------------

    def _restore_before(self, stage: str) -> None:
        """重建"已确认阶段之后"进入 ``stage`` 所需的内存前提，不重复物理动作。"""

        order_index = STAGE_ORDER.index
        if order_index(stage) > order_index(WEATHER):
            # 气象已确认：曲线与点火温度必须在内存里，否则调质没有目标。
            if self.curve.epoch() == 0:
                forecast = self.curve.apply_forecast(self.outdoor_c, self.now)
                self.source.fire(forecast["supply_c"])
        if order_index(stage) > order_index(QUALITY):
            # 调质已确认：重建确认态，供调量联锁放行。
            if not self.source.quality_confirmed():
                self.source.restore_quality_confirmed(self.curve.supply_c())
        if order_index(stage) > order_index(QUANTITY):
            # 调量已确认：把一次网跟随到既设流量，纯断言、无第二遍副作用。
            flow = self.network.baseline_t_h() * max(0.0, min(1.0, self.demand_pct / 100.0))
            self.network.set_supply(self.source.temperature_c())
            self.network.set_return(self.loop.return_c())
            self.network.set_flow(flow)
        if order_index(stage) > order_index(MAKEUP):
            # 补水已确认：压力已在目标带，无需恢复动作。
            pass

    def _stage_weather(self) -> dict:
        return push_weather(self.source, self.curve, WeatherPush(self.outdoor_c, self.now), self.now)

    def _stage_quality(self) -> dict:
        steps: list[dict] = []
        violations: list[str] = []
        for _ in range(MAX_STEPS):
            self.source.settle_quality(self.curve.supply_c(), self.dt)
            if self.source.quality_confirmed():
                break
        steps.append(
            {
                "component": "source",
                "action": "confirm_quality",
                "detail": {
                    "temperature_c": round(self.source.temperature_c(), 2),
                    "hold_s": round(self.source.quality_hold_s(), 3),
                    "confirmed": self.source.quality_confirmed(),
                },
            }
        )
        if not self.source.quality_confirmed():
            violations.append("quality_not_confirmed")
        return {"steps": steps, "violations": violations}

    def _stage_quantity(self) -> dict:
        steps: list[dict] = []
        flow = self.network.baseline_t_h() * max(0.0, min(1.0, self.demand_pct / 100.0))
        try:
            self.source.set_flow(flow)
        except PlantError as error:
            return {"steps": [], "violations": [error.message]}
        self.network.set_supply(self.source.temperature_c())
        self.network.set_return(self.loop.return_c())
        self.network.set_flow(self.source.flow_t_h())
        steps.append(
            {
                "component": "primary",
                "action": "follow_source",
                "detail": {
                    "flow_t_h": round(self.source.flow_t_h(), 2),
                    "supply_c": round(self.source.temperature_c(), 2),
                },
            }
        )
        return {"steps": steps, "violations": []}

    def _stage_makeup(self) -> dict:
        return makeup_then_pressurise(self.loop, self.dt)

    def _stage_cycle(self) -> dict:
        return run_cycle(self.loop, self.pump, self.valve, self.durable, self.dt)

    # ---- 主推进 -------------------------------------------------------

    def run(self) -> dict:
        """按起步阶段把链推进到完成或首个失败阶段。"""

        stages = (
            (WEATHER, "weather", self._stage_weather),
            (QUALITY, "quality", self._stage_quality),
            (QUANTITY, "quantity", self._stage_quantity),
            (MAKEUP, "makeup", self._stage_makeup),
            (CYCLE, "cycle", self._stage_cycle),
        )
        start_index = STAGE_ORDER.index(self.resume_from) if self.resume_from in STAGE_ORDER else 0
        if start_index > 0:
            self._restore_before(self.resume_from)

        failed_stage = ""
        for index, (stage, key, fn) in enumerate(stages):
            if index < start_index:
                # 已确认阶段：登记幂等跳过，物理动作不再执行。
                self._mark_skipped(stage)
                continue
            result = self._do(key, stage, fn)
            if self.violations:
                failed_stage = stage
                break

        if failed_stage:
            return {
                "ok": False,
                "steps": self.steps,
                "violations": self.violations,
                "stage": failed_stage,
                "resumed_from": self.resume_from,
                "executed": list(self.executed),
            }

        self._persist(DONE, running=False)
        return {
            "ok": True,
            "steps": self.steps,
            "violations": self.violations,
            "stage": DONE,
            "resumed_from": self.resume_from,
            "executed": list(self.executed),
            "flow_t_h": round(self.source.flow_t_h(), 2),
            "supply_c": round(self.source.temperature_c(), 2),
            "return_c": round(self.loop.return_c(), 2),
        }

    def _mark_skipped(self, stage: str) -> None:
        """把一个已确认阶段登记为跳过（保留进度展示，不产生第二遍副作用）。"""

        labels = {
            WEATHER: ("temp", "apply_forecast"),
            QUALITY: ("source", "confirm_quality"),
            QUANTITY: ("primary", "follow_source"),
            MAKEUP: ("secondary", "makeup"),
            CYCLE: ("pump", "start"),
        }
        component, action = labels[stage]
        self._skip(stage, component, action)


def startup_plant(
    source: HeatSource,
    curve: HeatCurve,
    network: PrimaryNetwork,
    loop: SecondaryLoop,
    pump: Pump,
    valve: Valve,
    durable: DurableState,
    outdoor_c: float,
    now: float,
    demand_pct: float = 60.0,
    dt: float = 1.0,
    *,
    resume_from: str = WEATHER,
    completed: set[str] | None = None,
    progress=None,
    chain: str = "primary",
    op_id: str = "",
    params: dict | None = None,
    clock_now=None,
) -> dict:
    """把整条调度链带到带负荷；带续作句柄时按决议起步并逐阶段落进度。"""

    runner = StartupRunner(
        source,
        curve,
        network,
        loop,
        pump,
        valve,
        durable,
        outdoor_c,
        now,
        demand_pct,
        dt,
        resume_from=resume_from,
        completed=completed,
        progress=progress,
        chain=chain,
        op_id=op_id,
        params=params,
        clock_now=clock_now,
    )
    return runner.run()
