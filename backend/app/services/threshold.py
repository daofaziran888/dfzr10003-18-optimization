"""质量阈值版本治理：准入复核、采样判定、告警去重与幂等确认。

质量页与监控页共用这一份口径，任何一方都不能单独切换判断标准：

- 新阈值版本先进入准入复核（待复核），复核通过且到达生效时间后才参与采样判定；
  复核不通过或版本不完整时保持原版本生效，不完整的新规则不会扩大阻断范围。
- 每条采样读数按「采样时刻」解析命中的阈值版本，判定后留档；历史读数不随
  阈值切换重判，仍然挂着当时命中的旧版本。
- 两个版本同时覆盖一个采样时刻时，按生效时间 → 复核序号（审批顺序）→ 完整度
  取优先级最高的一版，且必须已复核、能覆盖该时刻；被覆盖的版本保留供核对。
- 告警按（规则编号、批次、越界区间）去重：同一组合只产生一条告警；确认动作
  幂等，并发提交时只有第一次转换有效，已确认的区间不再反复提醒。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.store import store

THRESHOLD_MODULE = "threshold"
SAMPLE_MODULE = "sample"
ALERT_MODULE = "alert"
QUALITY_MODULE = "quality"

VERSION_REQUIRED_FIELDS = ["阈值上界", "阈值下界", "阻断级别", "生效时间"]
REVIEW_PENDING = "待复核"
REVIEW_APPROVED = "已复核"
REVIEW_REJECTED = "已驳回"
ACK_OPEN = "未确认"
ACK_DONE = "已确认"


def _parse_moment(value: object) -> datetime | None:
    """把 ISO 时间文本解析成可比较的时刻；非法输入返回 None 而不是抛错。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _now_text() -> str:
    return datetime.now().isoformat(timespec="minutes")


def _to_float(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


class ThresholdGovernance:
    """阈值版本、采样判定与告警确认的共享入口（进程内单例，锁保证并发安全）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._bootstrapped = False
        self._review_seq = 0
        self._alert_seq = 0

    # ---------- 基础取数 ----------
    def _versions(self) -> list[dict[str, Any]]:
        return store.rows(THRESHOLD_MODULE)

    def _samples(self) -> list[dict[str, Any]]:
        return store.rows(SAMPLE_MODULE)

    def _alerts(self) -> list[dict[str, Any]]:
        return store.rows(ALERT_MODULE)

    def bootstrap(self) -> None:
        """启动时重放采样留档：按采样时刻逐条判定并回填命中版本。

        重放走与实时采样完全相同的判定路径，命中既有告警时按去重键合并，
        已确认的告警保持静默，不会重复提醒。
        """
        with self._lock:
            if self._bootstrapped:
                return
            self._bootstrapped = True
            self._review_seq = max(
                (int(row.get("复核序号", 0) or 0) for row in self._versions()), default=0
            )
            self._alert_seq = max(
                (
                    int(str(row.get("告警编号", "MON-0")).rsplit("-", 1)[-1] or 0)
                    for row in self._alerts()
                    if str(row.get("告警编号", "")).startswith("MON-")
                ),
                default=0,
            )
            ordered = sorted(self._samples(), key=lambda row: str(row.get("采样时间", "")))
            for sample in ordered:
                self._judge_sample_locked(sample)
            self._refresh_version_status_locked()

    # ---------- 阈值版本 ----------
    def list_versions(self, rule_code: str) -> list[dict[str, Any]]:
        with self._lock:
            self.bootstrap()
            rows = [row for row in self._versions() if row.get("规则编号") == rule_code]
            return sorted(rows, key=lambda row: int(row.get("版本号", 0) or 0))

    def submit_version(self, rule_code: str, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """登记新阈值版本：一律先进入准入复核，不直接生效。"""
        with self._lock:
            self.bootstrap()
            rule = self._find_rule(rule_code)
            if rule is None:
                return None, f"质量规则 {rule_code} 不存在，阈值版本未登记"
            completeness = self._completeness(values)
            existing = [row for row in self._versions() if row.get("规则编号") == rule_code]
            next_no = max((int(row.get("版本号", 0) or 0) for row in existing), default=0) + 1
            version = {
                "id": max((int(row.get("id", 0)) for row in self._versions()), default=0) + 1,
                "版本编号": f"{rule_code}-V{next_no}",
                "规则编号": rule_code,
                "版本号": next_no,
                "阈值上界": values.get("阈值上界"),
                "阈值下界": values.get("阈值下界"),
                "阻断级别": str(values.get("阻断级别") or "").strip(),
                "提交时间": _now_text(),
                "生效时间": str(values.get("生效时间") or "").strip(),
                "复核状态": REVIEW_PENDING,
                "复核人": "",
                "复核时间": "",
                "复核序号": 0,
                "完整度": completeness,
                "覆盖说明": "",
                "status": "待复核",
                "pending": True,
                "abnormal": False,
            }
            self._versions().append(version)
            if completeness != "完整":
                return version, (
                    f"阈值版本 {version['版本编号']} 已登记但{completeness}，"
                    "复核将不予通过，当前仍按原版本生效"
                )
            return version, f"阈值版本 {version['版本编号']} 已提交，进入准入复核"

    def review_version(
        self, version_id: int, approve: bool, reviewer: str
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """复核阈值版本；重复复核按幂等处理，不改变既有结论。"""
        with self._lock:
            self.bootstrap()
            version = next((row for row in self._versions() if int(row.get("id", 0)) == version_id), None)
            if version is None:
                return None, f"阈值版本 {version_id} 不存在", False
            if version.get("复核状态") != REVIEW_PENDING:
                return version, (
                    f"阈值版本 {version['版本编号']} 已复核过（{version['复核状态']}），"
                    "本次提交按幂等处理，结论不变"
                ), True
            version["复核人"] = reviewer or "质量值班"
            version["复核时间"] = _now_text()
            if not approve:
                version["复核状态"] = REVIEW_REJECTED
                version["status"] = "已驳回"
                version["pending"] = False
                return version, (
                    f"阈值版本 {version['版本编号']} 复核驳回，保持原版本生效"
                ), True
            if version.get("完整度") != "完整":
                version["复核状态"] = REVIEW_REJECTED
                version["status"] = "已驳回"
                version["pending"] = False
                version["abnormal"] = True
                return version, (
                    f"阈值版本 {version['版本编号']}（{version.get('完整度', '不完整')}），"
                    "复核不通过，保持原版本生效，未扩大阻断范围"
                ), False
            version["复核状态"] = REVIEW_APPROVED
            self._review_seq += 1
            version["复核序号"] = self._review_seq
            version["pending"] = False
            self._refresh_version_status_locked()
            return version, (
                f"阈值版本 {version['版本编号']} 复核通过，{version['生效时间']} 起生效；"
                "生效时间之前的读数仍按原版本留档"
            ), True

    def _completeness(self, values: dict[str, Any]) -> str:
        missing = [field for field in VERSION_REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return f"缺失：{'、'.join(missing)}"
        upper = _to_float(values.get("阈值上界"))
        lower = _to_float(values.get("阈值下界"))
        if upper is None or lower is None:
            return "异常：阈值需为数值"
        if upper < lower:
            return "异常：上界小于下界"
        if _parse_moment(values.get("生效时间")) is None:
            return "异常：生效时间格式无效"
        return "完整"

    def _resolve_locked(
        self, rule_code: str, moment_text: str
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """解析某个采样时刻应命中的阈值版本。

        候选必须同时满足：已复核、完整、生效时间不晚于采样时刻（能覆盖该时刻）。
        候选之间按生效时间 → 复核序号（审批顺序）→ 完整度排序，取第一名为生效版本，
        其余候选作为被覆盖版本返回，保留供核对。
        """
        moment = _parse_moment(moment_text)
        if moment is None:
            return None, []
        candidates: list[dict[str, Any]] = []
        for row in self._versions():
            if row.get("规则编号") != rule_code:
                continue
            if row.get("复核状态") != REVIEW_APPROVED:
                continue
            if row.get("完整度") != "完整":
                continue
            effective = _parse_moment(row.get("生效时间"))
            if effective is None or effective > moment:
                continue
            candidates.append(row)
        candidates.sort(
            key=lambda row: (
                _parse_moment(row.get("生效时间")) or datetime.min,
                int(row.get("复核序号", 0) or 0),
                1 if row.get("完整度") == "完整" else 0,
            ),
            reverse=True,
        )
        chosen = candidates[0] if candidates else None
        return chosen, candidates[1:]

    def _refresh_version_status_locked(self) -> None:
        """按当前时刻重算每个规则各版本的展示状态，保证与解析口径一致。"""
        now = _now_text()
        rule_codes = {str(row.get("规则编号", "")) for row in self._versions()}
        for rule_code in rule_codes:
            chosen, covered = self._resolve_locked(rule_code, now)
            covered_ids = {int(row.get("id", 0)) for row in covered}
            for row in self._versions():
                if row.get("规则编号") != rule_code:
                    continue
                if row.get("复核状态") == REVIEW_PENDING:
                    row["status"] = "待复核"
                    row["覆盖说明"] = ""
                elif row.get("复核状态") == REVIEW_REJECTED:
                    row["status"] = "已驳回"
                    row["覆盖说明"] = ""
                elif chosen is not None and int(row.get("id", 0)) == int(chosen.get("id", 0)):
                    row["status"] = "生效中"
                    row["覆盖说明"] = ""
                elif int(row.get("id", 0)) in covered_ids and chosen is not None:
                    row["status"] = "留档"
                    row["覆盖说明"] = f"被 {chosen['版本编号']} 覆盖，留档核对"
                else:
                    row["status"] = "留档"
                    row["覆盖说明"] = "历史版本，留档核对"

    # ---------- 采样判定 ----------
    def record_sample(
        self,
        rule_code: str,
        metric_code: str,
        batch: str,
        sampled_at: str,
        value: float,
    ) -> tuple[dict[str, Any] | None, str]:
        """写入一条采样读数并按采样时刻判定；这是监控采样链路的唯一判断点。"""
        with self._lock:
            self.bootstrap()
            rule = self._find_rule(rule_code)
            if rule is None:
                return None, f"质量规则 {rule_code} 不存在，采样未写入"
            if _parse_moment(sampled_at) is None:
                return None, f"采样时间「{sampled_at}」格式无效，采样未写入"
            if _to_float(value) is None:
                return None, f"采样值「{value}」不是数值，采样未写入"
            sample = {
                "id": max((int(row.get("id", 0)) for row in self._samples()), default=0) + 1,
                "采样编号": f"SMP-{max((int(row.get('id', 0)) for row in self._samples()), default=0) + 1:04d}",
                "规则编号": rule_code,
                "指标编号": metric_code,
                "批次": batch,
                "采样时间": sampled_at,
                "采样值": value,
                "命中版本": "",
                "判定结果": "",
                "越界区间": "",
                "status": "已判定",
                "pending": False,
                "abnormal": False,
            }
            self._samples().append(sample)
            self._judge_sample_locked(sample)
            return sample, f"采样 {sample['采样编号']} 已按 {sample['命中版本']} 判定：{sample['判定结果']}"

    def list_samples(self, rule_code: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self.bootstrap()
            rows = self._samples()
            if rule_code:
                rows = [row for row in rows if row.get("规则编号") == rule_code]
            return sorted(rows, key=lambda row: str(row.get("采样时间", "")))

    def _judge_sample_locked(self, sample: dict[str, Any]) -> None:
        """按采样时刻解析命中版本并判定；越界时按去重键归并告警。"""
        chosen, _covered = self._resolve_locked(str(sample.get("规则编号", "")), str(sample.get("采样时间", "")))
        if chosen is None:
            sample["命中版本"] = "—"
            sample["判定结果"] = "未判定"
            sample["越界区间"] = "—"
            sample["abnormal"] = False
            return
        sample["命中版本"] = chosen["版本编号"]
        value = _to_float(sample.get("采样值"))
        upper = _to_float(chosen.get("阈值上界"))
        lower = _to_float(chosen.get("阈值下界"))
        if value is None or upper is None or lower is None:
            sample["判定结果"] = "未判定"
            sample["越界区间"] = "—"
            sample["abnormal"] = False
            return
        if value > upper:
            interval = f"({upper:g}, +∞)"
            sample["判定结果"] = "越上界"
            sample["越界区间"] = interval
            sample["abnormal"] = True
            self._raise_alert_locked(sample, chosen, interval)
        elif value < lower:
            interval = f"(-∞, {lower:g})"
            sample["判定结果"] = "越下界"
            sample["越界区间"] = interval
            sample["abnormal"] = True
            self._raise_alert_locked(sample, chosen, interval)
        else:
            sample["判定结果"] = "正常"
            sample["越界区间"] = "—"
            sample["abnormal"] = False

    # ---------- 告警 ----------
    def _raise_alert_locked(
        self, sample: dict[str, Any], version: dict[str, Any], interval: str
    ) -> tuple[dict[str, Any], str]:
        """按（规则、批次、越界区间）去重：同组合只保留一条告警。

        已确认的告警彻底静默，不再计数也不再提醒；未确认的只更新触发信息，
        不新建条目，避免同一批次反复打扰值班同事。
        """
        key = (sample.get("规则编号"), sample.get("批次"), interval)
        for alert in self._alerts():
            if (alert.get("规则编号"), alert.get("批次"), alert.get("越界区间")) != key:
                continue
            if alert.get("确认状态") == ACK_DONE:
                return alert, "已确认抑制"
            alert["触发次数"] = int(alert.get("触发次数", 0) or 0) + 1
            alert["最近触发时间"] = sample.get("采样时间")
            return alert, "合并既有告警"
        self._alert_seq += 1
        alert = {
            "id": max((int(row.get("id", 0)) for row in self._alerts()), default=0) + 1,
            "告警编号": f"MON-{self._alert_seq:04d}",
            "规则编号": sample.get("规则编号"),
            "指标编号": sample.get("指标编号"),
            "批次": sample.get("批次"),
            "越界区间": interval,
            "命中版本": version.get("版本编号"),
            "首次触发时间": sample.get("采样时间"),
            "最近触发时间": sample.get("采样时间"),
            "触发次数": 1,
            "确认状态": ACK_OPEN,
            "确认人": "",
            "确认时间": "",
            "有效确认次数": 0,
            "status": "未确认",
            "pending": True,
            "abnormal": True,
        }
        self._alerts().append(alert)
        return alert, "新建告警"

    def list_alerts(
        self, rule_code: str | None = None, metric_code: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            self.bootstrap()
            rows = self._alerts()
            if rule_code:
                rows = [row for row in rows if row.get("规则编号") == rule_code]
            if metric_code:
                rows = [row for row in rows if row.get("指标编号") == metric_code]
            return sorted(rows, key=lambda row: str(row.get("首次触发时间", "")))

    def ack_alert(self, alert_id: int, operator: str) -> tuple[dict[str, Any] | None, str, bool]:
        """确认告警：幂等且并发安全。

        锁内完成检查与状态转换，并发提交只有一次有效；重复提交返回成功并
        说明按幂等处理，已确认区间不会再次提醒。
        """
        with self._lock:
            self.bootstrap()
            alert = next((row for row in self._alerts() if int(row.get("id", 0)) == alert_id), None)
            if alert is None:
                return None, f"告警 {alert_id} 不存在或已清理", False
            if alert.get("确认状态") == ACK_DONE:
                return alert, (
                    f"告警 {alert['告警编号']} 此前已确认，本次提交按幂等处理，状态不变"
                ), True
            alert["确认状态"] = ACK_DONE
            alert["确认人"] = operator or "质量值班"
            alert["确认时间"] = _now_text()
            alert["有效确认次数"] = int(alert.get("有效确认次数", 0) or 0) + 1
            alert["status"] = "已确认"
            alert["pending"] = False
            alert["abnormal"] = False
            return alert, f"告警 {alert['告警编号']} 已确认，该区间不再重复提醒", True

    # ---------- 跨页面共享结论 ----------
    def conclusion_for_rule(self, rule_code: str) -> dict[str, Any]:
        """质量页与监控页共用的阈值生效结论，两边读取同一份结果。"""
        with self._lock:
            self.bootstrap()
            chosen, covered = self._resolve_locked(rule_code, _now_text())
            rule_alerts = [row for row in self._alerts() if row.get("规则编号") == rule_code]
            open_alerts = [row for row in rule_alerts if row.get("确认状态") != ACK_DONE]
            latest_open = max(open_alerts, key=lambda row: str(row.get("首次触发时间", "")), default=None)
            breach_samples = [
                row
                for row in self._samples()
                if row.get("规则编号") == rule_code and str(row.get("判定结果", "")).startswith("越")
            ]
            latest_breach = max(breach_samples, key=lambda row: str(row.get("采样时间", "")), default=None)
            if open_alerts:
                ack_status = ACK_OPEN
            elif rule_alerts:
                ack_status = ACK_DONE
            else:
                ack_status = "无告警"
            if latest_open is not None:
                interval = str(latest_open.get("越界区间", "—"))
            elif latest_breach is not None:
                interval = str(latest_breach.get("越界区间", "—"))
            else:
                interval = "—"
            if chosen is not None:
                verdict = f"{chosen['版本编号']} 生效中（{chosen['生效时间']} 起）"
                if covered:
                    verdict += f"；{'、'.join(str(row['版本编号']) for row in covered)} 留档核对"
            else:
                verdict = "无已复核版本，采样不触发阻断"
            return {
                "阈值版本": chosen["版本编号"] if chosen is not None else "—",
                "越界区间": interval,
                "确认状态": ack_status,
                "阈值生效结论": verdict,
                "告警ID": latest_open.get("id") if latest_open is not None else None,
                "告警编号": latest_open.get("告警编号", "") if latest_open is not None else "",
            }

    def conclusion_for_metric(self, metric_code: str) -> dict[str, Any]:
        """监控指标按其关联规则取同一份结论；没有关联规则时给出空口径。"""
        with self._lock:
            self.bootstrap()
            metric = next(
                (row for row in store.rows("monitor") if row.get("指标编号") == metric_code), None
            )
            rule_code = str((metric or {}).get("关联规则") or "").strip()
            if not rule_code:
                return {
                    "阈值版本": "—",
                    "越界区间": "—",
                    "确认状态": "无告警",
                    "阈值生效结论": "未关联质量规则",
                    "告警ID": None,
                    "告警编号": "",
                }
            return self.conclusion_for_rule(rule_code)

    # ---------- 内部 ----------
    def _find_rule(self, rule_code: str) -> dict[str, Any] | None:
        for row in store.rows(QUALITY_MODULE):
            if row.get("规则编号") == rule_code:
                return row
        return None


threshold_governance = ThresholdGovernance()
