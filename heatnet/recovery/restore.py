"""按落盘进度把设备现场重建回内存。

热源、曲线、一次网、二次网本身是实时对象，进程重启后内存归零；但投运进度文档里
保存了每个步骤/阶段提交时的现场快照。这里取链上**最后一个已持久化动作**的快照，
把设备一次性恢复到该现场，续作才是在"当时确认过的真实状态"上继续，而不是从冷态
假装续做。

快照只在动作确认成功之后落盘，因此最后一个快照就是最后一个真实完成的动作；
"步骤已落盘、阶段未提交"窗口里的半成品，至少其已完成步骤本身是既成事实，可以
安全恢复，未完成的部分交给续作/退回决议重跑。
"""

from __future__ import annotations

from ..primary.network import PrimaryNetwork
from ..secondary.loop import SecondaryLoop
from ..source.source import HeatSource
from ..temp.curve import HeatCurve
from .progress import StartupProgress


def restore_field(
    progress_store: StartupProgress,
    *,
    source: HeatSource,
    curve: HeatCurve,
    network: PrimaryNetwork,
    loop: SecondaryLoop,
) -> str:
    """从最后一个已持久化现场恢复设备，返回恢复到的阶段（无则空串）。"""

    checkpoint, field = progress_store.last_field()
    if not checkpoint or not field:
        return ""

    source.restore(
        state=str(field.get("source_state", "idle")),
        temperature_c=float(field.get("source_temperature_c", 0.0)),
        flow_t_h=float(field.get("source_flow_t_h", 0.0)),
        target_c=float(field.get("source_target_c", source.target_c())),
        quality_hold_s=float(field.get("source_hold_s", 0.0)),
    )
    curve.restore(
        outdoor_c=float(field.get("curve_outdoor_c", curve.outdoor_c())),
        supply_c=float(field.get("curve_supply_c", curve.supply_c())),
        epoch=int(field.get("curve_epoch", curve.epoch())),
        updated_at=float(field.get("curve_updated_at", 0.0)),
    )
    network.restore(
        supply_c=float(field.get("network_supply_c", 0.0)),
        return_c=float(field.get("network_return_c", 0.0)),
        flow_t_h=float(field.get("network_flow_t_h", 0.0)),
    )
    loop.restore(
        supply_c=float(field.get("loop_supply_c", loop.supply_c())),
        return_c=float(field.get("loop_return_c", loop.return_c())),
        pressure_kpa=float(field.get("pressure_kpa", loop.pressure_kpa())),
    )
    return checkpoint
