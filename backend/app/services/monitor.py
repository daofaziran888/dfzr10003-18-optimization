"""运行监控面板业务规则：状态流转、字段校验与筛选口径都收在这里。

监控清单的阈值生效结论、越界区间与告警确认状态统一从
``threshold_governance`` 读取，与质量页保持同一份口径；
监控采样判定也走治理服务，不再直接引用行内静态阈值。
"""
from __future__ import annotations

from typing import Any

from app.services.threshold import threshold_governance
from app.store import store

MODULE = "monitor"
REQUIRED_FIELDS = ["指标编号", "指标名称", "监控对象"]
STATUS_ORDER = ["草稿", "运行中", "已暂停", "已归档"]
ACTION_RULES = {"启动监控": "草稿", "暂停监控": "运行中", "归档指标": "已暂停"}
NEGATIVE_ACTIONS = []


class MonitorService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("指标编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._with_conclusion(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return self._with_conclusion(entry)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"监控指标 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于运行监控面板可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"监控指标已{action}"

    # ---------- 告警与采样（共享口径，委托给 threshold_governance） ----------
    def list_alerts(self, metric_code: str | None = None) -> list[dict[str, Any]]:
        return threshold_governance.list_alerts(metric_code=metric_code)

    def ack_alert(self, alert_id: int, operator: str) -> tuple[dict[str, Any] | None, str, bool]:
        return threshold_governance.ack_alert(alert_id, operator)

    def list_samples(self, metric_code: str | None = None) -> list[dict[str, Any]]:
        samples = threshold_governance.list_samples()
        if metric_code:
            samples = [row for row in samples if row.get("指标编号") == metric_code]
        return samples

    def record_sample(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"监控指标 {entry_id} 不存在或已归档"
        rule_code = str(entry.get("关联规则") or "").strip()
        if not rule_code:
            return None, f"监控指标 {entry.get('指标编号')} 未关联质量规则，采样未写入"
        return threshold_governance.record_sample(
            rule_code,
            str(entry.get("指标编号", "")),
            str(values.get("批次") or "").strip(),
            str(values.get("采样时间") or "").strip(),
            values.get("采样值"),
        )

    def _with_conclusion(self, row: dict[str, Any]) -> dict[str, Any]:
        """给清单行注入共享阈值生效结论；拷贝成行，不回写存储层。"""
        merged = dict(row)
        merged.update(threshold_governance.conclusion_for_metric(str(row.get("指标编号", ""))))
        return merged
