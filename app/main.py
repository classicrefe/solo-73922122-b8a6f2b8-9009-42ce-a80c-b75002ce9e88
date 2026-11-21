"""FastAPI 入口：路由、异常处理、对账 CSV、演示页面。"""
from __future__ import annotations

import csv
import io
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import reconciliation, services
from .database import init_db, read_connection
from .schemas import PackageIn, WaybillIn
from .services import DomainError


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="联运改单对账演示",
    description="原单录入、改单包整包切换（拆单/并单）、挂起续办、对账 CSV、账单核对",
    version="1.1.0",
    lifespan=lifespan,
)


@app.exception_handler(DomainError)
async def domain_error_handler(_, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.message}
    )


@app.exception_handler(reconciliation.BillFormatError)
async def bill_format_error_handler(
    _, exc: reconciliation.BillFormatError
) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(
        os.path.join(os.path.dirname(__file__), "static", "index.html")
    )


# --------------------------------------------------------------------------- #
# 原单
# --------------------------------------------------------------------------- #
@app.post("/api/waybills", status_code=201, tags=["waybills"])
def create_waybill(waybill: WaybillIn) -> dict:
    return services.create_waybill(waybill)


@app.get("/api/waybills", tags=["waybills"])
def list_waybills(
    status: str | None = Query(None, pattern="^(active|replaced)$"),
) -> list[dict]:
    return services.list_waybills(status)


@app.get("/api/stats", tags=["waybills"])
def get_stats() -> dict:
    return services.stats()


# --------------------------------------------------------------------------- #
# 改单包
# --------------------------------------------------------------------------- #
@app.post("/api/packages", tags=["packages"])
def submit_package(package: PackageIn) -> JSONResponse:
    view, replayed = services.submit_package(package)
    return JSONResponse(
        status_code=200 if replayed else 201,
        content={"replayed": replayed, "package": view},
    )


@app.get("/api/packages", tags=["packages"])
def list_packages(
    status: str | None = Query(
        None, pattern="^(applied|suspended|rejected)$"
    ),
) -> list[dict]:
    return services.list_packages(status)


# --------------------------------------------------------------------------- #
# 对账 CSV：仅当前有效单
# --------------------------------------------------------------------------- #
@app.get("/api/reconciliation.csv", tags=["reconciliation"])
def reconciliation_csv() -> StreamingResponse:
    rows = services.active_waybill_rows()
    buf = io.StringIO()
    # UTF-8 BOM，便于 Excel 直接打开中文表头
    buf.write("﻿")
    writer = csv.writer(buf)
    writer.writerow(["运单号", "货批号", "重量kg"])
    for r in rows:
        writer.writerow([r["waybill_no"], r["batch_no"], r["weight_kg"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                'attachment; filename="reconciliation_active.csv"'
            )
        },
    )


# --------------------------------------------------------------------------- #
# 承运方账单核对
# --------------------------------------------------------------------------- #
MAX_BILL_BYTES = 5 * 1024 * 1024


@app.get("/api/bill-checks/sample.csv", tags=["bill-review"])
def sample_bill_csv() -> StreamingResponse:
    """下载演示用账单样例：正常申报全部有效货批，再把首行重复申报一次。"""
    with read_connection() as conn:
        rows = conn.execute(
            "SELECT w.waybill_no, b.batch_no, b.weight_kg "
            "FROM waybills w JOIN batches b ON b.waybill_id = w.id "
            "WHERE w.status = 'active' ORDER BY w.waybill_no, b.batch_no "
            "LIMIT 50"
        ).fetchall()
    buf = io.StringIO()
    buf.write("﻿")
    writer = csv.writer(buf)
    writer.writerow(["运单号", "货批号", "计费公斤数"])
    for r in rows:
        writer.writerow([r["waybill_no"], r["batch_no"], r["weight_kg"]])
    if rows:
        # 把首行再申报一次，演示“同一原单货批被两行共同覆盖”
        writer.writerow([rows[0]["waybill_no"], rows[0]["batch_no"],
                         rows[0]["weight_kg"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                'attachment; filename="bill_sample.csv"'
            )
        },
    )


@app.post("/api/bill-checks", tags=["bill-review"])
async def check_bill(file: UploadFile = File(...)) -> JSONResponse:
    """上传承运方账单 CSV，按同一台账快照核对并返回逐行结论。"""
    if file.filename and not file.filename.lower().endswith(".csv"):
        raise reconciliation.BillFormatError("仅支持 .csv 文件")
    data = await file.read(MAX_BILL_BYTES + 1)
    if len(data) > MAX_BILL_BYTES:
        raise reconciliation.BillFormatError(
            f"文件超过 {MAX_BILL_BYTES // 1024 // 1024}MB 上限"
        )
    # 解析与核对是同步 CPU/IO 工作，放到线程池，避免阻塞事件循环
    result = await run_in_threadpool(
        reconciliation.verify_bill, data, filename=file.filename or ""
    )
    return JSONResponse(content=result)
