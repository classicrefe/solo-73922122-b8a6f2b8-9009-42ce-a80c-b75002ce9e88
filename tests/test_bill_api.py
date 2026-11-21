"""账单核对 HTTP 接口：上载、错误处理、示例下载。"""
from __future__ import annotations

import csv
import io


def _bill(*rows, header=("运单号", "货批号", "计费公斤数")) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def _upload(client, data: bytes, filename: str = "bill.csv", field: str = "file"):
    return client.post(
        "/api/bill-checks",
        files={field: (filename, data, "text/csv")},
    )


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


def test_bill_check_over_http(client):
    _post_waybill(client, "W1", {"B1": 100, "B2": 200})
    _post_package(client, "P1", ["W1"], {"N1": {"B1": 100}, "N2": {"B2": 200}})

    data = _bill(
        ("N1", "B1", 100),
        ("N2", "B2", 200),
        ("W1", "B1", 100),
        ("W1", "B2", 200),
        ("W1", "B1", 100),
        ("W1", "B9", 200),
        ("N404", "B2", 200),
        ("N2", "B2", 199),
    )
    r = _upload(client, data)
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "bill.csv"
    assert body["summary"] == {
        "total": 8,
        "passed": 0,
        "manual_review": 3,
        "suspected_duplicate": 5,
        "duplicate_groups": 2,
    }
    groups = body["duplicate_groups"]
    assert [g["line_numbers"] for g in groups] == [[2, 4, 6], [3, 5]]
    assert groups[0]["original_waybill_no"] == "W1"
    assert groups[0]["original_batch_no"] == "B1"
    assert groups[1]["original_batch_no"] == "B2"
    assert body["rows"][0]["duplicate_with"] == [
        {"line_no": 4, "waybill_no": "W1", "batch_no": "B1"},
        {"line_no": 6, "waybill_no": "W1", "batch_no": "B1"},
    ]
    assert body["rows"][2]["shared_original_batches"] == [
        {"waybill_no": "W1", "batch_no": "B1", "weight_kg": 100}
    ]


def test_bill_check_upload_requires_csv_extension(client):
    _post_waybill(client, "W1", {"B1": 100})
    r = _upload(client, _bill(("W1", "B1", 100)), filename="bill.xlsx")
    assert r.status_code == 400
    assert "csv" in r.json()["detail"].lower()


def test_bill_check_bad_format_returns_400(client):
    r = _upload(client, b"foo,bar\n1,2\n")
    assert r.status_code == 400
    assert "表头" in r.json()["detail"]

    r = _upload(client, b"")
    assert r.status_code == 400


def test_bill_check_requires_file_field(client):
    r = client.post("/api/bill-checks")
    assert r.status_code == 422


def test_sample_bill_download(client):
    _post_waybill(client, "W1", {"B1": 100})
    _post_waybill(client, "W2", {"B9": 50})
    r = client.get("/api/bill-checks/sample.csv")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8-sig")
    lines = [ln for ln in text.strip().split("\r\n") if ln]
    assert lines[0] == "运单号,货批号,计费公斤数"
    assert len(lines) == 4  # 表头 + 两张有效货批 + 首行重复一次

    # 样例回传后：两条不同货批通过，首行与其重复申报被判疑似重复
    up = _upload(client, r.content)
    assert up.status_code == 200
    assert up.json()["summary"] == {
        "total": 3,
        "passed": 1,
        "manual_review": 0,
        "suspected_duplicate": 2,
        "duplicate_groups": 1,
    }


def test_page_contains_bill_upload_entry(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.content.decode("utf-8")
    assert "账单核对" in html
    assert "/api/bill-checks" in html
