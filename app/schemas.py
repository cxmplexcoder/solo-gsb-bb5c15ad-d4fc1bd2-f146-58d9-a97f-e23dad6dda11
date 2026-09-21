"""API 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from . import severity as sev


class EventIn(BaseModel):
    source: str = Field(min_length=1, max_length=256, examples=["edge-gateway-01"])
    event_id: str = Field(min_length=1, max_length=256, examples=["evt-0001"])
    occurred_at: datetime = Field(examples=["2026-09-20T08:00:00Z"])
    severity: str = Field(examples=["warning"])

    @field_validator("severity")
    @classmethod
    def _normalize_severity(cls, v: str) -> str:
        try:
            return sev.normalize(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("occurred_at")
    @classmethod
    def _assume_utc(cls, v: datetime) -> datetime:
        # 无时区的时间按 UTC 处理
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v


class SegmentOut(BaseModel):
    id: int
    source: str
    started_at: datetime
    ended_at: datetime
    max_severity: str
    event_count: int
    revision: int


class IngestResponse(BaseModel):
    deduplicated: bool
    segment: SegmentOut


class EventOut(BaseModel):
    """事件审计视图：事件四要素 + 当前所属 segment_id。

    segment_id 为查询时刻的归属（撤回/合并重算后可能变化），随快照实时返回。
    """

    source: str
    event_id: str
    occurred_at: datetime
    severity: str
    segment_id: int


class EventsPage(BaseModel):
    """事件审计一页；next_after 为不透明游标，作为下一页的 after 继续拉取。

    空页时 next_after 与入参 after 相同（首页无 after 时为 null），游标保持不变。
    """

    events: list[EventOut]
    next_after: str | None = None


class WithdrawResponse(BaseModel):
    """误报事件撤回结果。

    - ``deleted_segment``：原段仅含该事件，段随之删除，``segments`` 为空；
    - ``shrunk_segment``：移除后原段未拆开（边界/最高级别可能重算），
      保留段沿用原 id 且 revision 递增，``segments`` 含 1 个段；
    - ``split_segment``：桥接事件移除导致原段拆成多段，最早子段沿用原 id
      （revision 递增），其余子段新建（revision=1），按时间序全部返回。
    """

    source: str
    event_id: str
    result: Literal["deleted_segment", "shrunk_segment", "split_segment"]
    segments: list[SegmentOut]


class SegmentChange(BaseModel):
    """一条段变更记录。

    - upsert：段创建或边界/最高级别变化，segment 为变更时刻的完整快照；
    - merged：墓碑，segment_id 段已被删除并并入 merged_into 指向的保留段；
    - deleted：墓碑，segment_id 段因其唯一事件被撤回而删除（无并入目标）。
    """

    change_id: int
    type: Literal["upsert", "merged", "deleted"]
    segment_id: int
    merged_into: int | None = None
    revision: int | None = None
    segment: SegmentOut | None = None


class SegmentChangesPage(BaseModel):
    """变更流一页；next_after 作为下一页的 after 继续拉取。"""

    changes: list[SegmentChange]
    next_after: int


class PreviewSegmentOut(BaseModel):
    """按候选静默间隔重算出的模拟段；temp_id 仅本次预演响应内有效。"""

    temp_id: int
    source: str
    started_at: datetime
    ended_at: datetime
    max_severity: str
    event_count: int


class SegmentMappingOut(BaseModel):
    """单个现有段到模拟段的映射，依据事件成员关系判定：

    - unchanged：该现有段的事件与唯一一个模拟段完全相同（一对一）；
    - split：该现有段的事件分散到多个模拟段（一对多，候选间隔更小）；
    - merged：多个现有段的事件并入同一个模拟段（多对一，候选间隔更大）。
    """

    segment_id: int
    relation: Literal["unchanged", "split", "merged"]
    simulated_temp_ids: list[int]
    event_count: int


class ResegmentationSummary(BaseModel):
    changed_existing_segment_count: int
    split_existing_segment_count: int
    merged_existing_segment_count: int
    unchanged_existing_segment_count: int
    existing_segment_count: int
    simulated_segment_count: int


class ResegmentationPreviewResponse(BaseModel):
    """静默间隔重分段预演结果：纯读，不改变任何事件、现有段与变更流。"""

    source: str
    silence_interval_seconds: float
    event_count: int
    simulated_segments: list[PreviewSegmentOut]
    mappings: list[SegmentMappingOut]
    summary: ResegmentationSummary


class AcknowledgementIn(BaseModel):
    """段人工确认请求（乐观锁 + 幂等）。"""

    operator: str = Field(
        min_length=1, max_length=256, examples=["ops-oncall-01"]
    )
    note: str = Field(
        min_length=1, max_length=4096, examples=["已电话联系现场确认，重启处理中"]
    )
    expected_revision: int = Field(
        ge=1, examples=[2], description="客户端读取段时持有的 revision"
    )
    idempotency_key: str = Field(
        min_length=1, max_length=512, examples=["ack-20260920-0001"]
    )


class AcknowledgementOut(BaseModel):
    """一条不可变的人工确认记录。

    seq 为段内确认序号（从 1 起、按段独立）；expected_revision 是操作员断言
    的 revision，acknowledged_revision 是确认落库时段的实际 revision（校验通过时
    二者相等）；idempotency_key 重放同一请求时返回的仍是同一条记录。
    """

    ack_id: int
    segment_id: int
    seq: int
    operator: str
    note: str
    expected_revision: int
    acknowledged_revision: int
    idempotency_key: str
    acknowledged_at: datetime


class AcknowledgementsPage(BaseModel):
    """段确认记录列表：按确认序号 seq 升序。"""

    segment_id: int
    acknowledgements: list[AcknowledgementOut]


class AcknowledgementResult(BaseModel):
    """POST 确认结果：replayed=False 为新建（201），True 为幂等重放（200/409）。

    同幂等键但载荷不同时 HTTP 状态码为 409，error 标明冲突类型，
    acknowledgement 为已落库的原记录（不可变，绝不覆盖）。
    """

    replayed: bool
    error: str | None = None
    acknowledgement: AcknowledgementOut
