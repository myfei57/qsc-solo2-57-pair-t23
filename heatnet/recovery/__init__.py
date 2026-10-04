"""投运续作：阶段、迁移核对与续作决议。

投运是一条按顺序走的链，中途停电或进程重启后必须能回答三件事：

1. 走到哪了——每个阶段的完成情况都落盘，阶段是恢复判定的最小单元；
2. 接着做还是退回去——按阶段判，阶段自带续作/退回策略，不允许按步骤临时拍板；
3. 现场假设还成不成立——停机期间参数可能被改过，续作前必须重新核对（迁移），
   核对不过不许续，只能退到安全边界重做。

判定粒度在此定死：**续作按阶段判**。阶段内的步骤只负责幂等——已落盘完成的步骤
重启后跳过，绝不再做第二遍；"接着做"还是"退回去"永远以阶段为单位决定，不存在
"按步骤续"。
"""

from __future__ import annotations

from .decision import (
    DECISION_ABORT,
    DECISION_FRESH,
    DECISION_RESUME,
    DECISION_ROLLBACK,
    STAGE_ORDER,
    ResumeDecision,
)
from .migration import MigrationCheck, migration_fingerprint, verify_migration
from .progress import StartupProgress
from .restore import restore_field
from .runner import StartupContext, field_snapshot, plan_resume, run_startup

__all__ = [
    "DECISION_ABORT",
    "DECISION_FRESH",
    "DECISION_RESUME",
    "DECISION_ROLLBACK",
    "STAGE_ORDER",
    "MigrationCheck",
    "ResumeDecision",
    "StartupContext",
    "StartupProgress",
    "field_snapshot",
    "migration_fingerprint",
    "plan_resume",
    "restore_field",
    "run_startup",
    "verify_migration",
]
