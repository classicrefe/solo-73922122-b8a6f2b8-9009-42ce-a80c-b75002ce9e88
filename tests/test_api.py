"""API 层测试：HTTP 状态码、CSV、并发请求与页面可访问。"""
from __future__ import annotations

import threading


def _post_waybill(client, no: str, batches: dict[str, int]):
    return client.post(
        "/api/waybills",
        json={
            "waybill_no": no,
            "batches": [
                {"batch_no": k, "weight_kg": v} for k, v in batches.items()
            ],
        },
    )


def _post_package(client, no: str, old: list[str], new: dict[str, dict]):
    return client.post(
        "/api/packages",
        json={
            "package_no": no,
            "old_waybill_nos": old,
            "new_waybills": [
                {
                    "waybill_no": n,
                    "batches": [
                        {"batch_no": k, "weight_kg": v} for k, v in b.items()
                    ],
                }
                for n, b in new.items()
            ],
        },
    )


def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "联运改单".encode() in r.content


def test_full_flow_over_http(client):
    # 录入两张原单
    assert _post_waybill(client, "W1", {"B1": 100, "B2": 200}).status_code == 201
    assert _post_waybill(client, "W2", {"B3": 300}).status_code == 201
    # 重复录入 409
    assert _post_waybill(client, "W1", {"B1": 1}).status_code == 409
    # 结构错误 422（FastAPI 标准请求校验错误；重量为 0）
    bad = client.post(
        "/api/waybills",
        json={"waybill_no": "W9", "batches": [{"batch_no": "B", "weight_kg": 0}]},
    )
    assert bad.status_code == 422

    # 拆单：W1 -> N1/N2
    r = _post_package(client, "P1", ["W1"],
                      {"N1": {"B1": 100}, "N2": {"B2": 200}})
    assert r.status_code == 201
    assert r.json()["package"]["status"] == "applied"

    # 挂起：缺 W4
    r = _post_package(client, "P2", ["W2", "W4"],
                      {"N3": {"B3": 300, "B4": 40}})
    assert r.status_code == 201
    assert r.json()["package"]["status"] == "suspended"

    # 同包号同内容重传 -> 200 且 replayed
    r2 = _post_package(client, "P2", ["W2", "W4"],
                       {"N3": {"B3": 300, "B4": 40}})
    assert r2.status_code == 200 and r2.json()["replayed"] is True

    # 同包号异内容 -> 409 拒收
    r3 = _post_package(client, "P2", ["W2"], {"N3": {"B3": 300}})
    assert r3.status_code == 409

    # 补齐 W4 -> 自动续办
    assert _post_waybill(client, "W4", {"B4": 40}).status_code == 201
    pkgs = {p["package_no"]: p for p in client.get("/api/packages").json()}
    assert pkgs["P2"]["status"] == "applied"

    # 引用已替换旧单 -> 拒收
    r4 = _post_package(client, "P3", ["W1"], {"NX": {"B1": 100, "B2": 200}})
    assert r4.json()["package"]["status"] == "rejected"


def test_reconciliation_csv_only_active(client):
    _post_waybill(client, "W1", {"B1": 100, "B2": 200})
    _post_package(client, "P1", ["W1"], {"N1": {"B1": 100}, "N2": {"B2": 200}})
    r = client.get("/api/reconciliation.csv")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8-sig")
    lines = [ln for ln in text.strip().split("\r\n") if ln]
    assert lines[0] == "运单号,货批号,重量kg"
    data = lines[1:]
    # 仅含有效单 N1/N2，不含已替换的 W1
    assert any(ln.startswith("N1,") for ln in data)
    assert any(ln.startswith("N2,") for ln in data)
    assert not any(ln.startswith("W1,") for ln in data)
    assert "100" in text and "200" in text


def test_concurrent_package_requests(client):
    """并行两个替换同一旧单的请求：仅一个生效。"""
    _post_waybill(client, "W1", {"B1": 10})
    outcomes: list[dict] = []
    lock = threading.Lock()

    def worker(no: str, new_no: str) -> None:
        r = _post_package(client, no, ["W1"], {new_no: {"B1": 10}})
        with lock:
            outcomes.append(r.json())

    t1 = threading.Thread(target=worker, args=("P1", "N1"))
    t2 = threading.Thread(target=worker, args=("P2", "N2"))
    t1.start(); t2.start(); t1.join(); t2.join()

    statuses = sorted(o["package"]["status"] for o in outcomes)
    assert statuses == ["applied", "rejected"]
    actives = [
        w["waybill_no"]
        for w in client.get("/api/waybills?status=active").json()
    ]
    assert actives in (["N1"], ["N2"])


def test_stats_endpoint(client):
    _post_waybill(client, "W1", {"B1": 10})
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["active_waybills"] == 1
    assert body["active_total_weight_kg"] == 10
    assert set(body["packages"]) == {"applied", "suspended", "rejected"}
