"""账单核对：解析、快照一致性、逐行判定、重复识别。"""
from __future__ import annotations

import csv
import io
import json

import pytest

from app import reconciliation, services
from app.reconciliation import BillFormatError
from app.schemas import BatchIn, NewWaybillIn, PackageIn, WaybillIn


def wb(no: str, batches: dict[str, int]) -> WaybillIn:
    return WaybillIn(
        waybill_no=no,
        batches=[BatchIn(batch_no=k, weight_kg=v) for k, v in batches.items()],
    )


def pkg(no: str, old: list[str], new: dict[str, dict[str, int]]) -> PackageIn:
    return PackageIn(
        package_no=no,
        old_waybill_nos=old,
        new_waybills=[
            NewWaybillIn(
                waybill_no=n,
                batches=[BatchIn(batch_no=k, weight_kg=v) for k, v in b.items()],
            )
            for n, b in new.items()
        ],
    )


def bill(*rows: tuple[str, str, object], header=("运单号", "货批号", "计费公斤数")) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode()


def by_line(result: dict) -> dict[int, dict]:
    return {r["line_no"]: r for r in result["rows"]}


# --------------------------------------------------------------------------- #
# 拆并后回追
# --------------------------------------------------------------------------- #
def test_split_new_waybills_trace_to_original(temp_db):
    services.create_waybill(wb("W1", {"B1": 100, "B2": 200}))
    services.submit_package(pkg("P1", ["W1"], {"N1": {"B1": 100}, "N2": {"B2": 200}}))

    result = reconciliation.verify_bill(bill(("N1", "B1", 100), ("N2", "B2", 200)))
    rows = by_line(result)
    assert result["summary"]["passed"] == 2
    assert result["summary"]["manual_review"] == 0
    assert rows[2]["original_batch"] == {
        "waybill_no": "W1", "batch_no": "B1", "weight_kg": 100,
    }
    assert [t["waybill_no"] for t in rows[2]["trace"]] == ["W1", "N1"]
    assert rows[2]["trace"][1]["via_package_no"] == "P1"
    assert rows[2]["waybill_status"] == "active"


def test_old_and_new_bill_lines_both_trace_and_are_duplicates(temp_db):
    """承运方按旧单 + 新单各计一次费：两行都追到同一原单货批。"""
    services.create_waybill(wb("W1", {"B1": 100}))
    services.submit_package(pkg("P1", ["W1"], {"N1": {"B1": 100}}))

    result = reconciliation.verify_bill(bill(("W1", "B1", 100), ("N1", "B1", 100)))
    rows = by_line(result)
    assert rows[2]["waybill_status"] == "replaced"
    assert rows[3]["waybill_status"] == "active"
    for line_no in (2, 3):
        assert rows[line_no]["status"] == "suspected_duplicate"
        assert rows[line_no]["original_batch"]["waybill_no"] == "W1"
        assert rows[line_no]["original_batch"]["batch_no"] == "B1"
    assert rows[2]["duplicate_with"] == [
        {"line_no": 3, "waybill_no": "N1", "batch_no": "B1"}
    ]
    assert rows[3]["duplicate_with"] == [
        {"line_no": 2, "waybill_no": "W1", "batch_no": "B1"}
    ]
    assert rows[2]["shared_original_batches"] == [
        {"waybill_no": "W1", "batch_no": "B1", "weight_kg": 100}
    ]

    assert result["summary"]["suspected_duplicate"] == 2
    assert result["summary"]["duplicate_groups"] == 1
    group = result["duplicate_groups"][0]
    assert group["line_numbers"] == [2, 3]
    assert group["original_waybill_no"] == "W1"
    assert group["original_batch_no"] == "B1"


def test_multi_level_split_chain_traced_to_root(temp_db):
    services.create_waybill(wb("W1", {"B1": 100, "B2": 200}))
    services.submit_package(pkg("P1", ["W1"], {"N1": {"B1": 100}, "N2": {"B2": 200}}))
    # 并单：N1 + N2 -> N9（顺序反过来也可）
    services.submit_package(pkg("P2", ["N2", "N1"], {"N9": {"B1": 100, "B2": 200}}))

    result = reconciliation.verify_bill(bill(("N9", "B1", 100), ("N9", "B2", 200)))
    rows = by_line(result)
    assert result["summary"]["passed"] == 2
    assert [t["waybill_no"] for t in rows[2]["trace"]] == ["W1", "N1", "N9"]
    assert [t["waybill_no"] for t in rows[3]["trace"]] == ["W1", "N2", "N9"]
    assert rows[2]["original_batch"]["batch_no"] == "B1"
    assert rows[3]["original_batch"]["batch_no"] == "B2"


def test_same_batch_no_on_unrelated_originals_pass_separately(temp_db):
    services.create_waybill(wb("W1", {"B1": 100}))
    services.create_waybill(wb("W2", {"B1": 50}))  # 与 W1 无关，但货批号同为 B1
    result = reconciliation.verify_bill(bill(("W1", "B1", 100), ("W2", "B1", 50)))
    rows = by_line(result)
    assert result["summary"]["passed"] == 2
    assert result["summary"]["suspected_duplicate"] == 0
    assert result["duplicate_groups"] == []
    assert rows[2]["original_batch"]["waybill_no"] == "W1"
    assert rows[3]["original_batch"]["waybill_no"] == "W2"


def test_same_batch_no_from_split_of_unrelated_originals_not_flagged(temp_db):
    """两批同号货批分别来自两张互不相关的原单，各自拆单后仍可分别通过。"""
    services.create_waybill(wb("W1", {"B1": 100}))
    services.create_waybill(wb("W2", {"B1": 100}))
    services.submit_package(pkg("P1", ["W1"], {"N1": {"B1": 100}}))
    services.submit_package(pkg("P2", ["W2"], {"N2": {"B1": 100}}))
    result = reconciliation.verify_bill(bill(("N1", "B1", 100), ("N2", "B1", 100)))
    assert result["summary"]["passed"] == 2
    assert result["summary"]["suspected_duplicate"] == 0


# --------------------------------------------------------------------------- #
# 待人工核实
# --------------------------------------------------------------------------- #
def test_unknown_waybill_and_batch_are_manual_review(temp_db):
    services.create_waybill(wb("W1", {"B1": 100}))
    result = reconciliation.verify_bill(
        bill(("W1", "B9", 100), ("W404", "B1", 100), ("W1", "B1", 100))
    )
    rows = by_line(result)
    assert rows[2]["status"] == "manual_review"
    assert any("不存在货批" in r for r in rows[2]["reasons"])
    assert rows[3]["status"] == "manual_review"
    assert any("在台账中不存在" in r for r in rows[3]["reasons"])
    assert rows[4]["status"] == "passed"


def test_billed_weight_mismatch_is_manual_review(temp_db):
    services.create_waybill(wb("W1", {"B1": 100}))
    result = reconciliation.verify_bill(bill(("W1", "B1", 99)))
    row = result["rows"][0]
    assert row["status"] == "manual_review"
    assert any("不符" in r for r in row["reasons"])
    assert row["ledger_weight_kg"] == 100


def test_mismatched_row_is_not_used_for_duplicate_detection(temp_db):
    services.create_waybill(wb("W1", {"B1": 100}))
    services.submit_package(pkg("P1", ["W1"], {"N1": {"B1": 100}}))
    result = reconciliation.verify_bill(
        bill(("W1", "B1", 100), ("N1", "B1", 80))
    )
    rows = by_line(result)
    assert rows[2]["status"] == "passed"
    assert rows[3]["status"] == "manual_review"
    assert result["summary"]["duplicate_groups"] == 0


def test_invalid_billed_weight_is_manual_review(temp_db):
    services.create_waybill(wb("W1", {"B1": 100}))
    result = reconciliation.verify_bill(
        bill(
            ("W1", "B1", "abc"),
            ("W1", "B1", ""),
            ("W1", "B1", -5),
            ("W1", "B1", "100.0"),
            ("W1", "B1", "1_00"),
            ("W1", "B1", "1e2"),
            ("W1", "B1", "nan"),
        )
    )
    rows = by_line(result)
    for line_no in (2, 3, 4, 6, 7, 8):
        assert rows[line_no]["status"] == "manual_review"
        assert rows[line_no]["reasons"]
    # 100.0 与台账整数 100 等值，应通过
    assert rows[5]["status"] == "passed"


def test_billed_weight_unit_suffix_accepted(temp_db):
    services.create_waybill(wb("W1", {"B1": 100}))
    result = reconciliation.verify_bill(bill(("W1", "B1", "100 kg")))
    assert result["summary"]["passed"] == 1


def test_waybill_missing_from_package_payload_is_manual_review(temp_db):
    """已生效包引用的新单若在拆并记录中缺失，无法追溯 -> 待人工核实。"""
    services.create_waybill(wb("W1", {"B1": 100, "B2": 200}))
    services.submit_package(pkg("P1", ["W1"], {"N1": {"B1": 100}, "N2": {"B2": 200}}))
    # 直接篡改已生效包的 payload，模拟历史台账缺记录
    from app.database import write_transaction

    with write_transaction() as conn:
        row = conn.execute(
            "SELECT payload FROM packages WHERE package_no = 'P1'"
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["new_waybills"] = [
            w for w in payload["new_waybills"] if w["waybill_no"] != "N2"
        ]
        conn.execute(
            "UPDATE packages SET payload = ? WHERE package_no = 'P1'",
            (json.dumps(payload, ensure_ascii=False),),
        )

    result = reconciliation.verify_bill(bill(("N1", "B1", 100), ("N2", "B2", 200)))
    rows = by_line(result)
    assert rows[2]["status"] == "passed"
    assert rows[3]["status"] == "manual_review"
    assert any("无法追溯" in r for r in rows[3]["reasons"])


def test_split_total_weight_imbalance_is_manual_review(temp_db):
    """沿途某次拆并前后货批总重不等 -> 该行待人工核实，即使计费重量与现行台账相等。"""
    services.create_waybill(wb("W1", {"B1": 100}))
    services.submit_package(pkg("P1", ["W1"], {"N1": {"B1": 100}}))
    from app.database import write_transaction

    # 模拟拆并后新单台账被改成 90kg（包记录仍是 100kg）
    with write_transaction() as conn:
        conn.execute(
            "UPDATE batches SET weight_kg = 90 WHERE batch_no = 'B1' "
            "AND waybill_id = (SELECT id FROM waybills WHERE waybill_no = 'N1')"
        )

    result = reconciliation.verify_bill(bill(("N1", "B1", 90)))
    row = result["rows"][0]
    assert row["status"] == "manual_review"
    assert any("总重不等" in r for r in row["reasons"])
    assert any("拆并" in r for r in row["reasons"])
    assert row["original_batch"]["weight_kg"] == 100


def test_package_record_weight_differs_from_ledger_is_manual_review(temp_db):
    """包记录中的新单重量与台账新单重量不符 -> 待人工核实。"""
    services.create_waybill(wb("W1", {"B1": 100}))
    services.submit_package(pkg("P1", ["W1"], {"N1": {"B1": 100}}))
    from app.database import write_transaction

    with write_transaction() as conn:
        row = conn.execute(
            "SELECT payload FROM packages WHERE package_no = 'P1'"
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["new_waybills"][0]["batches"][0]["weight_kg"] = 95
        conn.execute(
            "UPDATE packages SET payload = ? WHERE package_no = 'P1'",
            (json.dumps(payload, ensure_ascii=False),),
        )

    result = reconciliation.verify_bill(bill(("N1", "B1", 100)))
    row = result["rows"][0]
    assert row["status"] == "manual_review"
    assert any("拆并记录重量" in r for r in row["reasons"])


def test_ambiguous_split_merge_is_manual_review(temp_db):
    """拆并记录把同号货批指向多张旧单 -> 无法唯一追溯，待人工核实。

    正常录入流程不允许这种包生效，这里直接构造已生效的异常包记录，
    验证核对端不会盲目挑一条路径。
    """
    services.create_waybill(wb("W1", {"B1": 60}))
    services.create_waybill(wb("W2", {"B1": 40}))
    from app.database import write_transaction

    payload = {
        "package_no": "PX",
        "old_waybill_nos": ["W1", "W2"],
        "new_waybills": [
            {"waybill_no": "N1", "batches": [{"batch_no": "B1", "weight_kg": 100}]}
        ],
    }
    with write_transaction() as conn:
        conn.execute(
            "INSERT INTO packages (package_no, payload, content_hash, status, "
            "reason, created_at, updated_at) VALUES ('PX', ?, 'x', 'applied', "
            "NULL, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (json.dumps(payload, ensure_ascii=False),),
        )
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "UPDATE waybills SET status = 'replaced', replaced_by_pkg = 'PX', "
            "replaced_at = ? WHERE waybill_no IN ('W1', 'W2')",
            (now,),
        )
        cur = conn.execute(
            "INSERT INTO waybills (waybill_no, status, source_package_no, "
            "created_at) VALUES ('N1', 'active', 'PX', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO batches (waybill_id, batch_no, weight_kg) "
            "VALUES (?, 'B1', 100)",
            (cur.lastrowid,),
        )

    result = reconciliation.verify_bill(bill(("N1", "B1", 100), ("W1", "B1", 60)))
    rows = by_line(result)
    assert rows[2]["status"] == "manual_review"
    assert any("无法唯一追溯" in r for r in rows[2]["reasons"])
    assert rows[3]["status"] == "passed"
    assert result["summary"]["duplicate_groups"] == 0


# --------------------------------------------------------------------------- #
# 快照一致性与格式
# --------------------------------------------------------------------------- #
def test_bill_is_checked_against_one_ledger_snapshot(temp_db, monkeypatch):
    """核对中途发生拆单，整份账单仍按核对开始时的台账判定。"""
    services.create_waybill(wb("W1", {"B1": 100}))
    original_load = reconciliation._load_ledger

    def load_then_mutate(conn):
        ledger = original_load(conn)
        # 模拟核对开始后另一个改单包生效：W1 -> N1
        services.submit_package(pkg("P1", ["W1"], {"N1": {"B1": 100}}))
        return ledger

    monkeypatch.setattr(reconciliation, "_load_ledger", load_then_mutate)
    result = reconciliation.verify_bill(bill(("W1", "B1", 100)))
    row = result["rows"][0]
    assert row["status"] == "passed"
    assert row["waybill_status"] == "active"
    assert result["ledger_state"]["waybill_count"] == 1
    assert result["ledger_state"]["applied_package_count"] == 0


def test_snapshot_token_is_stable_when_nothing_changes(temp_db):
    services.create_waybill(wb("W1", {"B1": 100}))
    first = reconciliation.verify_bill(bill(("W1", "B1", 100)))
    second = reconciliation.verify_bill(bill(("W1", "B1", 100)))
    assert first["ledger_state"]["token"] == second["ledger_state"]["token"]


def test_csv_header_aliases_and_bom(temp_db):
    services.create_waybill(wb("W1", {"B1": 100}))
    data = bill(("W1", "B1", 100), header=("waybill_no", "batch_no", "weight_kg"))
    result = reconciliation.verify_bill(b"\xef\xbb\xbf" + data)
    assert result["summary"]["passed"] == 1


def test_gb18030_csv_supported(temp_db):
    services.create_waybill(wb("W1", {"B1": 100}))
    text = "运单号,货批号,计费公斤数\nW1,B1,100\n"
    result = reconciliation.verify_bill(text.encode("gb18030"))
    assert result["summary"]["passed"] == 1


def test_missing_header_rejected(temp_db):
    with pytest.raises(BillFormatError) as exc:
        reconciliation.verify_bill("运单号,货批号\nW1,B1\n".encode())
    assert "计费公斤数" in str(exc.value)


def test_empty_and_header_only_files_rejected(temp_db):
    with pytest.raises(BillFormatError):
        reconciliation.verify_bill(b"")
    with pytest.raises(BillFormatError):
        reconciliation.verify_bill("运单号,货批号,计费公斤数\n".encode())


def test_blank_rows_skipped_and_line_numbers_preserved(temp_db):
    services.create_waybill(wb("W1", {"B1": 100}))
    data = (
        "运单号,货批号,计费公斤数\n\n"
        "W1,B1,100\n"
        "\n"
        "W1,B1,999\n"
    ).encode()
    result = reconciliation.verify_bill(data)
    assert [r["line_no"] for r in result["rows"]] == [3, 5]
    assert result["rows"][0]["status"] == "passed"
    assert result["rows"][1]["status"] == "manual_review"


def test_billed_weight_with_thousands_separator(temp_db):
    services.create_waybill(wb("W1", {"B1": 1200}))
    result = reconciliation.verify_bill(bill(("W1", "B1", "1,200")))
    assert result["summary"]["passed"] == 1
