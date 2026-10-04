"""投运续作：阶段进度、按阶段决议、参数迁移核对与控制台/审计一致性。

这些用例钉死续作口径：

* 续作按**阶段**判，不按步骤判；阶段策略是固定常量；
* 已确认阶段整段跳过、已完成步骤不做第二遍；
* 参数核对属于迁移：未核对或漂移未确认一律不许续；
* 续作决议、现场状态、审计三者一致，决议供控制台展示。
"""

from __future__ import annotations

import pytest

from heatnet.console.api import Api
from heatnet.errors import InterlockError
from heatnet.resume import (
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
    STAGE_POLICY,
    WEATHER,
    StartupProgress,
    decide_resume,
    fingerprint,
    normalise_params,
    reconcile,
)
from heatnet.resume.stages import describe_policies


def _params(outdoor_c: float = -8.0, demand_pct: float = 60.0) -> dict:
    return normalise_params(
        {
            "outdoor_c": outdoor_c,
            "demand_pct": demand_pct,
            "curve_supply_c": 81.2,
            "supply_target_c": 92.0,
            "quality_confirm_s": 12.0,
            "flow_min_t_h": 900.0,
            "flow_max_t_h": 4200.0,
            "makeup_target_kpa": 320.0,
            "makeup_min_kpa": 260.0,
            "makeup_max_kpa": 420.0,
        }
    )


# ---- 阶段策略定死 ----------------------------------------------------


def test_resume_granularity_is_stage_and_policies_are_fixed():
    overview = describe_policies()
    assert [item["stage"] for item in overview] == [WEATHER, QUALITY, QUANTITY, MAKEUP, CYCLE]
    assert STAGE_POLICY == {
        WEATHER: "redo",
        QUALITY: "redo",
        QUANTITY: "resume",
        MAKEUP: "rollback",
        CYCLE: "rollback",
    }
    # 没有任何阶段使用"按步骤"策略，口径唯一。
    assert set(STAGE_POLICY.values()) <= {"redo", "resume", "rollback"}


# ---- 参数迁移核对 ----------------------------------------------------


def test_fingerprint_stable_and_detects_drift():
    params = _params()
    assert fingerprint(params) == fingerprint(dict(params))
    changed = dict(params, outdoor_c=-20.0)
    assert fingerprint(changed) != fingerprint(params)


def test_reconcile_match_drifted_and_confirmed():
    params = _params()
    match = reconcile(params, dict(params))
    assert match.passed and match.status == "match"

    drifted = reconcile(params, dict(params, demand_pct=80.0))
    assert not drifted.passed and drifted.status == "drifted"
    assert drifted.changed == ("demand_pct",)
    with pytest.raises(InterlockError):
        drifted.require_passed()

    confirmed = reconcile(params, dict(params, demand_pct=80.0), confirmed=True)
    assert confirmed.passed and confirmed.status == "confirmed"
    confirmed.require_passed()  # 不抛


def test_reconcile_without_recorded_params_is_pending():
    check = reconcile(None, _params())
    assert check.status == "pending" and not check.passed


# ---- 决议（按阶段判） ------------------------------------------------


def test_decision_no_progress_is_full_start_even_when_pending():
    decision = decide_resume({}, reconcile(None, _params()))
    assert decision.decision == REDO
    assert decision.resume_from == WEATHER


def test_decision_done_is_finished():
    progress = {"stage": DONE, "running": False, "completed": [CYCLE], "params": _params()}
    decision = decide_resume(progress, reconcile(_params(), _params()))
    assert decision.decision == FINISHED and decision.resume_from is None


@pytest.mark.parametrize(
    "stage,running,expected",
    [
        (WEATHER, True, REDO),
        (QUALITY, True, REDO),
        (QUANTITY, True, RESUME),
        (MAKEUP, True, ROLLBACK),
        (CYCLE, True, ROLLBACK),
        (WEATHER, False, RESUME),
        (MAKEUP, False, RESUME),
        (CYCLE, False, FINISHED),
    ],
)
def test_decision_follows_stage_policy(stage, running, expected):
    progress = {"stage": stage, "running": running, "completed": [stage], "params": _params()}
    decision = decide_resume(progress, reconcile(_params(), _params()))
    assert decision.decision == expected


def test_decision_blocked_when_params_drifted():
    progress = {"stage": MAKEUP, "running": False, "completed": [QUANTITY], "params": _params()}
    decision = decide_resume(progress, reconcile(_params(), _params(-20.0, 80.0)))
    assert decision.decision == BLOCKED
    assert not decision.allowed
    assert decision.resume_from is None
    with pytest.raises(InterlockError):
        decision.require_allowed()


def test_decision_record_is_immutable_and_serializable():
    progress = {"stage": QUANTITY, "running": False, "completed": [WEATHER, QUALITY], "params": _params()}
    decision = decide_resume(progress, reconcile(_params(), _params()))
    payload = decision.as_dict()
    assert payload["decision"] == RESUME
    assert payload["resume_from"] == MAKEUP
    assert payload["migration"]["status"] == "match"
    with pytest.raises(Exception):
        decision.decision = ROLLBACK  # type: ignore[misc]


# ---- 进度落盘 --------------------------------------------------------


def test_progress_round_trip_and_rejects_bad_shape(tmp_path):
    progress = StartupProgress.__new__(StartupProgress)
    from heatnet.store.jsonfile import JsonStore

    progress.__init__(JsonStore(str(tmp_path)))
    assert progress.load("primary") == {}
    payload = {
        "chain": "primary",
        "op_id": "op-1",
        "stage": QUANTITY,
        "running": True,
        "completed": [WEATHER, QUALITY],
        "params": _params(),
        "updated_at": 7.0,
    }
    progress.save("primary", payload)
    loaded = progress.load("primary")
    assert loaded["stage"] == QUANTITY and loaded["running"] is True
    assert loaded["completed"] == [WEATHER, QUALITY]
    assert progress.reset("primary") is True
    assert progress.exists("primary") is False


# ---- 平台端到端 ------------------------------------------------------


def _commission(platform):
    platform.start()
    platform.push_weather(-8.0)
    return platform.startup(-8.0, 60.0)


def test_full_startup_persists_done_progress(platform):
    report = _commission(platform)
    assert report["ok"] and report["stage"] == DONE
    assert report["executed"] == [WEATHER, QUALITY, QUANTITY, MAKEUP, CYCLE]
    progress = platform.resume.progress()
    assert progress["stage"] == DONE and progress["running"] is False


def test_restart_after_completion_is_finished_and_does_nothing(platform):
    _commission(platform)
    root = platform.store.root
    from heatnet.app import build_platform
    from heatnet.clock import ManualClock
    from heatnet.config import default_config

    restarted = build_platform(default_config(), clock=ManualClock(), storage_root=root)
    decision = restarted.resume.resume_decision(restarted.startup_params(-8.0, 60.0))
    assert decision.decision == FINISHED
    again = restarted.startup(-8.0, 60.0)
    assert again["stage"] == DONE and again["steps"] == []


def test_completed_stages_are_not_executed_twice(platform):
    _commission(platform)
    # 回退到 quantity 边界，模拟"调量刚做完就停电"。
    snapshot = platform.resume.progress()
    platform.resume.save_progress(
        {**snapshot, "stage": QUANTITY, "running": False, "completed": [WEATHER, QUALITY, QUANTITY]}
    )
    root = platform.store.root
    from heatnet.app import build_platform
    from heatnet.clock import ManualClock
    from heatnet.config import default_config

    restarted = build_platform(default_config(), clock=ManualClock(), storage_root=root)
    report = restarted.startup(-8.0, 60.0)
    assert report["resume"]["decision"] == RESUME
    assert report["resumed_from"] == MAKEUP
    # 只物理执行未完成的两个阶段。
    assert report["executed"] == [MAKEUP, CYCLE]
    skipped = [step["action"] for step in report["steps"] if step["detail"].get("skipped")]
    assert set(skipped) == {"apply_forecast", "confirm_quality", "follow_source"}


def test_makeup_interrupted_triggers_rollback_and_restart(platform):
    _commission(platform)
    snapshot = platform.resume.progress()
    platform.resume.save_progress(
        {**snapshot, "stage": MAKEUP, "running": True, "completed": [WEATHER, QUALITY, QUANTITY]}
    )
    root = platform.store.root
    from heatnet.app import build_platform
    from heatnet.clock import ManualClock
    from heatnet.config import default_config

    restarted = build_platform(default_config(), clock=ManualClock(), storage_root=root)
    decision = restarted.resume.resume_decision(restarted.startup_params(-8.0, 60.0))
    assert decision.decision == ROLLBACK
    report = restarted.startup(-8.0, 60.0)
    assert report["rollback"]["progress_cleared"] is True
    assert report["rollback"]["source_state"] == "idle"
    # 退回后整条链从头重做。
    assert report["executed"] == [WEATHER, QUALITY, QUANTITY, MAKEUP, CYCLE]
    assert report["stage"] == DONE


def test_drifted_params_block_resume_until_reconfirmed(platform):
    _commission(platform)
    root = platform.store.root
    from heatnet.app import build_platform
    from heatnet.clock import ManualClock
    from heatnet.config import default_config

    restarted = build_platform(default_config(), clock=ManualClock(), storage_root=root)
    decision = restarted.resume.resume_decision(restarted.startup_params(-20.0, 80.0))
    assert decision.decision == BLOCKED
    with pytest.raises(InterlockError):
        restarted.startup(-20.0, 80.0)
    confirmation = restarted.confirm_startup_params(-20.0, 80.0)
    assert confirmation["passed"] is True
    # 已完成的链在确认后给 finished，而不是带着旧假设盲跑。
    after = restarted.resume.resume_decision(restarted.startup_params(-20.0, 80.0))
    assert after.decision == FINISHED


# ---- 控制台展示与审计一致 --------------------------------------------


def test_resume_decision_is_shown_and_audited(platform):
    api = Api(platform)
    _commission(platform)
    status, body = api.handle("GET", "/api/status", {}, {})
    assert status == 200 and body["resume"]["progress"]["stage"] == DONE
    assert body["resume"]["granularity"] == "stage"

    status, body = api.handle("GET", "/api/resume", {"outdoor_c": "-8", "demand_pct": "60"}, {})
    assert status == 200 and body["decision"]["decision"] == FINISHED

    actions = [entry["action"] for entry in platform.audit.entries(limit=200)]
    assert "resume_decision" in actions


def test_failed_stage_stays_in_progress_and_never_confirms_boundary(platform):
    # 越界气象（适用区间 [-26,18]），让 weather 阶段失败。
    platform.start()
    report = platform.startup(40.0, 60.0)
    assert report["ok"] is False and report["stage"] == WEATHER
    progress = platform.resume.progress()
    # 失败停在原地：仍是"阶段进行中"，且该阶段不算已完成。
    assert progress["running"] is True
    assert progress["completed"] == []
    root = platform.store.root
    from heatnet.app import build_platform
    from heatnet.clock import ManualClock
    from heatnet.config import default_config

    restarted = build_platform(default_config(), clock=ManualClock(), storage_root=root)
    decision = restarted.resume.resume_decision(restarted.startup_params(40.0, 60.0))
    # weather 策略是 redo：半截中断重启后从头重做，绝不跳到下一阶段。
    assert decision.decision == REDO and decision.resume_from == WEATHER


def test_console_blocks_drifted_startup_with_409(platform):
    api = Api(platform)
    _commission(platform)
    status, body = api.handle("POST", "/api/startup", {}, {"outdoor_c": -20.0, "demand_pct": 80.0})
    assert status == 409 and body["component"] == "resume"
    status, body = api.handle("POST", "/api/resume/confirm", {}, {"outdoor_c": -20.0, "demand_pct": 80.0})
    assert status == 200 and body["passed"] is True
