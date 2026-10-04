"""续作协调器：把进度、参数迁移核对、决议与安全退回串成一件事。

它是控制台、命令行与编排之间唯一的续作入口，保证"续作决议、现场状态、审计"
三处口径一致：

1. 重启后先 :meth:`resume_decision`——读落盘进度、把当前参数和落盘指纹核对一遍
   （参数核对属于迁移），产出一条不可变决议并**落审计**；决议同时供控制台展示。
2. 决议被参数门挡住（``blocked``）时，现场改完／确认完参数调
   :meth:`confirm_params` 重新核对，核对通过才放行；绝不静默沿用旧指纹。
3. 决议为 ``rollback`` 时先 :meth:`rollback_to_safe` 把现场退回安全态（泵停稳、
   阀关、撤流量、热源回到待投）并清进度，再按全新投运从气象重做。

协调器只判定与落账，不直接跑投运动作；动作仍由 :func:`startup_plant` 在持有
进度句柄时推进并逐阶段落边界。
"""

from __future__ import annotations

from ..clock import Clock
from ..eventbus import Event
from ..primary.network import PrimaryNetwork
from ..pump.pump import Pump
from ..pump.start import stop_pump_before_valve
from ..source.source import HeatSource
from ..valve.valve import Valve
from .decision import ResumeDecision, decide_resume, policy_overview
from .fingerprint import MigrationCheck, reconcile
from .store import StartupProgress

DEFAULT_CHAIN = "primary"


class ResumeManager:
    """投运链的进度、迁移核对与续作决议。

    审计不直接依赖：核对与决议都发布到事件总线，由全量订阅的审计流水统一落盘，
    保证决议、现场状态、审计三处同源。
    """

    def __init__(
        self,
        progress: StartupProgress,
        clock: Clock,
        bus,
        chain: str = DEFAULT_CHAIN,
    ) -> None:
        self._progress = progress
        self._clock = clock
        self._bus = bus
        self._chain = chain
        # 最近一次决议按链缓存，控制台展示与落审计读同一条。
        self._last: ResumeDecision | None = None

    @property
    def chain(self) -> str:
        return self._chain

    # ---- 进度 ---------------------------------------------------------

    def progress(self) -> dict:
        """读取当前落盘进度。"""

        return self._progress.load(self._chain)

    def save_progress(self, payload: dict) -> str:
        """落盘一条进度边界（供编排逐阶段调用）。"""

        return self._progress.save(self._chain, payload)

    def clear_progress(self) -> bool:
        """清除进度（退回全新投运）。"""

        return self._progress.reset(self._chain)

    # ---- 迁移核对 -----------------------------------------------------

    def reconcile(self, current_params, *, confirmed: bool = False) -> MigrationCheck:
        """把当前参数与落盘参数核对一遍（不改变进度）。"""

        recorded = self.progress().get("params") or {}
        return reconcile(recorded, current_params, confirmed=confirmed)

    def confirm_params(self, current_params) -> dict:
        """现场按当前参数重新确认迁移核对，并以新参数回写进度。

        只有存在历史进度、且确实发生漂移时才需要确认：确认后以当前参数为准回写，
        后续续作读到的就是新指纹，旧假设不再残留。参数未漂移时直接返回一致结论。
        没有历史进度（首次投运）时没有旧假设可迁移，返回待核对结论、不落任何进度。
        """

        check = self.reconcile(current_params)
        if check.status == "pending":
            self._emit_migration(check)
            return check.as_dict()
        if check.status == "drifted":
            check = check.confirm()
            snapshot = self.progress()
            self._progress.save(
                self._chain,
                {
                    "chain": snapshot.get("chain", self._chain),
                    "op_id": snapshot.get("op_id", ""),
                    "stage": snapshot.get("stage"),
                    "running": snapshot.get("running", False),
                    "completed": list(snapshot.get("completed", [])),
                    "params": dict(check.new_params),
                    "updated_at": self._clock.now(),
                },
            )
        self._emit_migration(check)
        return check.as_dict()

    # ---- 决议 ---------------------------------------------------------

    def resume_decision(self, current_params) -> ResumeDecision:
        """产出重启后的续作决议，并落审计、供控制台展示。"""

        check = self.reconcile(current_params)
        decision = decide_resume(self.progress(), check)
        self._last = decision
        self._bus.publish(
            Event.of(
                "resume",
                "resume",
                "resume_decision",
                severity="notice" if decision.allowed else "warning",
                message=f"投运续作决议：{decision.decision}",
                chain=self._chain,
                **decision.as_dict(),
            )
        )
        return decision

    def last_decision(self) -> dict | None:
        """返回最近一次决议（控制台展示），没有则返回 ``None``。"""

        return self._last.as_dict() if self._last is not None else None

    def overview(self) -> dict:
        """返回"按阶段判"的固定口径与各阶段策略。"""

        return policy_overview()

    # ---- 安全退回 -----------------------------------------------------

    def rollback_to_safe(
        self,
        source: HeatSource,
        network: PrimaryNetwork,
        pump: Pump,
        valve: Valve,
        reason: str = "resume-rollback",
        dt: float = 1.0,
    ) -> dict:
        """把现场退回安全态并清除进度，供 ``rollback`` 决议后重做。

        退回顺序与停运一致：先停泵等流量归零，再关阀、撤一次网流量；热源若不在
        待投态，按"停机—复位"回到冷态。
        """

        stopped = stop_pump_before_valve(pump, valve, dt)
        network.set_flow(0.0)
        if source.state() != "idle":
            source.trip(reason)
            source.reset()
        removed = self.clear_progress()
        report = {
            "ok": True,
            "reason": reason,
            "pump": stopped,
            "flow_t_h": round(network.flow_t_h(), 2),
            "source_state": source.state(),
            "progress_cleared": removed,
        }
        self._bus.publish(
            Event.of(
                "resume",
                "resume",
                "rollback_safe",
                severity="warning",
                message="续作退回安全态，进度作废，等待从气象重做",
                chain=self._chain,
                **report,
            )
        )
        return report

    # ---- 内部 ---------------------------------------------------------

    def _emit_migration(self, check: MigrationCheck) -> None:
        self._bus.publish(
            Event.of(
                "resume",
                "resume",
                "param_reconcile",
                severity="notice" if check.passed else "alarm",
                message="投运参数迁移核对" if check.passed else "投运参数迁移核对未通过",
                chain=self._chain,
                **check.as_dict(),
            )
        )
