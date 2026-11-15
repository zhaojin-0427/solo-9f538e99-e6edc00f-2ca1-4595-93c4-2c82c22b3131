"""FastAPI entry point for the lineage impact analysis service."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import service
from .db import init_db
from .exceptions import LineageError
from .models import (
    GraphResponse,
    ImpactRequest,
    ImpactResponse,
    PartitionBatchRequest,
    PartitionBatchResponse,
    ReadinessRequest,
    ReadinessResponse,
    RegisterRequest,
    RegisterResponse,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Reads db.DEFAULT_DB_PATH at startup so tests can override it.
    init_db()
    yield


app = FastAPI(
    title="数据血缘影响分析 API",
    description=(
        "批量登记源字段 / 派生字段 / 报表列依赖，提交拟下线字段后递归分析"
        "确实失效的报表列及各候选组缺失依赖。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(LineageError)
async def _lineage_error_handler(
    _request: Request, exc: LineageError
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    )


@app.exception_handler(sqlite3.OperationalError)
async def _sqlite_error_handler(
    _request: Request, exc: sqlite3.OperationalError
) -> JSONResponse:
    if "locked" in str(exc).lower():
        return JSONResponse(
            status_code=503,
            content={
                "error": "database_busy",
                "message": "数据库正被登记事务占用，请重试",
            },
        )
    raise exc


@app.get("/health", tags=["meta"])
def health() -> dict:
    from .db import DEFAULT_DB_PATH

    return {"status": "ok", "db": DEFAULT_DB_PATH}


@app.post(
    "/lineage/register",
    response_model=RegisterResponse,
    tags=["lineage"],
    status_code=201,
)
def register(payload: RegisterRequest) -> dict:
    """批量登记 / 覆盖血缘；循环依赖或校验失败时整批不生效。"""
    return service.register_batch(payload.nodes)


@app.post(
    "/lineage/impact",
    response_model=ImpactResponse,
    tags=["lineage"],
)
def impact(payload: ImpactRequest) -> dict:
    """递归分析拟下线字段导致的失效报表列。"""
    return service.analyze_impact(payload.retiring)


@app.get("/lineage/graph", response_model=GraphResponse, tags=["lineage"])
def graph() -> dict:
    """查看当前已提交的完整血缘快照。"""
    return service.get_graph()


# --------------------------------------------------------------------------- #
# Partition readiness
# --------------------------------------------------------------------------- #

@app.post(
    "/readiness/partitions",
    response_model=PartitionBatchResponse,
    tags=["readiness"],
)
def record_partitions(payload: PartitionBatchRequest) -> dict:
    """批量记录源字段分区在某业务日期的发布 / 撤回状态与修订号。

    引用未登记或非源字段、同修订状态冲突时整批拒绝；低修订重传不回退，
    同修订同状态重传幂等。
    """
    return service.record_partitions(payload.records)


@app.post(
    "/readiness/check",
    response_model=ReadinessResponse,
    tags=["readiness"],
)
def readiness(payload: ReadinessRequest) -> dict:
    """按候选输入组判断报表列在业务日期是否存在完整可用路径。"""
    return service.check_readiness(payload.report_column, payload.biz_date)
