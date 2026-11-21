"""业务规则与持久化。

核心规则：
* 改单包以一组新单替换一组旧单，逐货批重量相等且新单号从未生效才可整包切换；
* 旧单未到齐 -> 挂起（suspended），原单补齐后自动续办；
* 任一旧单已被替换 -> 整包拒收（rejected）；
* 新单号曾经生效（含已被替换的历史单）-> 拒收；
* 同包号同内容重传沿用原处理结果，异内容 -> 拒收（409）；
* 写事务串行化，并行提交时同一旧单最多被替换一次。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .database import read_connection, write_transaction
from .schemas import PackageIn, WaybillIn


class DomainError(Exception):
    """业务/资源冲突（HTTP 409）。"""

    def __init__(self, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 内容指纹
# --------------------------------------------------------------------------- #
def canonical_payload(package: PackageIn) -> dict[str, Any]:
    """规范化的包内容（仅取决于业务字段，与字段提交顺序无关）。"""
    return {
        "package_no": package.package_no,
        "old_waybill_nos": sorted(package.old_waybill_nos),
        "new_waybills": sorted(
            (
                {
                    "waybill_no": w.waybill_no,
                    "batches": sorted(
                        ({"batch_no": b.batch_no, "weight_kg": b.weight_kg}
                         for b in w.batches),
                        key=lambda x: x["batch_no"],
                    ),
                }
                for w in package.new_waybills
            ),
            key=lambda x: x["waybill_no"],
        ),
    }


def content_hash(package: PackageIn) -> str:
    body = json.dumps(
        canonical_payload(package), ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


# --------------------------------------------------------------------------- #
# 视图
# --------------------------------------------------------------------------- #
def _waybill_view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    batches = [
        {"batch_no": r["batch_no"], "weight_kg": r["weight_kg"]}
        for r in conn.execute(
            "SELECT batch_no, weight_kg FROM batches WHERE waybill_id = ? "
            "ORDER BY batch_no",
            (row["id"],),
        )
    ]
    return {
        "waybill_no": row["waybill_no"],
        "status": row["status"],
        "batches": batches,
        "total_weight_kg": sum(b["weight_kg"] for b in batches),
        "source_package_no": row["source_package_no"],
        "replaced_by_pkg": row["replaced_by_pkg"],
        "created_at": row["created_at"],
        "replaced_at": row["replaced_at"],
    }


def list_waybills(status: str | None = None) -> list[dict[str, Any]]:
    with read_connection() as conn:
        sql = "SELECT * FROM waybills"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY id"
        rows = conn.execute(sql, params).fetchall()
        return [_waybill_view(conn, r) for r in rows]


def get_waybill(waybill_no: str) -> dict[str, Any] | None:
    with read_connection() as conn:
        row = conn.execute(
            "SELECT * FROM waybills WHERE waybill_no = ?", (waybill_no,)
        ).fetchone()
        return _waybill_view(conn, row) if row else None


def _package_view(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload"])
    return {
        "package_no": row["package_no"],
        "status": row["status"],
        "reason": row["reason"],
        "old_waybill_nos": payload["old_waybill_nos"],
        "new_waybills": payload["new_waybills"],
        "content_hash": row["content_hash"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_packages(status: str | None = None) -> list[dict[str, Any]]:
    with read_connection() as conn:
        sql = "SELECT * FROM packages"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY id"
        rows = conn.execute(sql, params).fetchall()
        return [_package_view(r) for r in rows]


# --------------------------------------------------------------------------- #
# 原单录入
# --------------------------------------------------------------------------- #
def create_waybill(waybill: WaybillIn) -> dict[str, Any]:
    with write_transaction() as conn:
        existing = conn.execute(
            "SELECT id FROM waybills WHERE waybill_no = ?", (waybill.waybill_no,)
        ).fetchone()
        if existing:
            raise DomainError(f"运单号 {waybill.waybill_no} 已存在，不可重复录入")
        now = _now()
        cur = conn.execute(
            "INSERT INTO waybills (waybill_no, status, source_package_no, "
            "created_at) VALUES (?, 'active', NULL, ?)",
            (waybill.waybill_no, now),
        )
        wid = cur.lastrowid
        conn.executemany(
            "INSERT INTO batches (waybill_id, batch_no, weight_kg) VALUES (?, ?, ?)",
            [(wid, b.batch_no, b.weight_kg) for b in waybill.batches],
        )
        _resume_suspended(conn)
        row = conn.execute(
            "SELECT * FROM waybills WHERE id = ?", (wid,)
        ).fetchone()
        return _waybill_view(conn, row)


# --------------------------------------------------------------------------- #
# 改单包
# --------------------------------------------------------------------------- #
@dataclass
class Evaluation:
    status: str  # applied | suspended | rejected
    reason: str | None


def _evaluate(conn: sqlite3.Connection, package: PackageIn) -> Evaluation:
    """按既定优先级校验一个改单包。"""
    old_rows = {
        r["waybill_no"]: r
        for r in conn.execute(
            "SELECT * FROM waybills WHERE waybill_no IN (%s)"
            % ",".join("?" * len(package.old_waybill_nos)),
            package.old_waybill_nos,
        )
    }
    # 1) 任一旧单已被替换 -> 整包拒收
    replaced = [no for no, r in old_rows.items() if r["status"] == "replaced"]
    if replaced:
        return Evaluation(
            "rejected",
            f"旧单已被其他改单包替换: {', '.join(sorted(replaced))}",
        )
    # 2) 旧单未到齐 -> 挂起
    missing = [no for no in package.old_waybill_nos if no not in old_rows]
    if missing:
        return Evaluation(
            "suspended",
            f"旧单尚未到齐，等待录入: {', '.join(sorted(missing))}",
        )
    # 3) 新单号必须“从未生效”：active 或历史上出现过（含已替换）都不行
    if package.new_waybills:
        placeholders = ",".join("?" * len(package.new_waybills))
        new_nos = [w.waybill_no for w in package.new_waybills]
        clash = [
            r["waybill_no"]
            for r in conn.execute(
                f"SELECT waybill_no FROM waybills WHERE waybill_no IN ({placeholders})",
                new_nos,
            )
        ]
        if clash:
            return Evaluation(
                "rejected",
                f"新单号曾生效或已存在，不可复用: {', '.join(sorted(clash))}",
            )
    # 4) 逐货批重量相等（旧单集合与新单集合的 货批号->重量 映射必须完全一致）
    old_weights: dict[str, int] = {}
    for no in package.old_waybill_nos:
        for r in conn.execute(
            "SELECT batch_no, weight_kg FROM batches b "
            "JOIN waybills w ON w.id = b.waybill_id WHERE w.waybill_no = ?",
            (no,),
        ):
            old_weights[r["batch_no"]] = r["weight_kg"]
    new_weights: dict[str, int] = {}
    for w in package.new_waybills:
        for b in w.batches:
            new_weights[b.batch_no] = b.weight_kg
    if old_weights != new_weights:
        details: list[str] = []
        for key in sorted(set(old_weights) | set(new_weights)):
            o, n = old_weights.get(key), new_weights.get(key)
            if o != n:
                details.append(f"{key}: 旧={o}kg/新={n}kg")
        return Evaluation(
            "rejected", "逐货批重量不一致 -> " + "; ".join(details)
        )
    return Evaluation("applied", None)


def _apply_package(conn: sqlite3.Connection, package: PackageIn) -> None:
    """整包切换：旧单全部置 replaced，新单全部生效（同一事务内完成）。"""
    now = _now()
    conn.executemany(
        "UPDATE waybills SET status = 'replaced', replaced_at = ?, "
        "replaced_by_pkg = ? WHERE waybill_no = ?",
        [(now, package.package_no, no) for no in package.old_waybill_nos],
    )
    for w in package.new_waybills:
        cur = conn.execute(
            "INSERT INTO waybills (waybill_no, status, source_package_no, "
            "created_at) VALUES (?, 'active', ?, ?)",
            (w.waybill_no, package.package_no, now),
        )
        wid = cur.lastrowid
        conn.executemany(
            "INSERT INTO batches (waybill_id, batch_no, weight_kg) "
            "VALUES (?, ?, ?)",
            [(wid, b.batch_no, b.weight_kg) for b in w.batches],
        )


def _resume_suspended(conn: sqlite3.Connection) -> list[str]:
    """续办所有挂起包：按提交顺序逐轮重判，直到一轮中没有包被切换为止。

    拒收为终态；仍缺单的包保持挂起；一个包切换后可能解锁后续包，因此循环重判。
    """
    applied: list[str] = []
    while True:
        rows = conn.execute(
            "SELECT id, package_no, payload FROM packages "
            "WHERE status = 'suspended' ORDER BY id"
        ).fetchall()
        progress = False
        for row in rows:
            package = PackageIn.model_validate(json.loads(row["payload"]))
            result = _evaluate(conn, package)
            if result.status == "applied":
                _apply_package(conn, package)
                conn.execute(
                    "UPDATE packages SET status = 'applied', reason = NULL, "
                    "updated_at = ? WHERE id = ?",
                    (_now(), row["id"]),
                )
                applied.append(package.package_no)
                progress = True
            elif result.status == "rejected":
                conn.execute(
                    "UPDATE packages SET status = 'rejected', reason = ?, "
                    "updated_at = ? WHERE id = ?",
                    (result.reason, _now(), row["id"]),
                )
        if not progress:
            break
    return applied


def submit_package(package: PackageIn) -> tuple[dict[str, Any], bool]:
    """提交改单包，返回 (视图, 是否为重传)。"""
    digest = content_hash(package)
    payload = json.dumps(
        canonical_payload(package), ensure_ascii=False, sort_keys=True
    )
    with write_transaction() as conn:
        existing = conn.execute(
            "SELECT * FROM packages WHERE package_no = ?", (package.package_no,)
        ).fetchone()
        replayed = False
        if existing:
            if existing["content_hash"] != digest:
                raise DomainError(
                    f"包号 {package.package_no} 已存在且内容不同，整包拒收"
                )
            # 同包号同内容：沿用原处理结果，不改变任何状态
            replayed = True
            return _package_view(existing), replayed

        now = _now()
        result = _evaluate(conn, package)
        cur = conn.execute(
            "INSERT INTO packages (package_no, payload, content_hash, status, "
            "reason, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                package.package_no,
                payload,
                digest,
                result.status,
                result.reason,
                now,
                now,
            ),
        )
        pid = cur.lastrowid
        if result.status == "applied":
            _apply_package(conn, package)
        # 新包入库/切换后，尝试续办既有挂起包（本包若挂起也会在后续被续办）
        _resume_suspended(conn)
        row = conn.execute("SELECT * FROM packages WHERE id = ?", (pid,)).fetchone()
        return _package_view(row), replayed


# --------------------------------------------------------------------------- #
# 对账 CSV（仅当前有效单）
# --------------------------------------------------------------------------- #
def active_waybill_rows() -> list[sqlite3.Row]:
    with read_connection() as conn:
        return conn.execute(
            "SELECT w.waybill_no, b.batch_no, b.weight_kg "
            "FROM waybills w JOIN batches b ON b.waybill_id = w.id "
            "WHERE w.status = 'active' ORDER BY w.waybill_no, b.batch_no"
        ).fetchall()


def stats() -> dict[str, Any]:
    with read_connection() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM waybills WHERE status = 'active'"
        ).fetchone()[0]
        replaced = conn.execute(
            "SELECT COUNT(*) FROM waybills WHERE status = 'replaced'"
        ).fetchone()[0]
        total_weight = conn.execute(
            "SELECT COALESCE(SUM(b.weight_kg), 0) FROM batches b "
            "JOIN waybills w ON w.id = b.waybill_id WHERE w.status = 'active'"
        ).fetchone()[0]
        pkg_counts = dict(
            conn.execute(
                "SELECT status, COUNT(*) FROM packages GROUP BY status"
            ).fetchall()
        )
        return {
            "active_waybills": active,
            "replaced_waybills": replaced,
            "active_total_weight_kg": total_weight,
            "packages": {
                "applied": pkg_counts.get("applied", 0),
                "suspended": pkg_counts.get("suspended", 0),
                "rejected": pkg_counts.get("rejected", 0),
            },
        }
