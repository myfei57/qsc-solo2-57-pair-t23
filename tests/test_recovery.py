"""投运掉电续作：进度落盘、按阶段判定、参数迁移核对。

覆盖用户口径：

- 每步进度落盘，重启后能判定接着做还是退回去；
- 判定粒度**按阶段**定死，阶段内步骤只做幂等去重；
- 续做时序不乱，已完成步骤绝不做第二遍；
- 参数核对属于迁移，停机期间改过参数（或现场被拨动）核对不过就不许续；
- 续作决议落盘、供控制台展示并写审计，与现场状态一致。
"""

from __future__ import annotations

import dataclasses

import pytest

from heatnet.errors import PlantError
from heatnet.recovery import (
    DECISION_ABORT,
    DECISION_FRESH,
    DECISION_RESUME,
    DECISION_ROLLBACK,
    STAGE_ORDER,
    StartupContext,
    plan_resume,
    run_startup,
)
from heatnet.recovery.decision import STAGE_POLICY, ResumeDecision


class _Crash(Exception):
    """测试用掉电信号：在某步骤已落盘后立刻中断。"""


def _crash_after(stage: str, step: str):
    def _halt(current_stage: str, current_step: str) -> None:
        if current_stage == stage and current_step == step:
            raise _Crash()

    return _halt


def _drive_to_crash(platform, outdoor_c: float, demand_pct: float, stage: str, step: str) -> None:
    ctx = platform._startup_context(outdoor_c, demand_pct)
    decision = platform._resolve_startup_decision(ctx)
    with pytest.raises(_Crash):
        run_startup(ctx, platform.startup_progress, decision, halt=_crash_after(stage, step))


def test_fresh_startup_runs_whole_chain_and_clears_progress(platform):
    platform.start()
    report = platform.startup(-8.0, 60.0)
    assert report["ok"] is True
    assert report["stage"] == "done"
    assert report["resumed"] == DECISION_FRESH
    assert not platform.startup_progress.exists()
    assert platform.status()["startup_recovery"]["in_flight"] is False


def test_resume_policy_is_fixed_per_stage():
    # 判定粒度按阶段定死：不可安全续接的阶段整体重做，幂等阶段才允许续。
    assert STAGE_POLICY["weather"] == "rerun"
    assert STAGE_POLICY["quality"] == "rerun"
    assert STAGE_POLICY["cycle"] == "rerun"
    assert STAGE_POLICY["quantity"] == "resume"
    assert STAGE_POLICY["primary"] == "resume"
    assert STAGE_POLICY["makeup"] == "resume"
    assert STAGE_ORDER == ("weather", "quality", "quantity", "primary", "makeup", "cycle")


def test_restart_with_no_in_flight_startup_decides_fresh(platform):
    ctx = platform._startup_context(-8.0, 60.0)
    decision = plan_resume(ctx, platform.startup_progress)
    assert decision.decision == DECISION_FRESH
    assert decision.target_stage == "weather"


def test_crash_in_resumable_stage_restores_field_and_skips_done_step(tmp_path):
    from heatnet.app import build_platform
    from heatnet.config import default_config
    from heatnet.clock import ManualClock

    root = str(tmp_path / "state")
    cfg = default_config()
    clk = ManualClock()

    first = build_platform(cfg, clock=clk, storage_root=root)
    first.start()
    _drive_to_crash(first, -8.0, 60.0, "quantity", "set_flow")
    crashed = first.startup_progress.load()
    assert crashed["current_stage"] == "quantity"
    assert crashed["stages"]["quantity"]["steps"] == ["set_flow"]
    assert crashed["stages"]["quantity"]["done"] is False

    # 新进程：现场恢复到调量完成，决议按阶段判为续做 quantity。
    second = build_platform(cfg, clock=clk, storage_root=root)
    assert second.source.state() == "online"
    assert second.source.flow_t_h() == pytest.approx(1440.0)
    decision = plan_resume(second._startup_context(-8.0, 60.0), second.startup_progress)
    assert decision.decision == DECISION_RESUME
    assert decision.target_stage == "quantity"

    report = second.startup(-8.0, 60.0)
    assert report["ok"] is True
    actions = [(step["component"], step["action"]) for step in report["steps"]]
    # 已完成步骤不许再做第二遍，更早的阶段也不重放。
    assert ("source", "set_flow") not in actions
    assert ("source", "fire_forecast") not in actions
    # 从断点按原时序续：一次网跟随 → 补水 → 循环。
    assert actions == [
        ("primary", "follow_source"),
        ("secondary", "makeup"),
        ("pump", "start"),
        ("secondary", "settle"),
    ]


@pytest.mark.parametrize(
    "stage,step",
    [("quality", "confirm_quality"), ("cycle", "circulate")],
)
def test_crash_in_rerun_stage_rolls_back_to_weather(tmp_path, stage, step):
    from heatnet.app import build_platform
    from heatnet.config import default_config
    from heatnet.clock import ManualClock

    root = str(tmp_path / "state")
    cfg = default_config()
    clk = ManualClock()
    first = build_platform(cfg, clock=clk, storage_root=root)
    first.start()
    _drive_to_crash(first, -8.0, 60.0, stage, step)

    second = build_platform(cfg, clock=clk, storage_root=root)
    decision = plan_resume(second._startup_context(-8.0, 60.0), second.startup_progress)
    assert decision.decision == DECISION_ROLLBACK
    assert decision.target_stage == "weather"


def test_config_changed_during_outage_blocks_resume_and_reruns(tmp_path):
    from heatnet.app import build_platform
    from heatnet.config import default_config
    from heatnet.clock import ManualClock

    root = str(tmp_path / "state")
    cfg = default_config()
    clk = ManualClock()
    first = build_platform(cfg, clock=clk, storage_root=root)
    first.start()
    _drive_to_crash(first, -8.0, 60.0, "makeup", "makeup")

    changed = dataclasses.replace(
        cfg, source=dataclasses.replace(cfg.source, flow_max_t_h=5000.0)
    )
    second = build_platform(changed, clock=clk, storage_root=root)
    decision = plan_resume(second._startup_context(-8.0, 60.0), second.startup_progress)
    assert decision.decision == DECISION_ROLLBACK
    assert decision.target_stage == "weather"
    assert any("配置" in item for item in decision.migration["mismatches"])

    report = second.startup(-8.0, 60.0)
    assert report["ok"] is True
    assert report["resumed"] == DECISION_ROLLBACK
    actions = {(step["component"], step["action"]) for step in report["steps"]}
    # 退回链首：全部阶段按序重做，不带着旧假设续。
    assert ("temp", "apply_forecast") in actions
    assert ("source", "set_flow") in actions
    assert ("secondary", "makeup") in actions


def test_different_startup_input_blocks_resume(tmp_path):
    from heatnet.app import build_platform
    from heatnet.config import default_config
    from heatnet.clock import ManualClock

    root = str(tmp_path / "state")
    cfg = default_config()
    clk = ManualClock()
    first = build_platform(cfg, clock=clk, storage_root=root)
    first.start()
    _drive_to_crash(first, -8.0, 60.0, "makeup", "makeup")

    second = build_platform(cfg, clock=clk, storage_root=root)
    # 显式用一组新输入核对：与落盘假设不同即判为迁移差异，不许续。
    decision = plan_resume(
        second._startup_context(2.0, 40.0),
        second.startup_progress,
        use_recorded_input=False,
    )
    assert decision.decision == DECISION_ROLLBACK
    assert any("投运输入" in item for item in decision.migration["mismatches"])


def test_field_tampering_after_outage_blocks_resume(tmp_path):
    from heatnet.app import build_platform
    from heatnet.config import default_config
    from heatnet.clock import ManualClock

    root = str(tmp_path / "state")
    cfg = default_config()
    clk = ManualClock()
    first = build_platform(cfg, clock=clk, storage_root=root)
    first.start()
    _drive_to_crash(first, -8.0, 60.0, "cycle", "circulate")

    tripped = build_platform(cfg, clock=clk, storage_root=root)
    tripped.source.trip("manual-isolation")
    decision = plan_resume(tripped._startup_context(-8.0, 60.0), tripped.startup_progress)
    assert decision.decision == DECISION_ROLLBACK
    assert any("停机" in item for item in decision.migration["mismatches"])

    bled = build_platform(cfg, clock=clk, storage_root=root)
    bled.secondary.bleed_pressure(120.0)
    decision = plan_resume(bled._startup_context(-8.0, 60.0), bled.startup_progress)
    assert decision.decision == DECISION_ROLLBACK
    assert any("定压" in item for item in decision.migration["mismatches"])


def test_abort_decision_refuses_to_move_devices(tmp_path):
    from heatnet.app import build_platform
    from heatnet.config import default_config
    from heatnet.clock import ManualClock

    root = str(tmp_path / "state")
    cfg = default_config()
    platform = build_platform(cfg, clock=ManualClock(), storage_root=root)
    _drive_to_crash(platform, -8.0, 60.0, "makeup", "makeup")
    decision = ResumeDecision(
        DECISION_ABORT, "weather", "primary", "makeup", "需人工接管", {}, {}, {}
    )
    with pytest.raises(PlantError):
        run_startup(platform._startup_context(-8.0, 60.0), platform.startup_progress, decision)


def test_decision_is_persisted_audited_and_exposed(tmp_path):
    from heatnet.app import build_platform
    from heatnet.config import default_config
    from heatnet.clock import ManualClock

    root = str(tmp_path / "state")
    cfg = default_config()
    clk = ManualClock()
    first = build_platform(cfg, clock=clk, storage_root=root)
    first.start()
    _drive_to_crash(first, -8.0, 60.0, "quantity", "set_flow")

    second = build_platform(cfg, clock=clk, storage_root=root)
    second.startup(-8.0, 60.0)

    # 审计里既有续作决议也有参数核对（迁移），且决议与执行用同一份口径。
    actions = [event.action for event in second.bus.history("recovery")]
    assert "startup_resume_decision" in actions
    assert "parameter_verification" in actions
    decision_events = [
        event.payload for event in second.bus.history("recovery")
        if event.action == "startup_resume_decision"
    ]
    assert decision_events[-1]["decision"] == DECISION_RESUME
    assert decision_events[-1]["target_stage"] == "quantity"

    # 续作完成、进度结案后，控制台恢复视图回到无在途。
    report = second.startup_recovery_report()
    assert report["in_flight"] is False
    assert second.status()["startup_recovery"]["decision"] is None


def test_resume_then_complete_is_equivalent_to_uninterrupted_run(tmp_path):
    """掉电续作跑完的终态，应与一次跑完在关键字段上一致。"""

    from heatnet.app import build_platform
    from heatnet.config import default_config
    from heatnet.clock import ManualClock

    cfg = default_config()
    clk = ManualClock()

    uninterrupted = build_platform(cfg, clock=ManualClock(), storage_root=str(tmp_path / "a"))
    uninterrupted.start()
    ok = uninterrupted.startup(-8.0, 60.0)

    root = str(tmp_path / "b")
    first = build_platform(cfg, clock=ManualClock(), storage_root=root)
    first.start()
    _drive_to_crash(first, -8.0, 60.0, "primary", "follow_source")
    second = build_platform(cfg, clock=ManualClock(), storage_root=root)
    resumed = second.startup(-8.0, 60.0)

    assert resumed["ok"] is ok["ok"]
    assert resumed["flow_t_h"] == ok["flow_t_h"]
    assert resumed["supply_c"] == ok["supply_c"]
    assert resumed["return_c"] == ok["return_c"]
    assert second.source.state() == uninterrupted.source.state()
