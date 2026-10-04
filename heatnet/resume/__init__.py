"""投运续作：进度落盘、参数迁移核对与按阶段的续作决议。"""

from __future__ import annotations

from .decision import ResumeDecision, decide_resume, policy_overview
from .fingerprint import (
    MigrationCheck,
    fingerprint,
    normalise_params,
    reconcile,
)
from .manager import DEFAULT_CHAIN, ResumeManager
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
    STAGE_ORDER,
    STAGE_POLICY,
    WEATHER,
    describe_policies,
    next_stage,
    policy_of,
)
from .store import StartupProgress

__all__ = [
    "StartupProgress",
    "ResumeManager",
    "ResumeDecision",
    "MigrationCheck",
    "decide_resume",
    "policy_overview",
    "reconcile",
    "fingerprint",
    "normalise_params",
    "DEFAULT_CHAIN",
    "STAGE_ORDER",
    "STAGE_POLICY",
    "describe_policies",
    "policy_of",
    "next_stage",
    "WEATHER",
    "QUALITY",
    "QUANTITY",
    "MAKEUP",
    "CYCLE",
    "DONE",
    "REDO",
    "RESUME",
    "ROLLBACK",
    "BLOCKED",
    "FINISHED",
]
