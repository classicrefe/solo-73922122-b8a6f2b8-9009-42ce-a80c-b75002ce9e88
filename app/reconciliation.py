"""承运方月账单核对。

账单由承运方按运单号 + 货批号 + 计费公斤数提供，每行申报的是对应运单
货批的**全重**。核对规则：

* 整份账单在同一个只读快照事务中核对，看到的是同一份已提交台账状态；
* 从账单引用的现行单或历史单出发，沿已生效改单包逐级回追到原单货批；
* 单号/货批不存在、计费重量与台账不符、或沿途任一次拆并前后该货批总重
  不等时，该行转“待人工核实”并给出原因；
* 其余可追溯的行中，若两行（或多行）回追到同一条原单货批，则都标记为
  “疑似重复”，列出账单行号与共同的原单货批；
* 原单货批的身份是“原单运单 + 货批号”，因此不同原单上的同号货批互不
  干扰，可以分别通过。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .database import read_snapshot

# CSV 表头别名（统一做 NFKC、去空白、转小写后匹配）
_HEADER_ALIASES: dict[str, set[str]] = {
    "waybill_no": {
        "运单号",
        "运单编号",
        "waybill_no",
        "waybillno",
        "waybill",
    },
    "batch_no": {
        "货批号",
        "批号",
        "batch_no",
        "batchno",
        "batch",
    },
    "billed_weight_kg": {
        "计费公斤数",
        "计费重量",
        "计费重量kg",
        "计费重量(kg)",
        "计费kg",
        "重量kg",
        "重量",
        "billed_weight_kg",
        "billedweightkg",
        "weight_kg",
        "weightkg",
    },
}
_FIELD_LABELS = {
    "waybill_no": "运单号",
    "batch_no": "货批号",
    "billed_weight_kg": "计费公斤数",
}


class BillFormatError(ValueError):
    """整份账单无法解析（编码、表头、空文件等）。"""


# --------------------------------------------------------------------------- #
# CSV 解析
# --------------------------------------------------------------------------- #
@dataclass
class BillLine:
    line_no: int
    waybill_no: str
    batch_no: str
    billed_text: str
    billed_weight: Decimal | None
    errors: list[str] = field(default_factory=list)


def _norm_header(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split()).lower()


def _decode_csv(data: bytes) -> str:
    if not data:
        raise BillFormatError("CSV 文件为空")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise BillFormatError("无法识别 UTF-16 编码的 CSV") from exc
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise BillFormatError("无法识别 CSV 编码，请使用 UTF-8、UTF-16 或 GB18030")


def _detect_delimiter(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            counts = {
                ",": line.count(","),
                ";": line.count(";"),
                "\t": line.count("\t"),
            }
            best = max(counts, key=counts.get)
            return best if counts[best] else ","
    return ","


def _iter_rows(reader: Iterator[list[str]]) -> Iterator[tuple[int, list[str]]]:
    """产出 (物理起始行号, 行)。表头为第 1 行时，首条数据行为第 2 行。"""
    start = 1
    for row in reader:
        yield start, row
        start = reader.line_num + 1


_WEIGHT_RE = re.compile(r"\d+(?:\.\d+)?")
_WEIGHT_UNITS = ("kg", "公斤", "千克")


def _parse_weight(raw: str) -> tuple[Decimal | None, str | None]:
    text = unicodedata.normalize("NFKC", raw).strip()
    if not text:
        return None, "计费公斤数缺失"
    if len(text) > 64:
        return None, f"计费公斤数“{raw}”过长，无法识别"
    lowered = text.lower()
    for unit in _WEIGHT_UNITS:
        if lowered.endswith(unit):
            text = text[: -len(unit)].strip()
            break
    # 千分位（半角/全角逗号、空格）允许出现在数字之间
    normalized = re.sub(r"[,\s]", "", text)
    if not _WEIGHT_RE.fullmatch(normalized):
        return None, f"计费公斤数“{raw}”不是有效数字"
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return None, f"计费公斤数“{raw}”不是有效数字"
    if not value.is_finite():
        return None, f"计费公斤数“{raw}”不是有效数字"
    if value <= 0:
        return None, f"计费公斤数“{raw}”必须为正数"
    if value > Decimal(999999999):
        return None, f"计费公斤数“{raw}”超出合理范围"
    return value, None


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def parse_bill_csv(data: bytes) -> list[BillLine]:
    """解析并做行内字段校验；表头级错误抛 :class:`BillFormatError`。"""
    text = _decode_csv(data)
    delimiter = _detect_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows = _iter_rows(reader)

    header: list[str] | None = None
    for _, row in rows:
        if any(cell.strip() for cell in row):
            header = row
            break
    if header is None:
        raise BillFormatError("CSV 文件没有表头")

    indexes: dict[str, int] = {}
    for idx, cell in enumerate(header):
        key = _norm_header(cell)
        for field_name, aliases in _HEADER_ALIASES.items():
            if key in aliases and field_name not in indexes:
                indexes[field_name] = idx
    missing = [f for f in _HEADER_ALIASES if f not in indexes]
    if missing:
        names = "、".join(_FIELD_LABELS[f] for f in missing)
        raise BillFormatError(
            f"CSV 表头缺少列：{names}；需要 运单号、货批号、计费公斤数"
        )

    def cell(row: list[str], field_name: str) -> str:
        idx = indexes[field_name]
        return row[idx].strip() if idx < len(row) else ""

    lines: list[BillLine] = []
    for line_no, row in rows:
        if not any(c.strip() for c in row):
            continue
        waybill_no = cell(row, "waybill_no")
        batch_no = cell(row, "batch_no")
        billed_text = cell(row, "billed_weight_kg")
        errors: list[str] = []
        if not waybill_no:
            errors.append("运单号为空")
        if not batch_no:
            errors.append("货批号为空")
        billed_weight, weight_error = _parse_weight(billed_text)
        if weight_error:
            errors.append(weight_error)
        lines.append(
            BillLine(
                line_no=line_no,
                waybill_no=waybill_no,
                batch_no=batch_no,
                billed_text=billed_text,
                billed_weight=billed_weight,
                errors=errors,
            )
        )
    if not lines:
        raise BillFormatError("账单没有数据行")
    return lines


# --------------------------------------------------------------------------- #
# 台账快照与拆并链
# --------------------------------------------------------------------------- #
@dataclass
class _Batch:
    batch_no: str
    weight_kg: int


@dataclass
class _Waybill:
    id: int
    waybill_no: str
    status: str
    source_package_no: str | None
    replaced_by_pkg: str | None
    batches: dict[str, _Batch] = field(default_factory=dict)


@dataclass(frozen=True)
class _Key:
    waybill_id: int
    batch_no: str


@dataclass
class _Analysis:
    package_no: str
    predecessors: list[_Key]
    errors: list[str]


@dataclass
class _Ledger:
    waybills: dict[int, _Waybill]
    by_no: dict[str, _Waybill]
    analyses: dict[_Key, _Analysis]
    applied_packages: set[str] = field(default_factory=set)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _load_ledger(conn: sqlite3.Connection) -> _Ledger:
    waybills: dict[int, _Waybill] = {}
    by_no: dict[str, _Waybill] = {}
    for row in conn.execute(
        "SELECT id, waybill_no, status, source_package_no, replaced_by_pkg "
        "FROM waybills ORDER BY id"
    ):
        wb = _Waybill(
            id=row["id"],
            waybill_no=row["waybill_no"],
            status=row["status"],
            source_package_no=row["source_package_no"],
            replaced_by_pkg=row["replaced_by_pkg"],
        )
        waybills[wb.id] = wb
        by_no[wb.waybill_no] = wb
    for row in conn.execute(
        "SELECT waybill_id, batch_no, weight_kg FROM batches "
        "ORDER BY waybill_id, batch_no"
    ):
        wb = waybills.get(row["waybill_id"])
        if wb is not None:
            wb.batches[row["batch_no"]] = _Batch(
                batch_no=row["batch_no"], weight_kg=row["weight_kg"]
            )

    ledger = _Ledger(waybills=waybills, by_no=by_no, analyses={})
    for row in conn.execute(
        "SELECT package_no, payload FROM packages "
        "WHERE status = 'applied' ORDER BY id"
    ):
        ledger.applied_packages.add(row["package_no"])
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        _build_package_analysis(ledger, row["package_no"], payload)
    return ledger


def _build_package_analysis(
    ledger: _Ledger, package_no: str, payload: Any
) -> None:
    """为一个已生效改单包建立 新单货批 -> 旧单货批 的反向追溯关系。"""
    if not isinstance(payload, dict):
        return
    old_nos = payload.get("old_waybill_nos") or []
    new_waybills = payload.get("new_waybills") or []

    structural_errors: list[str] = []
    old_wbs: list[_Waybill] = []
    for no in old_nos:
        wb = ledger.by_no.get(str(no))
        if wb is None:
            structural_errors.append(
                f"改单包 {package_no} 引用的旧单 {no} 不在台账中"
            )
        else:
            old_wbs.append(wb)

    old_by_batch: dict[str, list[tuple[_Waybill, _Batch]]] = defaultdict(list)
    for wb in old_wbs:
        for batch in wb.batches.values():
            old_by_batch[batch.batch_no].append((wb, batch))

    # (新单, 货批号, 包内容中的重量)
    new_entries: list[tuple[_Waybill, str, int]] = []
    for new_wb in new_waybills:
        if not isinstance(new_wb, dict):
            continue
        new_no = new_wb.get("waybill_no")
        wb = ledger.by_no.get(str(new_no))
        if wb is None:
            structural_errors.append(
                f"改单包 {package_no} 的新单 {new_no} 不在台账中"
            )
            continue
        for batch in new_wb.get("batches") or []:
            if not isinstance(batch, dict):
                continue
            batch_no = batch.get("batch_no")
            try:
                weight = int(batch.get("weight_kg"))
            except (TypeError, ValueError):
                structural_errors.append(
                    f"改单包 {package_no} 的新单 {new_no} 货批 {batch_no} "
                    "重量记录无效"
                )
                continue
            new_entries.append((wb, str(batch_no), weight))

    new_by_batch: dict[str, list[tuple[_Waybill, str, int]]] = defaultdict(list)
    for entry in new_entries:
        new_by_batch[entry[1]].append(entry)

    for batch_no in sorted(set(old_by_batch) | set(new_by_batch)):
        old_list = old_by_batch.get(batch_no, [])
        new_list = new_by_batch.get(batch_no, [])
        errors = list(structural_errors)

        old_ledger_sum = sum(batch.weight_kg for _, batch in old_list)
        new_ledger_sum = 0
        for wb, batch_no_, _ in new_list:
            ledger_batch = wb.batches.get(batch_no_)
            if ledger_batch is None:
                errors.append(
                    f"改单包 {package_no} 的新单 {wb.waybill_no} 在台账中"
                    f"缺少货批 {batch_no_}，拆并记录与台账不符"
                )
            else:
                new_ledger_sum += ledger_batch.weight_kg
        payload_new_sum = sum(weight for _, _, weight in new_list)

        if old_ledger_sum != new_ledger_sum:
            errors.append(
                f"改单包 {package_no} 拆并前后货批 {batch_no} 总重不等："
                f"拆并前 {old_ledger_sum}kg，拆并后 {new_ledger_sum}kg"
            )
        if payload_new_sum != new_ledger_sum:
            errors.append(
                f"改单包 {package_no} 货批 {batch_no} 的拆并记录重量 "
                f"{payload_new_sum}kg 与新单台账 {new_ledger_sum}kg 不符"
            )
        if len(old_list) > 1:
            owners = "、".join(wb.waybill_no for wb, _ in old_list)
            errors.append(
                f"改单包 {package_no} 中货批 {batch_no} 对应多张旧单货批"
                f"（{owners}），无法唯一追溯"
            )
        if not old_list:
            errors.append(
                f"改单包 {package_no} 中货批 {batch_no} 没有对应的旧单货批，"
                "无法追溯"
            )
        if len(new_list) > 1:
            errors.append(
                f"改单包 {package_no} 中货批 {batch_no} 出现在多张新单，"
                "无法唯一追溯"
            )

        predecessors = [
            _Key(waybill_id=wb.id, batch_no=batch_no) for wb, _ in old_list
        ]
        for wb, _, _ in new_list:
            ledger.analyses[_Key(waybill_id=wb.id, batch_no=batch_no)] = (
                _Analysis(
                    package_no=package_no,
                    predecessors=predecessors,
                    errors=_dedupe(errors),
                )
            )


def _trace_to_origin(
    ledger: _Ledger, waybill: _Waybill, batch_no: str
) -> tuple[_Key | None, list[dict[str, Any]], list[str]]:
    """从账单引用的货批沿已生效拆并记录回追到原单货批。

    返回 (原单货批 key, 原单 -> 当前 的轨迹, 沿途异常原因)。
    """
    node = _Key(waybill_id=waybill.id, batch_no=batch_no)
    visited: set[_Key] = set()
    path: list[dict[str, Any]] = []
    errors: list[str] = []
    origin: _Key | None = None

    while True:
        if node in visited:
            errors.append("拆并链路存在循环，无法追溯到原单货批")
            break
        visited.add(node)

        wb = ledger.waybills.get(node.waybill_id)
        if wb is None:
            errors.append("拆并链路中的运单不在台账中，无法追溯")
            break
        batch = wb.batches.get(node.batch_no)
        if batch is None:
            errors.append(
                f"拆并链路中运单 {wb.waybill_no} 缺少货批 {node.batch_no}，"
                "无法追溯"
            )
            break

        path.append(
            {
                "waybill_no": wb.waybill_no,
                "batch_no": node.batch_no,
                "weight_kg": batch.weight_kg,
                "waybill_status": wb.status,
                "via_package_no": wb.source_package_no,
            }
        )

        if wb.source_package_no is None:
            origin = node
            break

        if wb.source_package_no not in ledger.applied_packages:
            errors.append(
                f"运单 {wb.waybill_no} 的来源改单包 {wb.source_package_no} "
                "不在已生效包中，拆并关系无依据，无法追溯"
            )
            break

        analysis = ledger.analyses.get(node)
        if analysis is None:
            errors.append(
                f"改单包 {wb.source_package_no} 的拆并记录中找不到新单 "
                f"{wb.waybill_no} 货批 {node.batch_no}，无法追溯到原单货批"
            )
            break

        errors.extend(analysis.errors)
        if len(analysis.predecessors) != 1:
            if not any("无法唯一追溯" in e for e in analysis.errors):
                errors.append(
                    f"改单包 {analysis.package_no} 中货批 {node.batch_no} "
                    "对应的旧单货批不唯一，无法追溯到原单货批"
                )
            break
        node = analysis.predecessors[0]

    path.reverse()
    return origin, path, _dedupe(errors)


# --------------------------------------------------------------------------- #
# 核对
# --------------------------------------------------------------------------- #
def _verify_line(ledger: _Ledger, line: BillLine) -> dict[str, Any]:
    reasons = list(line.errors)
    result: dict[str, Any] = {
        "line_no": line.line_no,
        "waybill_no": line.waybill_no,
        "batch_no": line.batch_no,
        "billed_weight_kg": (
            _decimal_text(line.billed_weight)
            if line.billed_weight is not None
            else line.billed_text
        ),
        "ledger_weight_kg": None,
        "waybill_status": None,
        "status": "manual_review",
        "reasons": [],
        "original_batch": None,
        "trace": [],
        "duplicate_with": [],
        "shared_original_batches": [],
    }

    waybill = ledger.by_no.get(line.waybill_no)
    if waybill is None:
        if line.waybill_no:
            reasons.append(f"运单号 {line.waybill_no} 在台账中不存在")
        result["reasons"] = _dedupe(reasons)
        return result

    result["waybill_status"] = waybill.status
    batch = waybill.batches.get(line.batch_no)
    if batch is None:
        if line.batch_no:
            reasons.append(
                f"运单 {waybill.waybill_no} 中不存在货批 {line.batch_no}"
            )
        result["reasons"] = _dedupe(reasons)
        return result

    result["ledger_weight_kg"] = batch.weight_kg
    if (
        line.billed_weight is not None
        and line.billed_weight != Decimal(batch.weight_kg)
    ):
        reasons.append(
            f"计费重量 {_decimal_text(line.billed_weight)}kg 与台账 "
            f"{batch.weight_kg}kg 不符"
        )

    origin, trace, trace_errors = _trace_to_origin(
        ledger, waybill, line.batch_no
    )
    result["trace"] = trace
    reasons.extend(trace_errors)
    if origin is not None:
        origin_wb = ledger.waybills[origin.waybill_id]
        origin_batch = origin_wb.batches[origin.batch_no]
        result["original_batch"] = {
            "waybill_no": origin_wb.waybill_no,
            "batch_no": origin_batch.batch_no,
            "weight_kg": origin_batch.weight_kg,
        }

    result["reasons"] = _dedupe(reasons)
    result["status"] = "manual_review" if result["reasons"] else "passed"
    return result


def _mark_duplicates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        origin = row.get("original_batch")
        if row["status"] == "passed" and origin:
            groups[(origin["waybill_no"], origin["batch_no"])].append(row)

    duplicate_groups: list[dict[str, Any]] = []
    for (waybill_no, batch_no), members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: r["line_no"])
        origin = members[0]["original_batch"]
        for row in members:
            row["status"] = "suspected_duplicate"
            row["duplicate_with"] = [
                {
                    "line_no": other["line_no"],
                    "waybill_no": other["waybill_no"],
                    "batch_no": other["batch_no"],
                }
                for other in members
                if other is not row
            ]
            row["shared_original_batches"] = [dict(origin)]
        duplicate_groups.append(
            {
                "original_waybill_no": waybill_no,
                "original_batch_no": batch_no,
                "weight_kg": origin["weight_kg"],
                "line_numbers": [r["line_no"] for r in members],
                "rows": [
                    {
                        "line_no": r["line_no"],
                        "waybill_no": r["waybill_no"],
                        "batch_no": r["batch_no"],
                        "billed_weight_kg": r["billed_weight_kg"],
                    }
                    for r in members
                ],
            }
        )

    duplicate_groups.sort(key=lambda g: min(g["line_numbers"]))
    return duplicate_groups


def _ledger_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """台账快照指纹与规模，证明整份账单使用的是同一份已提交状态。"""
    digest = hashlib.sha256()
    waybill_count = 0
    active_count = 0
    batch_count = 0
    for wb in conn.execute(
        "SELECT id, waybill_no, status, source_package_no, replaced_by_pkg "
        "FROM waybills ORDER BY id"
    ):
        waybill_count += 1
        if wb["status"] == "active":
            active_count += 1
        digest.update(
            json.dumps(
                [
                    "waybill",
                    wb["waybill_no"],
                    wb["status"],
                    wb["source_package_no"],
                    wb["replaced_by_pkg"],
                ],
                ensure_ascii=False,
            ).encode("utf-8")
        )
        for batch in conn.execute(
            "SELECT batch_no, weight_kg FROM batches "
            "WHERE waybill_id = ? ORDER BY batch_no",
            (wb["id"],),
        ):
            batch_count += 1
            digest.update(
                json.dumps(
                    ["batch", wb["waybill_no"], batch["batch_no"],
                     batch["weight_kg"]],
                    ensure_ascii=False,
                ).encode("utf-8")
            )

    applied_count = 0
    package_count = 0
    max_package_id = 0
    for pkg in conn.execute(
        "SELECT id, package_no, payload, status FROM packages ORDER BY id"
    ):
        package_count += 1
        max_package_id = max(max_package_id, pkg["id"])
        if pkg["status"] != "applied":
            continue
        applied_count += 1
        digest.update(
            json.dumps(
                ["applied_package", pkg["package_no"], pkg["payload"]],
                ensure_ascii=False,
            ).encode("utf-8")
        )

    max_waybill_id = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM waybills"
    ).fetchone()[0]
    return {
        "token": digest.hexdigest(),
        "waybill_count": waybill_count,
        "active_waybill_count": active_count,
        "batch_count": batch_count,
        "package_count": package_count,
        "applied_package_count": applied_count,
        "max_waybill_id": max_waybill_id,
        "max_package_id": max_package_id,
    }


def verify_bill(data: bytes, *, filename: str = "") -> dict[str, Any]:
    """核对整份账单；所有台账读取都发生在同一个只读快照事务内。"""
    lines = parse_bill_csv(data)
    with read_snapshot() as conn:
        ledger = _load_ledger(conn)
        ledger_state = _ledger_state(conn)
        rows = [_verify_line(ledger, line) for line in lines]

    duplicate_groups = _mark_duplicates(rows)
    summary = {
        "total": len(rows),
        "passed": sum(1 for r in rows if r["status"] == "passed"),
        "manual_review": sum(
            1 for r in rows if r["status"] == "manual_review"
        ),
        "suspected_duplicate": sum(
            1 for r in rows if r["status"] == "suspected_duplicate"
        ),
        "duplicate_groups": len(duplicate_groups),
    }
    return {
        "filename": filename,
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "ledger_state": ledger_state,
        "summary": summary,
        "rows": rows,
        "duplicate_groups": duplicate_groups,
    }
