"""FastAPI 入口：射频三阶互调（2fa - fb）分析 API。"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, status

from . import database
from .analysis import (
    analyze,
    build_cross_site_conflicts,
    build_mitigation_plan,
    build_risk_groups,
    carrier_exclusion_ranges,
)
from .schemas import (
    AnalysisMeta,
    AnalysisResult,
    CarrierExclusionResult,
    CrossSiteConflictRequest,
    CrossSiteConflictResponse,
    MitigationPlan,
    RiskGroup,
    SiteCreate,
    SiteOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()  # 启动时初始化数据库（幂等）
    yield


app = FastAPI(
    title="RF Intermodulation Analysis API",
    summary="射频三阶互调产物（2fa - fb）分析：站点管理、产物枚举与命中评估",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/sites",
    response_model=SiteOut,
    status_code=status.HTTP_201_CREATED,
    tags=["sites"],
    summary="创建站点（频率方案）",
    description="任一载频重复或频段非法时整批拒绝（422），不写入任何数据；名称重复返回 409。",
)
def create_site(payload: SiteCreate) -> SiteOut:
    try:
        site_id = database.insert_site(payload)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"site name already exists: {payload.name}"
        ) from exc
    site = database.fetch_site(site_id)
    assert site is not None
    return site


@app.get("/sites", response_model=list[SiteOut], tags=["sites"], summary="列出全部已保存方案")
def list_sites() -> list[SiteOut]:
    return database.list_sites()


@app.get("/sites/{site_id}", response_model=SiteOut, tags=["sites"], summary="查询单个已保存方案")
def get_site(site_id: int) -> SiteOut:
    site = database.fetch_site(site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"site {site_id} not found")
    return site


@app.get(
    "/sites/{site_id}/carrier-exclusion-ranges",
    response_model=CarrierExclusionResult,
    tags=["sites"],
    summary="候选范围内不宜新增载频的最小区间并集（不执行或保存分析）",
    description=(
        "对候选闭区间内的每个整数 Hz 位置 x：若新增载频 x 会与某个现有载频 c 产生"
        "违规三阶产物（2*x-c 或 2*c-x 落在扫描范围内，且到任一现有载频或保护频段的"
        "距离严格小于保护间隔），或 x 本身是现有载频，则 x 不宜新增。返回全部禁用"
        "位置的最小区间并集：裁剪到候选范围，重叠或相邻区间已合并，按起点升序。"
        "candidate_start_hz 大于 candidate_end_hz 时返回 422。"
    ),
)
def get_carrier_exclusion_ranges(
    site_id: int,
    candidate_start_hz: int = Query(..., gt=0, description="候选范围起点（含），正整数 Hz"),
    candidate_end_hz: int = Query(..., gt=0, description="候选范围终点（含），正整数 Hz"),
) -> CarrierExclusionResult:
    if candidate_start_hz > candidate_end_hz:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "candidate_start_hz must not be greater than candidate_end_hz",
        )
    site = database.fetch_site(site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"site {site_id} not found")
    ranges = carrier_exclusion_ranges(
        carriers_hz=site.carriers_hz,
        scan_start_hz=site.scan_start_hz,
        scan_end_hz=site.scan_end_hz,
        guard_bands=site.guard_bands,
        guard_interval_hz=site.guard_interval_hz,
        candidate_start_hz=candidate_start_hz,
        candidate_end_hz=candidate_end_hz,
    )
    return CarrierExclusionResult(
        site_id=site_id,
        candidate_start_hz=candidate_start_hz,
        candidate_end_hz=candidate_end_hz,
        exclusion_ranges=ranges,
    )


@app.post(
    "/sites/{site_id}/analysis",
    response_model=AnalysisResult,
    status_code=status.HTTP_201_CREATED,
    tags=["analysis"],
    summary="对方案执行三阶互调分析并保存结果",
)
def run_analysis(site_id: int) -> AnalysisResult:
    site = database.fetch_site(site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"site {site_id} not found")
    products = analyze(
        carriers_hz=site.carriers_hz,
        scan_start_hz=site.scan_start_hz,
        scan_end_hz=site.scan_end_hz,
        guard_bands=site.guard_bands,
        guard_interval_hz=site.guard_interval_hz,
    )
    analysis_id, created_at = database.insert_analysis(site_id, products)
    return AnalysisResult(
        analysis_id=analysis_id,
        site_id=site_id,
        created_at=created_at,
        product_count=len(products),
        products=products,
    )


@app.get(
    "/sites/{site_id}/analysis",
    response_model=AnalysisResult,
    tags=["analysis"],
    summary="查询该方案最近一次分析结果",
)
def get_latest_analysis(site_id: int) -> AnalysisResult:
    if database.fetch_site(site_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"site {site_id} not found")
    result = database.fetch_latest_analysis(site_id)
    if result is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no analysis for site {site_id} yet; POST /sites/{site_id}/analysis first",
        )
    return result


@app.get(
    "/sites/{site_id}/analyses",
    response_model=list[AnalysisMeta],
    tags=["analysis"],
    summary="列出该方案的全部分析记录",
)
def list_site_analyses(site_id: int) -> list[AnalysisMeta]:
    if database.fetch_site(site_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"site {site_id} not found")
    return database.list_analyses(site_id)


@app.get(
    "/analyses/{analysis_id}",
    response_model=AnalysisResult,
    tags=["analysis"],
    summary="按 id 查询已保存的分析结果",
)
def get_analysis(analysis_id: int) -> AnalysisResult:
    result = database.fetch_analysis(analysis_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"analysis {analysis_id} not found")
    return result


@app.get(
    "/analyses/{analysis_id}/risk-groups",
    response_model=list[RiskGroup],
    tags=["analysis"],
    summary="按已保存分析结果生成风险分组（违规产物与来源载频的二部图连通分量）",
    description=(
        "仅使用已持久化的分析结果：每个违规产物为一个风险节点，其全部来源组合中的 "
        "fa_hz/fb_hz 载频与该节点连边，按连通分量归组；非违规产物不参与。"
        "无违规产物时返回空列表。"
    ),
)
def get_risk_groups(analysis_id: int) -> list[RiskGroup]:
    result = database.fetch_analysis(analysis_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"analysis {analysis_id} not found")
    return build_risk_groups(result.products)


@app.get(
    "/analyses/{analysis_id}/mitigation-plan",
    response_model=MitigationPlan,
    tags=["analysis"],
    summary="为已保存分析的全部违规产物求最小停用载频集合（精确最小点覆盖）",
    description=(
        "仅使用已持久化的分析结果，不重新扫描。每个违规产物的每个来源组合 "
        "(fa_hz, fb_hz) 中至少停用一个载频，该产物才算被消除；在停用数不超过 "
        "max_disabled（1～8）的前提下精确求基数最小的停用集合，存在多个最小解时"
        "按载频升序列表的字典序择优。上限内无解时 feasible=false，并给出上限内"
        "消除产物最多的尽力方案与仍未消除的产物频率。分析不存在返回 404。"
    ),
)
def get_mitigation_plan(
    analysis_id: int,
    max_disabled: int = Query(
        ..., ge=1, le=8, description="允许停用的最大载频数，取 1～8 的整数"
    ),
) -> MitigationPlan:
    result = database.fetch_analysis(analysis_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"analysis {analysis_id} not found")
    return build_mitigation_plan(result.products, analysis_id, max_disabled)


@app.post(
    "/cross-site-conflicts",
    response_model=CrossSiteConflictResponse,
    tags=["cross-site"],
    summary="跨站点互调冲突检测：不同站点产物按频率容差配对并聚合为冲突簇",
    description=(
        "接收 2～8 个已保存站点 id 与非负频率容差 tolerance_hz，读取每个站点"
        "**最近一次**分析结果（不重新扫描、不保存任何数据）。在**不同站点**的"
        "产物之间，凡频率差 |fa - fb| <= tolerance_hz 即形成一对匹配（同站点内"
        "不配对）；以匹配关系为无向边、(站点, 产物频率) 为节点取连通分量，"
        "每个连通分量即一个冲突簇。响应逐簇列出涉及站点、产物频率（含各自"
        "analysis_id）、簇内最小频率差 min_delta_hz 与全部配对明细；无任何"
        "匹配时返回空簇列表。站点 id 数量越界、非正或重复、容差非法 -> 422；"
        "站点不存在 -> 404（列出不存在的 id）；站点尚无分析 -> 404（列出"
        "缺少分析的站点 id）。"
    ),
)
def detect_cross_site_conflicts(
    payload: CrossSiteConflictRequest,
) -> CrossSiteConflictResponse:
    requested = payload.site_ids
    existing = database.existing_site_ids(requested)
    missing_sites = [site_id for site_id in requested if site_id not in existing]
    if missing_sites:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"site(s) not found: {missing_sites}",
        )
    latest = database.fetch_latest_analyses(requested)
    sites_without_analysis = [site_id for site_id in requested if site_id not in latest]
    if sites_without_analysis:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no analysis yet for site(s): {sites_without_analysis};"
            " POST /sites/{site_id}/analysis first",
        )
    # 请求顺序回显；聚类内部按 site_id 规范化，不依赖请求排列
    analyses = [latest[site_id] for site_id in requested]
    clusters = build_cross_site_conflicts(analyses, payload.tolerance_hz)
    return CrossSiteConflictResponse(
        tolerance_hz=payload.tolerance_hz,
        site_ids=requested,
        cluster_count=len(clusters),
        pair_count=sum(len(cluster.pairs) for cluster in clusters),
        clusters=clusters,
    )
