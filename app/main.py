"""FastAPI 入口：路由、异常处理、对账 CSV、演示页面。"""
from __future__ import annotations

import csv
import io
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import services
from .database import init_db
from .schemas import PackageIn, WaybillIn
from .services import DomainError


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="联运改单对账演示",
    description="原单录入、改单包整包切换（拆单/并单）、挂起续办与对账 CSV",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(DomainError)
async def domain_error_handler(_, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.message}
    )


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
