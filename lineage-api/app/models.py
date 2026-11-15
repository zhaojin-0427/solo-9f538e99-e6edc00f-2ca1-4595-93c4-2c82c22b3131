"""Pydantic request / response models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

NodeKind = Literal["source", "derived", "report"]


class NodeIn(BaseModel):
    name: str = Field(..., min_length=1, description="全局唯一字段名")
    kind: NodeKind
    # 候选输入组：组内字段全部可用时该组可用；任一组可用即可产出本列。
    # source 节点必须为空或省略。
    candidate_groups: list[list[str]] = Field(default_factory=list)


class RegisterRequest(BaseModel):
    nodes: list[NodeIn] = Field(..., min_length=1)


class RegisterResponse(BaseModel):
    registered: int
    nodes: list[str]
    message: str


class ImpactRequest(BaseModel):
    retiring: list[str] = Field(
        ..., min_length=1, description="拟下线的字段名（任意类型节点）"
    )


class MissingDependency(BaseModel):
    name: str
    kind: NodeKind
    root_causes: list[str] = Field(
        description="导致该输入不可用的最深层退役字段"
    )


class FailedGroup(BaseModel):
    group_index: int
    inputs: list[str]
    missing: list[MissingDependency]


class FailedReportColumn(BaseModel):
    name: str
    failed_groups: list[FailedGroup]


class ImpactResponse(BaseModel):
    retiring: list[str]
    failed_report_columns: list[FailedReportColumn]
    unavailable_count: int
    unavailable_nodes: list[str] = Field(
        description="所有不可用节点（含派生列，便于递归排查）"
    )


class NodeOut(BaseModel):
    name: str
    kind: NodeKind
    candidate_groups: list[list[str]]


class GraphResponse(BaseModel):
    nodes: list[NodeOut]
    counts: dict[str, int]


# --------------------------------------------------------------------------- #
# Partition readiness
# --------------------------------------------------------------------------- #

PartitionStatus = Literal["published", "withdrawn"]


class PartitionRecord(BaseModel):
    field_name: str = Field(..., min_length=1, description="源字段名（须为已登记的 source 节点）")
    biz_date: str = Field(..., min_length=1, description="业务日期，如 2026-11-14")
    revision: int = Field(..., ge=1, description="修订号，从 1 开始递增")
    status: PartitionStatus


class PartitionBatchRequest(BaseModel):
    records: list[PartitionRecord] = Field(..., min_length=1)


class PartitionBatchResponse(BaseModel):
    received: int = Field(description="批次内记录条数（含重复键）")
    applied: int = Field(description="新写入的修订记录数")
    unchanged: int = Field(description="同修订同状态重传、未改变结果的条数")
    superseded: int = Field(description="低于当前最高修订、被忽略的条数")
    rejected: int = Field(description="整批拒绝时的问题记录条数")
    results: list[dict]
    message: str


class ReadinessRequest(BaseModel):
    report_column: str = Field(..., min_length=1)
    biz_date: str = Field(..., min_length=1)


class MissingPartition(BaseModel):
    name: str = Field(description="缺失分区的源字段名")
    status: Literal["unpublished", "withdrawn"]
    revision: int | None = Field(
        description="已撤回时生效的最高修订号；无记录时为 null"
    )


class BlockedGroup(BaseModel):
    group_index: int
    inputs: list[str]
    missing_partitions: list[MissingPartition] = Field(
        description="该组追溯到的缺失源字段分区（去重）"
    )


class ReadinessResponse(BaseModel):
    report_column: str
    biz_date: str
    ready: bool
    available_groups: list[int] = Field(
        description="存在完整可用路径的候选组下标"
    )
    blocked_groups: list[BlockedGroup] = Field(
        description="每个受阻候选组及其缺少的源字段分区"
    )
