"""pytest 夹具：每个测试用独立临时 SQLite 库。"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    # database.get_db_path() 惰性读取环境变量，设置后所有连接都指向临时库
    monkeypatch.setenv("WAYBILL_DB_PATH", db_path)
    from app.database import init_db

    init_db(db_path)
    yield db_path


@pytest.fixture()
def client(temp_db):
    from app.main import app

    with TestClient(app) as c:
        yield c
