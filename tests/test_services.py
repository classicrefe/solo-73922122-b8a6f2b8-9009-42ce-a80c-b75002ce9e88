"""服务层业务规则测试：拆并单、挂起续办、拒收、幂等、并发。"""
from __future__ import annotations

import threading

import pytest

from app import services
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


# --------------------------------------------------------------------------- #
def test_split_and_merge_roundtrip(temp_db):
    """一张原单拆成两张，再并回一张，逐货批重量始终相等。"""
    services.create_waybill(wb("W1", {"B1": 100, "B2": 200}))

    view, replayed = services.submit_package(
        pkg("P1", ["W1"], {"W1-A": {"B1": 100}, "W1-B": {"B2": 200}})
    )
    assert replayed is False
    assert view["status"] == "applied"

    actives = [w["waybill_no"] for w in services.list_waybills("active")]
    assert actives == ["W1-A", "W1-B"]
    old = services.get_waybill("W1")
    assert old["status"] == "replaced" and old["replaced_by_pkg"] == "P1"

    # 并单：两张有效单并回一张
    view2, _ = services.submit_package(
        pkg("P2", ["W1-A", "W1-B"], {"W1-M": {"B1": 100, "B2": 200}})
    )
    assert view2["status"] == "applied"
    actives = [w["waybill_no"] for w in services.list_waybills("active")]
    assert actives == ["W1-M"]
    assert services.stats()["active_total_weight_kg"] == 300


def test_suspended_then_auto_resume(temp_db):
    """旧单未到齐挂起；补齐原单后自动续办并生效。"""
    services.create_waybill(wb("W1", {"B1": 50}))
    view, _ = services.submit_package(
        pkg("P1", ["W1", "W2"], {"N1": {"B1": 50}})
    )
    assert view["status"] == "suspended"
    assert "W2" in view["reason"]

    # 补齐缺失旧单（W2 货批须与新单映射一致，否则续办时转拒收）
    services.create_waybill(wb("W2", {"B1": 50}))

    pkgs = {p["package_no"]: p for p in services.list_packages()}
    assert pkgs["P1"]["status"] == "applied"
    assert pkgs["P1"]["reason"] is None
    actives = [w["waybill_no"] for w in services.list_waybills("active")]
    assert "N1" in actives and "W1" not in actives and "W2" not in actives


def test_rejected_when_old_waybill_already_replaced(temp_db):
    services.create_waybill(wb("W1", {"B1": 10}))
    services.create_waybill(wb("W2", {"B2": 20}))
    services.submit_package(pkg("P1", ["W1"], {"N1": {"B1": 10}}))
    # W1 已被 P1 替换，P2 再引用 W1 -> 整包拒收
    view, _ = services.submit_package(
        pkg("P2", ["W1", "W2"], {"N2": {"B1": 10, "B2": 20}})
    )
    assert view["status"] == "rejected"
    assert "W1" in view["reason"]
    # W2 不受影响，仍然有效
    assert services.get_waybill("W2")["status"] == "active"


def test_suspended_package_rejected_on_resume_when_old_got_replaced(temp_db):
    """挂起中的包，续办时若发现旧单已被别的包替换 -> 转拒收。"""
    services.create_waybill(wb("W1", {"B1": 10}))
    # P1 挂起等待 W2
    services.submit_package(
        pkg("P1", ["W1", "W2"], {"N1": {"B1": 10, "B2": 5}})
    )
    # 先把 W1 用另一个包替换掉
    services.submit_package(pkg("P2", ["W1"], {"N2": {"B1": 10}}))
    # 再录入 W2 触发续办
    services.create_waybill(wb("W2", {"B2": 5}))
    pkgs = {p["package_no"]: p for p in services.list_packages()}
    assert pkgs["P1"]["status"] == "rejected"
    assert "替换" in pkgs["P1"]["reason"]
    # P2 的新单 N2 仍有效
    assert services.get_waybill("N2")["status"] == "active"


def test_existing_new_no_rejected(temp_db):
    """新单号与现存运单（含有效单）冲突 -> 拒收。"""
    services.create_waybill(wb("W1", {"B1": 10}))
    services.create_waybill(wb("W2", {"B1": 10}))
    view, _ = services.submit_package(pkg("P1", ["W1"], {"W2": {"B1": 10}}))
    assert view["status"] == "rejected"
    assert "W2" in view["reason"]


def test_reused_new_no_rejected_even_after_replacement(temp_db):
    """新单号“从未生效”：历史上生效过、即使已被替换也不可复用。"""
    services.create_waybill(wb("W1", {"B1": 10}))
    services.submit_package(pkg("P1", ["W1"], {"N1": {"B1": 10}}))
    services.submit_package(pkg("P2", ["N1"], {"N2": {"B1": 10}}))
    # N1 已被替换，尝试再次启用 N1
    view, _ = services.submit_package(pkg("P3", ["N2"], {"N1": {"B1": 10}}))
    assert view["status"] == "rejected"
    assert "N1" in view["reason"]


def test_weight_mismatch_rejected(temp_db):
    services.create_waybill(wb("W1", {"B1": 100, "B2": 200}))
    view, _ = services.submit_package(
        pkg("P1", ["W1"], {"N1": {"B1": 100, "B2": 201}})
    )
    assert view["status"] == "rejected"
    assert "重量" in view["reason"]
    # 拒收后原单依旧有效，数据未被改动
    assert services.get_waybill("W1")["status"] == "active"


def test_idempotent_same_content_replay(temp_db):
    services.create_waybill(wb("W1", {"B1": 10}))
    p = pkg("P1", ["W1"], {"N1": {"B1": 10}})
    first, replayed1 = services.submit_package(p)
    assert first["status"] == "applied" and replayed1 is False
    # 同包号同内容（即使 JSON 字段顺序不同）重传 -> 沿用原结果
    p2 = PackageIn.model_validate(
        {
            "new_waybills": [
                {"waybill_no": "N1", "batches": [
                    {"weight_kg": 10, "batch_no": "B1"}]}
            ],
            "old_waybill_nos": ["W1"],
            "package_no": "P1",
        }
    )
    second, replayed2 = services.submit_package(p2)
    assert replayed2 is True
    assert second["content_hash"] == first["content_hash"]
    assert second["status"] == "applied"


def test_same_package_no_different_content_rejected(temp_db):
    services.create_waybill(wb("W1", {"B1": 10}))
    services.submit_package(pkg("P1", ["W1"], {"N1": {"B1": 10}}))
    with pytest.raises(services.DomainError) as exc:
        services.submit_package(pkg("P1", ["W1"], {"N9": {"B1": 10}}))
    assert exc.value.status_code == 409
    assert "内容不同" in exc.value.message


def test_duplicate_waybill_entry_rejected(temp_db):
    services.create_waybill(wb("W1", {"B1": 10}))
    with pytest.raises(services.DomainError):
        services.create_waybill(wb("W1", {"B1": 10}))


def test_concurrent_packages_same_old_waybill(temp_db):
    """两个包并行替换同一旧单：恰好一个生效，另一个拒收。"""
    services.create_waybill(wb("W1", {"B1": 10}))
    p1 = pkg("P1", ["W1"], {"N1": {"B1": 10}})
    p2 = pkg("P2", ["W1"], {"N2": {"B1": 10}})
    results: list[str] = []
    errors: list[str] = []

    def worker(p: PackageIn, out: list[str]) -> None:
        try:
            view, _ = services.submit_package(p)
            out.append(view["status"])
        except services.DomainError as e:
            errors.append(e.message)

    t1 = threading.Thread(target=worker, args=(p1, results))
    t2 = threading.Thread(target=worker, args=(p2, results))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == ["applied", "rejected"]
    actives = [w["waybill_no"] for w in services.list_waybills("active")]
    assert actives.count("W1") == 0
    assert len(actives) == 1 and actives[0] in {"N1", "N2"}


def test_chain_suspended_packages_resume_in_order(temp_db):
    """多个挂起包链式等待，一次补齐后按序全部续办。"""
    services.create_waybill(wb("W1", {"B1": 10}))
    services.submit_package(pkg("P1", ["W1", "W2"], {"N1": {"B1": 10, "B2": 5}}))
    # P2 依赖 P1 的产物 N1，且还缺 W3
    services.submit_package(
        pkg("P2", ["N1", "W3"], {"N2": {"B1": 10, "B2": 5, "B3": 7}})
    )
    services.create_waybill(wb("W2", {"B2": 5}))
    services.create_waybill(wb("W3", {"B3": 7}))
    pkgs = {p["package_no"]: p for p in services.list_packages()}
    assert pkgs["P1"]["status"] == "applied"
    assert pkgs["P2"]["status"] == "applied"
    assert [w["waybill_no"] for w in services.list_waybills("active")] == ["N2"]


def test_schema_rejects_bad_inputs():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WaybillIn(waybill_no="W", batches=[])  # 无货批
    with pytest.raises(ValidationError):
        WaybillIn(
            waybill_no="W",
            batches=[BatchIn(batch_no="B1", weight_kg=0)],  # 重量非正
        )
    with pytest.raises(ValidationError):
        PackageIn(
            package_no="P",
            old_waybill_nos=["W1", "W1"],  # 旧单重复
            new_waybills=[NewWaybillIn(
                waybill_no="N1", batches=[BatchIn(batch_no="B1", weight_kg=1)])],
        )
