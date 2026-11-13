"""质量探针接口：维护质量规则，覆盖启动校验、暂停校验、归档规则等动作。

阈值版本走准入复核链路：提交 → 复核 → 生效；采样留档与告警确认状态
与监控页共享同一份结论。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.quality import QualityService

router = APIRouter(prefix="/api/quality", tags=["质量探针"])

service = QualityService()

LIST_FIELDS = ["规则编号", "表名称", "校验字段", "校验类型", "阈值上界", "阈值下界", "阻断级别", "最近校验"]
STATUSES = ["草稿", "运行中", "已暂停", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已暂停、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤质量探针列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出质量探针清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "quality", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条质量规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"质量规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条质量规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="质量规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条质量规则执行启动校验、暂停校验、归档规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/thresholds")
def list_thresholds(entry_id: int) -> dict[str, Any]:
    """列出规则的阈值版本：生效中、待复核与被覆盖留档的版本都在，供核对。"""
    versions, message = service.list_thresholds(entry_id)
    if versions is None:
        raise HTTPException(status_code=404, detail=message)
    return {"items": versions, "total": len(versions)}


@router.post("/{entry_id}/thresholds", response_model=ActionResult)
def submit_threshold(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交新阈值版本：先进入准入复核，复核通过并到达生效时间后才影响新读数。"""
    version, message = service.submit_threshold(entry_id, payload.values)
    if version is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=version)


@router.post("/{entry_id}/thresholds/{version_id}/review", response_model=ActionResult)
def review_threshold(entry_id: int, version_id: int, payload: EntryPayload) -> ActionResult:
    """复核阈值版本：approve=true 通过、false 驳回；不完整版本复核不通过且保持原版本生效。"""
    approve = bool(payload.values.get("approve", True))
    reviewer = str(payload.values.get("reviewer") or "质量值班")
    version, message, ok = service.review_threshold(version_id, approve, reviewer)
    if version is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=ok, message=message, entry=version)


@router.get("/{entry_id}/samples")
def list_samples(entry_id: int) -> dict[str, Any]:
    """采样留档：每条读数记录判定时刻命中的阈值版本，历史读数不随阈值切换重判。"""
    samples, message = service.list_samples(entry_id)
    if samples is None:
        raise HTTPException(status_code=404, detail=message)
    return {"items": samples, "total": len(samples)}
