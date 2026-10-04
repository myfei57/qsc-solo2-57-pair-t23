"""投运阶段与续作决议。

阶段是投运链的恢复单元，顺序在 :data:`STAGE_ORDER` 里写死，与
:mod:`heatnet.sequence.startup` 的真实调用链一一对应：

    weather   气象推送 → 曲线更新 → 热源点火
    quality   调质到位并确认（确认计时不可跨重启累积）
    quantity  调量（热源设定流量）
    primary   一次网跟随热源
    makeup    二次网补水定压（先补后定）
    cycle     启泵建立循环流量，结束后泵停稳

每个阶段带一个不可更改的恢复策略 :data:`STAGE_POLICY`——"重启后接着做还是退回
去"**按阶段判**，在这里定死，运行期不得临时改变：

- ``rerun``：该阶段没有可安全续接的中间态（确认计时/循环会因停机而作废），
  重启后本阶段整体退回重做；
- ``resume``：阶段内动作均为幂等的设定/跟随，重启后跳过已完成步骤，从第一个
  未完成步骤按原顺序续做。

阶段内步骤只用于幂等去重，不参与"续不续"的判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 续作决议的四种结果（决议落盘、供控制台展示并写审计，口径一致）。
DECISION_FRESH = "fresh"        # 无在途投运：从头开始
DECISION_RESUME = "resume"      # 迁移核对通过：从目标阶段续做
DECISION_ROLLBACK = "rollback"  # 迁移核对不过或阶段不可续：退回目标阶段重做
DECISION_ABORT = "abort"        # 现场状态与落盘进度无法调和，禁止自动续/退，交人工

WEATHER = "weather"
QUALITY = "quality"
QUANTITY = "quantity"
PRIMARY = "primary"
MAKEUP = "makeup"
CYCLE = "cycle"
DONE = "done"

# 投运链阶段顺序（不含 done），任何代码都以这份顺序为准。
STAGE_ORDER: tuple[str, ...] = (WEATHER, QUALITY, QUANTITY, PRIMARY, MAKEUP, CYCLE)

# 阶段内步骤：键为阶段，值为该阶段按序执行的步骤标识。
# 步骤标识同时是幂等去重键——已落盘完成的步骤重启后一律跳过。
STAGE_STEPS: dict[str, tuple[str, ...]] = {
    WEATHER: ("apply_forecast", "fire_forecast"),
    QUALITY: ("confirm_quality",),
    QUANTITY: ("set_flow",),
    PRIMARY: ("follow_source",),
    MAKEUP: ("makeup",),
    CYCLE: ("circulate",),
}

# 每个阶段的恢复策略，按阶段定死：rerun=整体重做，resume=幂等续做。
# weather/quality/cycle 中断后现场不再代表阶段目标（曲线可能已旧、确认计时随停机
# 清零、循环要求泵重新启停），只能整体退回；quantity/primary/makeup 都是幂等设定，
# 可以从断点续。
STAGE_POLICY: dict[str, str] = {
    WEATHER: "rerun",
    QUALITY: "rerun",
    QUANTITY: "resume",
    PRIMARY: "resume",
    MAKEUP: "resume",
    CYCLE: "rerun",
}

# rerun 阶段整体退回时，回退到的目标阶段（连同该阶段本身一起重做）。
# 与"参数被改过"的安全边界一致：调质/循环相关阶段退回气象，重新按曲线建立假设。
STAGE_RERUN_TARGET: dict[str, str] = {
    WEATHER: WEATHER,
    QUALITY: WEATHER,
    CYCLE: WEATHER,
}

# 决议常量到审计/展示文案的映射，避免各处自己拼字符串造成口径分叉。
DECISION_LABEL: dict[str, str] = {
    DECISION_FRESH: "从头投运",
    DECISION_RESUME: "续做",
    DECISION_ROLLBACK: "退回重做",
    DECISION_ABORT: "禁止自动续作，转人工",
}


def stage_index(stage: str) -> int:
    """返回阶段在链上的序号（``done`` 排在最后）。"""

    if stage == DONE:
        return len(STAGE_ORDER)
    return STAGE_ORDER.index(stage)


@dataclass(frozen=True)
class ResumeDecision:
    """一次重启后的续作决议。

    决议、现场状态与审计共用这一个结构：``progress`` 是落盘进度，``migration`` 是
    本次重新核对的结果，``field`` 是出决议时读到的现场快照。控制台展示、审计落盘、
    执行器读取三者必须来自同一份对象，不允许各算各的。
    """

    decision: str
    target_stage: str
    last_completed_stage: str
    interrupted_stage: str
    reason: str
    progress: dict = field(default_factory=dict)
    migration: dict = field(default_factory=dict)
    field: dict = field(default_factory=dict)

    def can_run(self) -> bool:
        """该决议是否允许执行器继续推进（abort 不许自动动设备）。"""

        return self.decision in (DECISION_FRESH, DECISION_RESUME, DECISION_ROLLBACK)

    def as_dict(self) -> dict:
        return {
            "decision": self.decision,
            "label": DECISION_LABEL[self.decision],
            "target_stage": self.target_stage,
            "last_completed_stage": self.last_completed_stage,
            "interrupted_stage": self.interrupted_stage,
            "reason": self.reason,
            "progress": self.progress,
            "migration": self.migration,
            "field": self.field,
        }


def decide_resume(progress: dict, migration: dict, field_state: dict) -> ResumeDecision:
    """按**阶段**出续作决议。判定规则在此定死。

    - 无在途投运（或已完成）→ fresh；
    - 迁移核对不过（停机期间参数被改过 / 现场状态对不上）→ rollback，退到链首
      weather 重建全部假设，绝不带着旧假设续做；
    - 迁移通过：中断阶段策略为 resume → resume 到该阶段（已完成步骤跳过）；
      策略为 rerun → rollback 到该阶段指定的重做边界。
    """

    current = str(progress.get("current_stage", ""))
    completed = [str(name) for name in progress.get("completed_stages", [])]
    last_completed = completed[-1] if completed else ""
    interrupted = current if current in STAGE_ORDER else ""

    if not progress or current == DONE or not current:
        return ResumeDecision(
            DECISION_FRESH,
            STAGE_ORDER[0],
            last_completed,
            "",
            "无在途投运记录，从头开始",
            progress,
            migration,
            field_state,
        )

    if not migration.get("ok", False):
        return ResumeDecision(
            DECISION_ROLLBACK,
            WEATHER,
            last_completed,
            interrupted,
            f"参数核对未通过（迁移）：{'; '.join(migration.get('mismatches', [])) or '未知差异'}，"
            "禁止续作，退回链首重建假设",
            progress,
            migration,
            field_state,
        )

    policy = STAGE_POLICY[current]
    if policy == "resume":
        return ResumeDecision(
            DECISION_RESUME,
            current,
            last_completed,
            interrupted,
            f"参数核对通过，阶段 {current} 允许幂等续做",
            progress,
            migration,
            field_state,
        )

    target = STAGE_RERUN_TARGET[current]
    return ResumeDecision(
        DECISION_ROLLBACK,
        target,
        last_completed,
        interrupted,
        f"参数核对通过，但阶段 {current} 策略为整体重做，退回 {target}",
        progress,
        migration,
        field_state,
    )
