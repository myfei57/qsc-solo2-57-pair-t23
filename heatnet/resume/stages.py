"""投运阶段与续作策略（定死，不允许按步骤摇摆）。

投运是一条链：``weather → quality → quantity → makeup → cycle → done``。
停电或进程重启以后"接着做还是退回去"**只按阶段判，不按步骤判**——步骤只用于
幂等去重（已经做完的步骤不许再做第二遍）与进度展示，不参与续/退决议。

每个阶段的续作策略在这里固定成常量，调用方不能临时改：

``REDO``      阶段不可中途续。该阶段已经完整做完（处于"上一阶段"边界）才允许
              跳过；只要阶段处在进行中，就整条链从头重做。
``RESUME``    阶段天然幂等（调量是一次"断言到目标值"的写入），进行中也允许
              从边界断言续做，不会产生第二遍副作用。
``ROLLBACK``  阶段带物理累积副作用（补水／建立循环）。进行中无法判断半截动作
              现场走到了哪里，必须先退回安全态再从头来，禁止盲续。
"""

from __future__ import annotations

from dataclasses import dataclass

# 阶段标识，顺序即投运链顺序；done 是完成哨兵，本身没有策略。
WEATHER = "weather"
QUALITY = "quality"
QUANTITY = "quantity"
MAKEUP = "makeup"
CYCLE = "cycle"
DONE = "done"

# 续作策略。
REDO = "redo"
RESUME = "resume"
ROLLBACK = "rollback"

#: 投运链阶段顺序（不含完成哨兵）。
STAGE_ORDER: tuple[str, ...] = (WEATHER, QUALITY, QUANTITY, MAKEUP, CYCLE)

#: 每个阶段固定的续作策略。决议口径只认这张表。
STAGE_POLICY: dict[str, str] = {
    WEATHER: REDO,      # 曲线/点火假设必须用重启后最新气象重建
    QUALITY: REDO,      # 调质确认时间在内存里累积，掉电即失效，必须重新确认
    QUANTITY: RESUME,   # 调量是幂等断言，目标值重写一遍无第二遍副作用
    MAKEUP: ROLLBACK,   # 补水带物理累积，半截续做可能过补，必须先退回
    CYCLE: ROLLBACK,    # 循环涉及启泵，半截续做可能二次启泵，必须先退回
}

#: 决议动作。
RESUME = "resume"
REDO = "redo"
ROLLBACK = "rollback"
BLOCKED = "blocked"
FINISHED = "finished"


@dataclass(frozen=True)
class StagePolicy:
    """一个阶段的固定续作策略与理由。"""

    stage: str
    policy: str
    rationale: str

    def as_dict(self) -> dict:
        return {"stage": self.stage, "policy": self.policy, "rationale": self.rationale}


_STAGE_RATIONALE: dict[str, str] = {
    WEATHER: "曲线与点火假设必须按重启后最新气象重建",
    QUALITY: "调质确认时间只在内存累积，掉电即失效",
    QUANTITY: "调量是对目标值的幂等断言，重写无第二遍副作用",
    MAKEUP: "补水带物理累积，半截续做可能过补，必须先退回安全态",
    CYCLE: "循环涉及启泵，半截续做可能二次启泵，必须先退回安全态",
}


def policy_of(stage: str) -> str:
    """返回某阶段固定的续作策略；未知阶段直接报错，不允许悄悄放过。"""

    try:
        return STAGE_POLICY[stage]
    except KeyError:
        raise ValueError(f"未知投运阶段: {stage!r}")


def describe_policies() -> list[dict]:
    """返回全部阶段策略，供控制台展示与核对。"""

    return [
        StagePolicy(stage, STAGE_POLICY[stage], _STAGE_RATIONALE[stage]).as_dict()
        for stage in STAGE_ORDER
    ]


def next_stage(stage: str) -> str | None:
    """返回链上的下一个阶段；末阶段返回完成哨兵，完成哨兵返回 ``None``。"""

    if stage == DONE:
        return None
    if stage not in STAGE_POLICY:
        raise ValueError(f"未知投运阶段: {stage!r}")
    index = STAGE_ORDER.index(stage)
    return STAGE_ORDER[index + 1] if index + 1 < len(STAGE_ORDER) else DONE
