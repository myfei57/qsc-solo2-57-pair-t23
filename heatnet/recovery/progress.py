"""投运进度的落盘与恢复。

进度文档 ``recovery/startup_progress`` 是续作决议的唯一事实来源。一个投运实例
记录：走到的当前阶段、每个阶段内已完成的步骤（幂等去重键）、整体完成的阶段、
启动参数与迁移指纹、以及每次阶段提交时的现场快照。

写盘遵循与阀门当前态相同的顺序：动作确认成功后立即把"步骤完成"落盘，再推进
下一步；阶段全部步骤完成后再落"阶段完成"。这样中途掉电后，盘上不会出现"没做
却标完成"的步骤；已完成步骤也不会被再执行第二遍。文档本身由原子 JSON 存储写入，
掉电只会留下上一版完整文档。
"""

from __future__ import annotations

from ..store.jsonfile import JsonStore
from .decision import DONE, STAGE_ORDER, STAGE_STEPS, stage_index

DOC_ID = "recovery/startup_progress"
DECISION_DOC_ID = "recovery/startup_decision"


class StartupProgress:
    """投运进度的薄持久化封装。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @property
    def store(self) -> JsonStore:
        return self._store

    def exists(self) -> bool:
        """是否存在在途（或完成）的投运进度文档。"""

        return self._store.exists(DOC_ID)

    def load(self) -> dict:
        """读取进度；缺失或内容非法时返回空字典（视为无在途投运）。"""

        raw = self._store.read(DOC_ID)
        if isinstance(raw, dict) and isinstance(raw.get("stages"), dict):
            return raw
        return {}

    def begin(self, params: dict, fingerprint: dict, now: float) -> dict:
        """为一次新投运建立进度文档并落盘。"""

        progress = {
            "status": "running",
            "current_stage": STAGE_ORDER[0],
            "completed_stages": [],
            "stages": {stage: {"done": False, "steps": []} for stage in STAGE_ORDER},
            "params": dict(params),
            "fingerprint": dict(fingerprint),
            "started_at": round(float(now), 3),
            "updated_at": round(float(now), 3),
        }
        self._store.write(DOC_ID, progress)
        return progress

    def mark_step(self, stage: str, step: str, snapshot: dict | None = None) -> dict:
        """把某阶段内的一个步骤标记完成（幂等），返回更新后的进度。"""

        progress = self.load()
        record = progress["stages"].setdefault(stage, {"done": False, "steps": []})
        if step not in record["steps"]:
            record["steps"].append(step)
        if snapshot is not None:
            record["field_after"] = snapshot
        self._store.write(DOC_ID, progress)
        return progress

    def commit_stage(self, stage: str, fingerprint: dict, now: float, snapshot: dict | None = None) -> dict:
        """整体完成一个阶段：标记阶段完成、推进 current_stage 并落盘。"""

        progress = self.load()
        record = progress["stages"].setdefault(stage, {"done": False, "steps": []})
        record["done"] = True
        # 阶段完成时把该阶段全部步骤记全，防止单步落盘遗漏导致重跑。
        for step in STAGE_STEPS.get(stage, ()):
            if step not in record["steps"]:
                record["steps"].append(step)
        if snapshot is not None:
            record["field_after"] = snapshot
        if stage not in progress["completed_stages"]:
            progress["completed_stages"].append(stage)
        # 按链序推进 current_stage，不允许跳跃或回写乱序。
        ordered = [s for s in STAGE_ORDER if s in progress["completed_stages"]]
        if len(ordered) >= len(STAGE_ORDER):
            progress["current_stage"] = DONE
            progress["status"] = "complete"
        else:
            nxt = STAGE_ORDER[len(ordered)]
            progress["current_stage"] = nxt
            progress["stages"].setdefault(nxt, {"done": False, "steps": []})
        progress["fingerprint"] = dict(fingerprint)
        progress["updated_at"] = round(float(now), 3)
        self._store.write(DOC_ID, progress)
        return progress

    def rollback_to(self, target_stage: str, fingerprint: dict, now: float) -> dict:
        """退回某个边界：清空目标阶段及其后所有阶段进度，从目标阶段重做。

        已完成步骤"不许再做第二遍"针对的是同一次续做内的顺序；退回是显式决议，
        被退回区间的进度标记必须清掉，这些阶段才会按原顺序整体重跑。
        """

        progress = self.load()
        cutoff = stage_index(target_stage)
        progress["completed_stages"] = [s for s in STAGE_ORDER if stage_index(s) < cutoff]
        for stage in STAGE_ORDER:
            if stage_index(stage) >= cutoff:
                progress["stages"][stage] = {"done": False, "steps": []}
        progress["current_stage"] = target_stage
        progress["status"] = "running"
        progress["fingerprint"] = dict(fingerprint)
        progress["rollback_to"] = target_stage
        progress["updated_at"] = round(float(now), 3)
        self._store.write(DOC_ID, progress)
        return progress

    def done_steps(self, stage: str) -> list[str]:
        """返回某阶段内已落盘完成的步骤。"""

        progress = self.load()
        record = progress.get("stages", {}).get(stage)
        if isinstance(record, dict):
            return list(record.get("steps", []))
        return []

    def last_field(self) -> tuple[str, dict]:
        """返回链上最后一个已持久化现场（阶段提交快照优先，否则取步骤级快照）。

        掉电可能停在"步骤已落盘、阶段尚未提交"的窗口，这时只有步骤级现场可用；
        它代表最后一个真实完成的动作，是重启重建能依据的最新现场。
        """

        progress = self.load()
        if not progress:
            return "", {}
        stages = progress.get("stages", {})
        for stage in reversed(STAGE_ORDER):
            record = stages.get(stage)
            if not isinstance(record, dict):
                continue
            field = record.get("field_after")
            if isinstance(field, dict):
                return stage, field
        return "", {}

    def save_decision(self, decision: dict, now: float) -> dict:
        """落盘续作决议（控制台展示与审计共用这一份）。"""

        payload = {"decided_at": round(float(now), 3), **dict(decision)}
        self._store.write(DECISION_DOC_ID, payload)
        return payload

    def load_decision(self) -> dict:
        """读取最近一次续作决议，缺失或非法时返回空字典。"""

        raw = self._store.read(DECISION_DOC_ID)
        return raw if isinstance(raw, dict) else {}

    def clear(self) -> bool:
        """删除进度与决议文档（交付前重置/一次投运彻底结案后清理）。"""

        removed = self._store.exists(DOC_ID) and self._store.remove(DOC_ID)
        if self._store.exists(DECISION_DOC_ID):
            self._store.remove(DECISION_DOC_ID)
        return removed
