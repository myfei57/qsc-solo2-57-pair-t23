"""投运进度的持久化。

一条投运链的进度落成**一个**文档，包含三样东西，写在同一处、同一次原子替换里：

``stage``          最近一次到达的阶段边界；
``completed``      该阶段边界之前已经做完的步骤键（幂等去重就靠它，已完成步骤
                   不许再做第二遍）；
``params``         本次投运所依据参数的指纹（见 :mod:`heatnet.resume.fingerprint`）。

另外记录链是否仍在进行（``running``）：阶段函数返回前先置 ``True``，阶段成功
落边界后置 ``False``。掉电发生在阶段进行中，重启读到的就是 ``running=True``，
据此区分"边界已确认"与"半截子阶段"。

文档形状固定，缺字段或内容不是映射一律按"无进度"处理，让调用方走全新投运，
绝不带着半截解析结果往下走。
"""

from __future__ import annotations

from typing import Any

from ..store.jsonfile import JsonStore
from .stages import DONE, STAGE_ORDER


class StartupProgress:
    """按投运链标识读写进度的薄封装。"""

    def __init__(self, store: JsonStore, prefix: str = "resume") -> None:
        self._store = store
        self._prefix = prefix.strip("/")

    @property
    def store(self) -> JsonStore:
        return self._store

    def doc_id(self, chain: str) -> str:
        """返回某条投运链进度文档的标识。"""

        return f"{self._prefix}/{chain}"

    def load(self, chain: str) -> dict:
        """读取进度；缺失或形状非法时返回表示"无进度"的空文档。"""

        raw = self._store.read(self.doc_id(chain))
        if not isinstance(raw, dict):
            return {}
        stage = raw.get("stage")
        if stage not in STAGE_ORDER and stage != DONE:
            return {}
        completed = raw.get("completed")
        params = raw.get("params")
        return {
            "chain": str(raw.get("chain", chain)),
            "op_id": str(raw.get("op_id", "")),
            "stage": stage,
            "running": bool(raw.get("running", False)),
            "completed": [str(item) for item in completed] if isinstance(completed, list) else [],
            "params": dict(params) if isinstance(params, dict) else {},
            "updated_at": raw.get("updated_at"),
        }

    def save(self, chain: str, payload: dict) -> str:
        """原子写入进度，返回落盘路径。"""

        if not isinstance(payload, dict):
            raise ValueError("进度载荷必须是映射")
        stage = payload.get("stage")
        if stage not in STAGE_ORDER and stage != DONE:
            raise ValueError(f"非法投运阶段: {stage!r}")
        completed = payload.get("completed", [])
        if not isinstance(completed, list):
            raise ValueError("已完成步骤必须是列表")
        return self._store.write(self.doc_id(chain), payload)

    def reset(self, chain: str) -> bool:
        """清除一条链的进度（退回全新投运用），返回是否真的删掉了文件。"""

        return self._store.remove(self.doc_id(chain))

    def exists(self, chain: str) -> bool:
        """是否存在一条可识别的投运进度。"""

        return bool(self.load(chain))
