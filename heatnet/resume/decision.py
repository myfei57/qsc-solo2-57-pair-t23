"""重启后的续作决议（按阶段判，定死）。

输入是三样只读事实：落盘进度（到了哪个阶段边界、阶段进行中还是已确认）、现场
参数迁移核对结论、以及设备当前态。输出是一条**不可变决议记录**，动作只可能是：

``finished``   链已完成，无需再动；
``resume``     从某阶段边界断言续做（幂等），已完成步骤一律跳过；
``redo``       阶段不可中途续，从头重做整条链；
``rollback``   阶段带物理副作用且处于半截，必须先退回安全态再从头来；
``blocked``    参数迁移核对未放行（没核对，或改过但没重新确认），禁止续作。

顺序固定：先看参数门，再看完成哨兵，最后按**阶段**（不是步骤）查表定动作。
决议记录同时是落盘、审计与控制台展示的同一份事实，保证三者一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .fingerprint import CONFIRMED, MATCH, MigrationCheck
from .stages import (
    BLOCKED,
    CYCLE,
    DONE,
    FINISHED,
    MAKEUP,
    QUALITY,
    QUANTITY,
    REDO,
    RESUME,
    ROLLBACK,
    WEATHER,
    describe_policies,
    next_stage,
    policy_of,
)


@dataclass(frozen=True)
class ResumeDecision:
    """一条续作决议（落盘／审计／控制台同源）。"""

    decision: str
    stage: str
    resume_from: str | None
    policy: str
    reason: str
    in_progress: bool
    migration: dict
    completed_steps: tuple[str, ...] = field(default_factory=tuple)

    @property
    def allowed(self) -> bool:
        """决议是否允许启动一次（续做或重做）投运推进。"""

        return self.decision in (RESUME, REDO, ROLLBACK, FINISHED)

    def require_allowed(self) -> None:
        """决议被参数门或现场态挡住时抛联锁异常。"""

        if not self.allowed:
            from ..errors import InterlockError

            raise InterlockError(f"续作被拒绝：{self.reason}", component="resume")

    def as_dict(self) -> dict:
        return {
            "decision": self.decision,
            "allowed": self.allowed,
            "stage": self.stage,
            "resume_from": self.resume_from,
            "policy": self.policy,
            "reason": self.reason,
            "in_progress": self.in_progress,
            "migration": self.migration,
            "completed_steps": list(self.completed_steps),
        }


def _blocked(progress: dict, migration: MigrationCheck, reason: str) -> ResumeDecision:
    return ResumeDecision(
        decision=BLOCKED,
        stage=str(progress.get("stage", "")),
        resume_from=None,
        policy="",
        reason=reason,
        in_progress=bool(progress.get("running", False)),
        migration=migration.as_dict(),
        completed_steps=tuple(progress.get("completed", [])),
    )


def decide_resume(progress: dict | None, migration: MigrationCheck) -> ResumeDecision:
    """按阶段给出重启后的续作决议。

    ``progress`` 为 :meth:`StartupProgress.load` 的结果（空映射表示全新投运）。
    """

    progress = progress or {}
    stage = str(progress.get("stage", ""))
    in_progress = bool(progress.get("running", False))
    completed = tuple(progress.get("completed", []))

    # 1) 没有可识别进度：全新投运，没有旧假设可核对，从气象开始。参数迁移门只在
    #    "存在历史进度、要接着做"时才生效，首次投运不被它挡住。
    if not progress:
        return ResumeDecision(
            decision=REDO,
            stage="",
            resume_from=WEATHER,
            policy="",
            reason="没有历史进度，按全新投运从气象推送开始",
            in_progress=False,
            migration=migration.as_dict(),
        )

    # 2) 参数迁移门：有进度要续，但参数没核对、或停机期间被改过又没重新确认，
    #    一律挡住，不许续也不许盲目重做。
    if not migration.passed:
        if migration.status == "drifted":
            reason = "停机期间参数被修改，迁移核对未重新确认"
        else:
            reason = "投运参数尚未迁移核对"
        return _blocked(progress, migration, reason)

    # 3) 链已完成。
    if stage == DONE:
        return ResumeDecision(
            decision=FINISHED,
            stage=DONE,
            resume_from=None,
            policy="",
            reason="投运链已完成，无需续作",
            in_progress=False,
            migration=migration.as_dict(),
            completed_steps=completed,
        )

    policy = policy_of(stage)

    # 4) 边界已确认（阶段不在进行中）：该阶段及之前都已做完，跳到下一阶段续。
    if not in_progress:
        nxt = next_stage(stage)
        if nxt == DONE:
            return ResumeDecision(
                decision=FINISHED,
                stage=stage,
                resume_from=None,
                policy=policy,
                reason="最后一个阶段边界已确认，投运已完成",
                in_progress=False,
                migration=migration.as_dict(),
                completed_steps=completed,
            )
        return ResumeDecision(
            decision=RESUME,
            stage=stage,
            resume_from=nxt,
            policy=policy,
            reason=f"阶段 {stage} 已完整做完，从下一阶段 {nxt} 续做（已完成步骤跳过）",
            in_progress=False,
            migration=migration.as_dict(),
            completed_steps=completed,
        )

    # 5) 阶段进行中掉电：按该阶段固定策略处置。
    if policy == RESUME:
        return ResumeDecision(
            decision=RESUME,
            stage=stage,
            resume_from=stage,
            policy=policy,
            reason=f"阶段 {stage} 进行中断电，调量为幂等断言，从该阶段边界续做",
            in_progress=True,
            migration=migration.as_dict(),
            completed_steps=completed,
        )
    if policy == ROLLBACK:
        return ResumeDecision(
            decision=ROLLBACK,
            stage=stage,
            resume_from=WEATHER,
            policy=policy,
            reason=f"阶段 {stage} 进行中断电且带物理副作用，先退回安全态再从气象重做",
            in_progress=True,
            migration=migration.as_dict(),
            completed_steps=completed,
        )
    return ResumeDecision(
        decision=REDO,
        stage=stage,
        resume_from=WEATHER,
        policy=policy,
        reason=f"阶段 {stage} 进行中断电且不可中途续，从气象推送重做整条链",
        in_progress=True,
        migration=migration.as_dict(),
        completed_steps=completed,
    )


def policy_overview() -> dict[str, Any]:
    """返回阶段策略口径，供控制台展示"按阶段判"这一固定规则。"""

    return {
        "granularity": "stage",
        "note": "续作决议按阶段判，不按步骤判；步骤只用于幂等去重与进度展示",
        "order": [WEATHER, QUALITY, QUANTITY, MAKEUP, CYCLE, DONE],
        "decisions": [RESUME, REDO, ROLLBACK, BLOCKED, FINISHED],
        "migration_gate": {
            "statuses": ["pending", MATCH, "drifted", CONFIRMED],
            "note": "参数核对属于迁移，未核对或漂移未确认时禁止续作",
        },
        "stages": describe_policies(),
    }
