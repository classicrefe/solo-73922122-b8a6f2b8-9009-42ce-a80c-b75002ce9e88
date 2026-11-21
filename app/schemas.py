"""请求/响应 Pydantic 模型。

结构层面的校验（空列表、重复单号、重量为正整数等）在这里完成，
业务规则（旧单是否到齐、是否已被替换等）在 services 层处理。
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class BatchIn(BaseModel):
    batch_no: str = Field(..., min_length=1, description="货批号")
    weight_kg: int = Field(..., strict=True, gt=0, description="整数公斤数，必须为正")

    @field_validator("batch_no")
    @classmethod
    def _strip_batch_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("货批号不能为空")
        return v


class WaybillIn(BaseModel):
    waybill_no: str = Field(..., min_length=1, description="运单号")
    batches: list[BatchIn] = Field(..., min_length=1)

    @field_validator("waybill_no")
    @classmethod
    def _strip_waybill_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("运单号不能为空")
        return v

    @model_validator(mode="after")
    def _unique_batch_no(self) -> "WaybillIn":
        nos = [b.batch_no for b in self.batches]
        if len(nos) != len(set(nos)):
            raise ValueError("同一运单内货批号不得重复")
        return self


class NewWaybillIn(BaseModel):
    """改单包中的新单：单号 + 货批重量。"""

    waybill_no: str = Field(..., min_length=1)
    batches: list[BatchIn] = Field(..., min_length=1)

    @field_validator("waybill_no")
    @classmethod
    def _strip_waybill_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("新单号不能为空")
        return v

    @model_validator(mode="after")
    def _unique_batch_no(self) -> "NewWaybillIn":
        nos = [b.batch_no for b in self.batches]
        if len(nos) != len(set(nos)):
            raise ValueError(f"新单 {self.waybill_no} 内货批号重复")
        return self


class PackageIn(BaseModel):
    package_no: str = Field(..., min_length=1, description="改单包号")
    old_waybill_nos: list[str] = Field(..., min_length=1)
    new_waybills: list[NewWaybillIn] = Field(..., min_length=1)

    @field_validator("package_no")
    @classmethod
    def _strip_package_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("改单包号不能为空")
        return v

    @field_validator("old_waybill_nos")
    @classmethod
    def _strip_old_nos(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values]
        if any(not v for v in cleaned):
            raise ValueError("旧单号不能为空字符串")
        return cleaned

    @model_validator(mode="after")
    def _cross_field_checks(self) -> "PackageIn":
        if len(self.old_waybill_nos) != len(set(self.old_waybill_nos)):
            raise ValueError("旧单号在包内重复")
        new_nos = [w.waybill_no for w in self.new_waybills]
        if len(new_nos) != len(set(new_nos)):
            raise ValueError("新单号在包内重复")
        overlap = set(new_nos) & set(self.old_waybill_nos)
        if overlap:
            raise ValueError(f"新单号不得与旧单号相同: {sorted(overlap)}")
        # 新单之间的货批号也不允许重复（同一批货在新单集合中必须唯一归属）
        seen: set[str] = set()
        for w in self.new_waybills:
            for b in w.batches:
                if b.batch_no in seen:
                    raise ValueError(f"货批号 {b.batch_no} 在新单之间重复")
                seen.add(b.batch_no)
        return self


class BatchView(BaseModel):
    batch_no: str
    weight_kg: int


class WaybillView(BaseModel):
    waybill_no: str
    status: str
    batches: list[BatchView]
    total_weight_kg: int
    source_package_no: str | None
    replaced_by_pkg: str | None
    created_at: str
    replaced_at: str | None


class PackageView(BaseModel):
    package_no: str
    status: str
    reason: str | None
    old_waybill_nos: list[str]
    new_waybills: list[NewWaybillIn]
    content_hash: str
    created_at: str
    updated_at: str
