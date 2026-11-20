"""FastAPI application: dangerous-goods custody chain API."""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from . import config, services
from .database import connect, init_db
from .schemas import (
    BatchResponse,
    BatchSubmit,
    ChainResponse,
    PlanReplace,
    PlanReplaceResponse,
    PlanView,
    Reconciliation,
    WaybillCreate,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> None:
    init_db()
    yield


app = FastAPI(
    title="空运危险品保管链 API",
    description=(
        "运单保管链（扫描交接 / 撤回 / 补报 / 更正）的修订裁决与可信前缀查询。"
        "同 ID 仅最高修订生效；同修订异文整批拒绝；失败保留旧链。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def get_conn() -> sqlite3.Connection:  # type: ignore[return]
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Error mapping: domain errors never leak 500s, and failed batches have
# already been rolled back by the service layer.
# --------------------------------------------------------------------------- #
@app.exception_handler(services.DomainConflict)
async def conflict_handler(_: Request, exc: services.DomainConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "conflict", "detail": str(exc)})


@app.exception_handler(services.DomainValidation)
async def not_found_handler(_: Request, exc: services.DomainValidation) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})


@app.exception_handler(ValidationError)
async def pydantic_handler(_: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "validation_error", "detail": exc.errors()})


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.post("/api/waybills", status_code=201, tags=["waybills"])
def create_waybill(
    body: WaybillCreate, conn: sqlite3.Connection = Depends(get_conn)
) -> dict:
    """创建运单并固定其起始保管方。"""
    return services.create_waybill(conn, body)


@app.get("/api/waybills", tags=["waybills"])
def list_waybills(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    rows = conn.execute(
        "SELECT id, origin_party, created_at FROM waybills ORDER BY id"
    ).fetchall()
    return {"waybills": [dict(r) for r in rows]}


@app.post(
    "/api/waybills/{waybill_id}/scans/batch",
    response_model=BatchResponse,
    tags=["scans"],
)
def submit_batch(
    waybill_id: str,
    batch: BatchSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> BatchResponse:
    """批量提交交接/撤回。

    * HANDOVER：scan_id、revision、ts_utc（带时区）、giver、receiver
    * WITHDRAWAL：仅 scan_id、revision（必须高于已知最高修订）
    * 同修订异文、过期修订、撤回未知 ID -> 409，整批回滚，旧链保留
    """
    return services.apply_batch(conn, waybill_id, batch)


@app.get(
    "/api/waybills/{waybill_id}/chain",
    response_model=ChainResponse,
    tags=["scans"],
)
def query_chain(
    waybill_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> ChainResponse:
    """返回可信连续交接前缀、首处断链原因、当前可确认保管方及计划核对结果。"""
    return services.compute_chain(conn, waybill_id)


# --------------------------------------------------------------------------- #
# Handover plan (registered expectations) and reconciliation queries
# --------------------------------------------------------------------------- #
@app.put(
    "/api/waybills/{waybill_id}/plan",
    response_model=PlanReplaceResponse,
    tags=["plan"],
)
def put_plan(
    waybill_id: str,
    body: PlanReplace,
    conn: sqlite3.Connection = Depends(get_conn),
) -> PlanReplaceResponse:
    """按运单登记或整版替换交接计划（最多 60 项）。

    成功回执带上本次提交的计划版号与核对结果；版号须严格递增，
    过期替换返回 409 且保留原计划。核对结果与本次提交后的扫描状态一致。
    """
    return services.replace_plan(conn, waybill_id, body)


@app.post(
    "/api/waybills/{waybill_id}/plan",
    response_model=PlanReplaceResponse,
    include_in_schema=False,
)
def post_plan(
    waybill_id: str,
    body: PlanReplace,
    conn: sqlite3.Connection = Depends(get_conn),
) -> PlanReplaceResponse:
    """Alias of PUT /plan for clients that only issue POST."""
    return services.replace_plan(conn, waybill_id, body)


@app.get(
    "/api/waybills/{waybill_id}/plan",
    response_model=PlanView,
    tags=["plan"],
)
def get_plan(
    waybill_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> PlanView:
    """读取当前生效的交接计划；未登记时 revision 为 null、items 为空。"""
    plan = services.get_plan(conn, waybill_id)
    if plan is None:
        return PlanView(waybill_id=waybill_id, revision=None, items=[])
    return plan


@app.get(
    "/api/waybills/{waybill_id}/reconciliation",
    response_model=Reconciliation,
    tags=["plan"],
)
def query_reconciliation(
    waybill_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> Reconciliation:
    """计划与可信扫描前缀的核对结果（同一已提交快照）。"""
    return services.reconcile(conn, waybill_id)


# Common path aliases so clients may register/query a plan under any of the
# intuitive resource names.  They share the same semantics and schema.
_PLAN_ALIASES = ("handover-plan", "handover_plan", "plans")


@app.put(
    "/api/waybills/{waybill_id}/{plan_resource}",
    response_model=PlanReplaceResponse,
    include_in_schema=False,
)
def put_plan_alias(
    waybill_id: str,
    plan_resource: str,
    body: PlanReplace,
    conn: sqlite3.Connection = Depends(get_conn),
) -> PlanReplaceResponse:
    if plan_resource not in _PLAN_ALIASES:
        raise services.DomainValidation(f"unknown resource {plan_resource!r}")
    return services.replace_plan(conn, waybill_id, body)


@app.post(
    "/api/waybills/{waybill_id}/{plan_resource}",
    response_model=PlanReplaceResponse,
    include_in_schema=False,
)
def post_plan_alias(
    waybill_id: str,
    plan_resource: str,
    body: PlanReplace,
    conn: sqlite3.Connection = Depends(get_conn),
) -> PlanReplaceResponse:
    if plan_resource not in _PLAN_ALIASES:
        raise services.DomainValidation(f"unknown resource {plan_resource!r}")
    return services.replace_plan(conn, waybill_id, body)


@app.get(
    "/api/waybills/{waybill_id}/{plan_resource}",
    response_model=PlanView,
    include_in_schema=False,
)
def get_plan_alias(
    waybill_id: str,
    plan_resource: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> PlanView:
    if plan_resource not in _PLAN_ALIASES:
        raise services.DomainValidation(f"unknown resource {plan_resource!r}")
    plan = services.get_plan(conn, waybill_id)
    if plan is None:
        return PlanView(waybill_id=waybill_id, revision=None, items=[])
    return plan


# --------------------------------------------------------------------------- #
# Minimal console (the interactive /docs remains fully functional too).
# --------------------------------------------------------------------------- #
_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")
