"""Pydantic 模型：请求与响应结构。

所有频率均为整数 Hz。任一字段校验失败都会使整个请求被拒绝（整批拒绝），
服务端不会写入任何部分数据。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class GuardBand(BaseModel):
    """保护频段，闭区间 [start_hz, end_hz]。"""

    start_hz: int = Field(..., gt=0, description="频段起点（含），整数 Hz")
    end_hz: int = Field(..., gt=0, description="频段终点（含），整数 Hz")

    @model_validator(mode="after")
    def _check_order(self) -> "GuardBand":
        if self.end_hz <= self.start_hz:
            raise ValueError("guard band end_hz must be greater than start_hz")
        return self


class SiteCreate(BaseModel):
    """创建站点（频率方案）的请求体。"""

    name: str = Field(..., min_length=1, max_length=128, description="方案名称，全局唯一")
    scan_start_hz: int = Field(..., gt=0, description="扫描范围起点（含），整数 Hz")
    scan_end_hz: int = Field(..., gt=0, description="扫描范围终点（含），整数 Hz")
    guard_interval_hz: int = Field(
        ..., ge=0, description="保护间隔：产物到命中对象的距离小于该值判定为违规"
    )
    carriers_hz: list[int] = Field(..., min_length=1, description="载频列表，整数 Hz，不允许重复")
    guard_bands: list[GuardBand] = Field(default_factory=list, description="保护频段列表")

    @field_validator("carriers_hz")
    @classmethod
    def _check_carriers(cls, v: list[int]) -> list[int]:
        if any(f <= 0 for f in v):
            raise ValueError("carrier frequencies must be positive integers (Hz)")
        if len(set(v)) != len(v):
            raise ValueError("duplicate carrier frequencies are not allowed")
        return v

    @model_validator(mode="after")
    def _check_scan(self) -> "SiteCreate":
        if self.scan_end_hz <= self.scan_start_hz:
            raise ValueError("scan_end_hz must be greater than scan_start_hz")
        return self


class SiteOut(BaseModel):
    id: int
    name: str
    scan_start_hz: int
    scan_end_hz: int
    guard_interval_hz: int
    carriers_hz: list[int]
    guard_bands: list[GuardBand]
    created_at: str


class SourcePair(BaseModel):
    """一个来源组合：2*fa_hz - fb_hz = 产物频率。"""

    fa_hz: int
    fb_hz: int


class HitObject(BaseModel):
    """产物命中的最近对象：某个载频或某个保护频段。"""

    type: Literal["carrier", "guard_band"]
    carrier_hz: int | None = None
    guard_band: GuardBand | None = None


class ProductOut(BaseModel):
    freq_hz: int = Field(..., description="互调产物频率，整数 Hz")
    distance_hz: int = Field(..., description="到命中对象的最短距离，整数 Hz")
    hit: HitObject
    violates_guard_interval: bool = Field(..., description="distance_hz < guard_interval_hz")
    sources: list[SourcePair] = Field(..., description="产生该频率的全部来源组合")


class AnalysisResult(BaseModel):
    analysis_id: int
    site_id: int
    created_at: str
    product_count: int
    products: list[ProductOut]


class AnalysisMeta(BaseModel):
    analysis_id: int
    site_id: int
    created_at: str
    product_count: int


class RiskGroup(BaseModel):
    """一个风险组：违规产物与其来源载频构成二部图后的一个连通分量。"""

    group_id: int = Field(..., description="组编号：按 (最小载频, 最小产物频率) 排序后从 1 连续编号")
    carriers_hz: list[int] = Field(..., description="组内全部来源载频（fa_hz/fb_hz），升序")
    product_freqs_hz: list[int] = Field(..., description="组内违规产物频率，升序")
    source_pair_count: int = Field(..., description="组内全部产物的来源组合总数")
    distance_hz: int = Field(..., description="组内产物到命中对象的最小距离")


class MitigationPlan(BaseModel):
    """违规产物停用方案：在停用载频数上限内精确求解最小停用集合（最小点覆盖）。

    判定规则：某违规产物的每个来源组合 (fa_hz, fb_hz) 中至少一个载频被停用，
    该产物才算被消除。feasible=true 时给出基数最小的停用集合，多个最小解按
    载频升序列表的字典序择优；feasible=false 时给出上限内消除违规产物最多的
    尽力方案及仍未消除的产物频率。
    """

    analysis_id: int
    max_disabled: int = Field(..., description="本次求解允许停用的最大载频数（1～8）")
    feasible: bool = Field(..., description="上限内是否存在可消除全部违规产物的停用集合")
    disabled_carriers_hz: list[int] = Field(
        ...,
        description="建议停用的载频（升序）。feasible=true 时为基数最小、字典序择优的精确解；"
        "feasible=false 时为上限内消除违规产物最多（并列时覆盖来源组合最多、再按字典序）的方案",
    )
    eliminated_product_freqs_hz: list[int] = Field(
        ..., description="该方案下可消除的违规产物频率（升序）；feasible=true 时为全部违规产物"
    )
    remaining_product_freqs_hz: list[int] = Field(
        ..., description="该方案下仍未消除的违规产物频率（升序）；feasible=true 时恒为空列表"
    )


class ExclusionRange(BaseModel):
    """一段不宜新增载频的闭区间 [start_hz, end_hz]（整数 Hz）。"""

    start_hz: int = Field(..., description="区间起点（含），整数 Hz")
    end_hz: int = Field(..., description="区间终点（含），整数 Hz")


class CarrierExclusionResult(BaseModel):
    """候选范围内不宜新增载频区间的最小并集（不执行也不保存分析）。"""

    site_id: int
    candidate_start_hz: int = Field(..., description="候选范围起点（含），整数 Hz")
    candidate_end_hz: int = Field(..., description="候选范围终点（含），整数 Hz")
    exclusion_ranges: list[ExclusionRange] = Field(
        ..., description="不宜新增载频的闭区间，按起点升序，互不重叠且互不相邻"
    )


# ---------------------------------------------------------- 跨站点互调冲突

class CrossSiteConflictRequest(BaseModel):
    """跨站点冲突检测请求：2～8 个已保存站点 id 与频率容差。"""

    site_ids: list[int] = Field(
        ..., min_length=2, max_length=8, description="参与比对的站点 id 列表，取 2～8 个，不允许重复"
    )
    tolerance_hz: int = Field(
        ..., ge=0, description="频率容差（非负整数 Hz）：不同站点产物频率差不超过该值即配对"
    )

    @field_validator("site_ids")
    @classmethod
    def _check_site_ids(cls, v: list[int]) -> list[int]:
        if any(site_id <= 0 for site_id in v):
            raise ValueError("site ids must be positive integers")
        if len(set(v)) != len(v):
            raise ValueError("duplicate site ids are not allowed")
        return v


class ConflictProduct(BaseModel):
    """冲突簇内来自某个站点分析的一个产物频率。"""

    site_id: int
    freq_hz: int = Field(..., description="产物频率，整数 Hz")
    analysis_id: int = Field(..., description="该站点最近一次分析的 id")


class ConflictPair(BaseModel):
    """一对跨站点匹配产物：来自不同站点，频率差不超过容差。"""

    site_a_id: int = Field(..., description="频率较低一侧产物所属站点 id（频率相同取较小站点 id）")
    freq_a_hz: int
    site_b_id: int = Field(..., description="频率较高一侧产物所属站点 id（频率相同取较大站点 id）")
    freq_b_hz: int
    delta_hz: int = Field(..., description="两产物频率差 |freq_b_hz - freq_a_hz|，整数 Hz")


class ConflictCluster(BaseModel):
    """一个冲突簇：配对关系图上的一个连通分量。"""

    cluster_id: int = Field(
        ..., description="簇编号：按簇内最小 (site_id, freq_hz) 升序排序后从 1 连续编号"
    )
    site_ids: list[int] = Field(..., description="簇内涉及的站点 id，升序")
    products: list[ConflictProduct] = Field(
        ..., description="簇内全部产物，按 (site_id, freq_hz) 升序"
    )
    min_delta_hz: int = Field(..., description="簇内全部配对的最小频率差（最接近的一对），整数 Hz")
    pairs: list[ConflictPair] = Field(
        ..., description="簇内全部跨站点配对，按 (freq_a_hz, site_a_id, freq_b_hz, site_b_id) 升序"
    )


class CrossSiteConflictResponse(BaseModel):
    """跨站点冲突检测结果：按配对连通分量聚合的冲突簇。"""

    tolerance_hz: int
    site_ids: list[int] = Field(..., description="参与比对的站点 id，按请求给出的顺序回显")
    cluster_count: int
    pair_count: int = Field(..., description="全部簇内跨站点配对总数")
    clusters: list[ConflictCluster] = Field(..., description="冲突簇，按 cluster_id 升序")
