"""API 端到端测试：使用临时 SQLite 库，通过 TestClient 走完整请求链路。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("IMD_DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:  # 触发 lifespan -> init_db
        yield c


def make_payload(**overrides):
    payload = {
        "name": "site-a",
        "scan_start_hz": 1,
        "scan_end_hz": 10_000,
        "guard_interval_hz": 150,
        "carriers_hz": [100, 300, 400],
        "guard_bands": [],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- 站点创建与校验

def test_create_site_ok(client):
    resp = client.post("/sites", json=make_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == 1
    assert body["carriers_hz"] == [100, 300, 400]
    assert body["guard_interval_hz"] == 150
    # 查询已保存方案
    assert client.get(f"/sites/{body['id']}").json()["name"] == "site-a"
    assert len(client.get("/sites").json()) == 1


def test_duplicate_carriers_rejected_as_whole_batch(client):
    resp = client.post("/sites", json=make_payload(carriers_hz=[100, 200, 100]))
    assert resp.status_code == 422
    assert client.get("/sites").json() == []  # 整批拒绝，未写入任何数据


def test_invalid_guard_band_rejected_as_whole_batch(client):
    bad = make_payload(guard_bands=[{"start_hz": 500, "end_hz": 500}])
    assert client.post("/sites", json=bad).status_code == 422
    bad2 = make_payload(guard_bands=[{"start_hz": 900, "end_hz": 100}])
    assert client.post("/sites", json=bad2).status_code == 422
    assert client.get("/sites").json() == []


def test_invalid_scan_range_rejected(client):
    assert client.post("/sites", json=make_payload(scan_end_hz=1)).status_code == 422
    assert client.get("/sites").json() == []


def test_non_positive_values_rejected(client):
    assert client.post("/sites", json=make_payload(carriers_hz=[0, 100])).status_code == 422
    assert client.post("/sites", json=make_payload(guard_interval_hz=-1)).status_code == 422
    assert client.get("/sites").json() == []


def test_duplicate_site_name_conflict(client):
    assert client.post("/sites", json=make_payload()).status_code == 201
    resp = client.post("/sites", json=make_payload())
    assert resp.status_code == 409
    sites = client.get("/sites").json()
    assert len(sites) == 1  # 第二个方案未残留


def test_get_missing_site_404(client):
    assert client.get("/sites/999").status_code == 404


# ---------------------------------------------------------------- 互调分析

def test_analysis_aggregates_same_frequency_sources(client):
    # 载频 [100, 300, 400]：2*300-100=500 与 2*400-300=500 同频，须聚合
    client.post("/sites", json=make_payload())
    resp = client.post("/sites/1/analysis")
    assert resp.status_code == 201, resp.text
    body = resp.json()

    freqs = [p["freq_hz"] for p in body["products"]]
    assert freqs == sorted(freqs)  # 按频率稳定排序
    assert freqs == [200, 500, 700]  # 范围外产物（<=0）已被过滤

    p500 = next(p for p in body["products"] if p["freq_hz"] == 500)
    assert {tuple(s.values()) for s in p500["sources"]} == {(300, 100), (400, 300)}

    # 命中对象与最短距离
    p200 = next(p for p in body["products"] if p["freq_hz"] == 200)
    assert p200["distance_hz"] == 100
    assert p200["hit"] == {"type": "carrier", "carrier_hz": 100, "guard_band": None}
    assert p200["violates_guard_interval"] is True  # 100 < 150

    p700 = next(p for p in body["products"] if p["freq_hz"] == 700)
    assert p700["hit"]["carrier_hz"] == 400
    assert p700["distance_hz"] == 300
    assert p700["violates_guard_interval"] is False  # 300 >= 150


def test_analysis_hits_guard_band_with_zero_distance(client):
    payload = make_payload(
        carriers_hz=[1000, 1100],
        guard_bands=[{"start_hz": 880, "end_hz": 920}],
        guard_interval_hz=50,
    )
    client.post("/sites", json=payload)
    body = client.post("/sites/1/analysis").json()

    products = {p["freq_hz"]: p for p in body["products"]}
    assert set(products) == {900, 1200}

    # 900 落在保护频段 [880, 920] 内：距离 0，命中频段
    assert products[900]["distance_hz"] == 0
    assert products[900]["hit"]["type"] == "guard_band"
    assert products[900]["hit"]["guard_band"] == {"start_hz": 880, "end_hz": 920}
    assert products[900]["violates_guard_interval"] is True

    # 1200 距载频 1100 为 100，距频段最近边界 280 -> 命中载频
    assert products[1200]["hit"] == {"type": "carrier", "carrier_hz": 1100, "guard_band": None}
    assert products[1200]["distance_hz"] == 100


def test_analysis_filters_out_of_scan_range(client):
    payload = make_payload(carriers_hz=[200, 500], scan_start_hz=500, scan_end_hz=900)
    client.post("/sites", json=payload)
    body = client.post("/sites/1/analysis").json()
    # 2*200-500=-100 与 2*500-200=800：仅 800 在 [500, 900] 内
    assert [p["freq_hz"] for p in body["products"]] == [800]
    assert body["product_count"] == 1


def test_analysis_persisted_and_queryable(client):
    client.post("/sites", json=make_payload())
    created = client.post("/sites/1/analysis").json()

    latest = client.get("/sites/1/analysis").json()
    assert latest == created  # 查询已保存的分析结果

    by_id = client.get(f"/analyses/{created['analysis_id']}").json()
    assert by_id == created

    metas = client.get("/sites/1/analyses").json()
    assert [m["analysis_id"] for m in metas] == [created["analysis_id"]]
    assert metas[0]["product_count"] == created["product_count"]


def test_analysis_404s(client):
    assert client.post("/sites/999/analysis").status_code == 404
    assert client.get("/sites/999/analysis").status_code == 404
    assert client.get("/analyses/999").status_code == 404
    client.post("/sites", json=make_payload())
    assert client.get("/sites/1/analysis").status_code == 404  # 尚未分析


# ---------------------------------------------------------------- 风险分组

def test_risk_groups_404(client):
    assert client.get("/analyses/999/risk-groups").status_code == 404


def test_risk_groups_empty_when_no_violation(client):
    # guard_interval_hz=0：distance < 0 永不成立，无违规产物 -> 空列表
    client.post("/sites", json=make_payload(guard_interval_hz=0))
    analysis_id = client.post("/sites/1/analysis").json()["analysis_id"]
    resp = client.get(f"/analyses/{analysis_id}/risk-groups")
    assert resp.status_code == 200
    assert resp.json() == []


def test_risk_groups_merge_multi_source_and_shared_carriers(client):
    # 载频 [100, 300, 400]：违规产物 200（来源 (300,400)）与 500（来源 (300,100)、
    # (400,300)，同频已合并）共享载频 300/400，归为一组；非违规的 700 不参与
    client.post("/sites", json=make_payload())
    analysis_id = client.post("/sites/1/analysis").json()["analysis_id"]
    resp = client.get(f"/analyses/{analysis_id}/risk-groups")
    assert resp.status_code == 200
    assert resp.json() == [
        {
            "group_id": 1,
            "carriers_hz": [100, 300, 400],
            "product_freqs_hz": [200, 500],
            "source_pair_count": 3,  # 200 有 1 个来源，500 有 2 个来源
            "distance_hz": 100,  # min(100, 100)
        }
    ]


def test_risk_groups_split_disjoint_components(client):
    # 两簇载频各产生违规产物；非违规产物 9900（来源含载频 100 与 5000）
    # 不参与分组，不得把两簇桥接成一组
    payload = make_payload(
        carriers_hz=[100, 200, 5000, 6000],
        scan_start_hz=1,
        scan_end_hz=100000,
        guard_interval_hz=1100,
    )
    client.post("/sites", json=payload)
    analysis_id = client.post("/sites/1/analysis").json()["analysis_id"]
    groups = client.get(f"/analyses/{analysis_id}/risk-groups").json()
    assert groups == [
        {
            "group_id": 1,
            "carriers_hz": [100, 200],
            "product_freqs_hz": [300],
            "source_pair_count": 1,
            "distance_hz": 100,
        },
        {
            "group_id": 2,
            "carriers_hz": [5000, 6000],
            "product_freqs_hz": [4000, 7000],
            "source_pair_count": 2,
            "distance_hz": 1000,
        },
    ]


def test_risk_groups_transitive_carrier_sharing(client):
    # 300 的载频 {100,200} 与 1200 的 {400,800} 不直接共享，
    # 仅经 600/700 链式共享载频传递归为一组
    payload = make_payload(
        carriers_hz=[100, 200, 400, 800],
        scan_start_hz=1,
        scan_end_hz=2000,
        guard_interval_hz=450,
    )
    client.post("/sites", json=payload)
    analysis_id = client.post("/sites/1/analysis").json()["analysis_id"]
    groups = client.get(f"/analyses/{analysis_id}/risk-groups").json()
    assert groups == [
        {
            "group_id": 1,
            "carriers_hz": [100, 200, 400, 800],
            "product_freqs_hz": [300, 600, 700, 1200],
            "source_pair_count": 4,
            "distance_hz": 100,
        }
    ]


def test_risk_groups_use_persisted_result(client):
    # 分组基于已持久化的分析结果：同一 analysis_id 重复查询结果一致
    client.post("/sites", json=make_payload())
    analysis_id = client.post("/sites/1/analysis").json()["analysis_id"]
    first = client.get(f"/analyses/{analysis_id}/risk-groups").json()
    second = client.get(f"/analyses/{analysis_id}/risk-groups").json()
    assert first == second
    assert [g["group_id"] for g in first] == list(range(1, len(first) + 1))


# ---------------------------------------------------------------- 新增载频禁用区间

def test_carrier_exclusion_ranges_both_formulas_and_merge(client):
    # 载频 [1000, 5000]，g=10：违规产物位置为 (990,1010) 与 (4990,5010) 的整数点。
    # 公式一 2*x-c：x∈[996,1004]（c=1000，ceil(1991/2)/floor(2009/2) 检验除 2 取整）、
    # [2996,3004]（两个载频各贡献一次）、[4996,5004]（c=5000）；
    # 公式二 2*c-x：x∈[991,1009]、[4991,5009]、[8991,9009]（负区间被候选范围裁掉）；
    # 载频点 [1000,1000]、[5000,5000] 被相邻区间吸收合并。
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 5000], scan_end_hz=100000, guard_interval_hz=10,
    ))
    resp = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 100000},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "site_id": 1,
        "candidate_start_hz": 1,
        "candidate_end_hz": 100000,
        "exclusion_ranges": [
            {"start_hz": 991, "end_hz": 1009},
            {"start_hz": 2996, "end_hz": 3004},
            {"start_hz": 4991, "end_hz": 5009},
            {"start_hz": 8991, "end_hz": 9009},
        ],
    }


def test_carrier_exclusion_ranges_open_boundary(client):
    # 开边界：产物到载频的距离恰好等于 g 时不违规。单载频 1000、g=10 时
    # 禁用区间为 [991,1009]；若误用闭边界会得到 [990,1010]
    client.post("/sites", json=make_payload(
        carriers_hz=[1000], scan_end_hz=100000, guard_interval_hz=10,
    ))
    body = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 100000},
    ).json()
    assert body["exclusion_ranges"] == [{"start_hz": 991, "end_hz": 1009}]


def test_carrier_exclusion_ranges_with_guard_band(client):
    # 保护频段 [480,520]、g=150 产生违规产物位置 [331,669]，载频 1000 产生
    # [851,1149]；反解后载频点 [1000,1000] 被 [851,1149] 吸收
    client.post("/sites", json=make_payload(
        carriers_hz=[1000],
        guard_bands=[{"start_hz": 480, "end_hz": 520}],
        guard_interval_hz=150,
    ))
    body = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 10000},
    ).json()
    assert body["exclusion_ranges"] == [
        {"start_hz": 666, "end_hz": 834},
        {"start_hz": 851, "end_hz": 1149},
        {"start_hz": 1331, "end_hz": 1669},
    ]


def test_carrier_exclusion_ranges_clipped_to_candidate(client):
    # 与上一组相同的数据，候选范围 [1005, 4990]：首区间被裁剪，
    # [4991,5009] 与 [8991,9009] 整体落在候选范围外被丢弃
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 5000], scan_end_hz=100000, guard_interval_hz=10,
    ))
    body = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1005, "candidate_end_hz": 4990},
    ).json()
    assert body["exclusion_ranges"] == [
        {"start_hz": 1005, "end_hz": 1009},
        {"start_hz": 2996, "end_hz": 3004},
    ]


def test_carrier_exclusion_ranges_zero_guard_interval(client):
    # g=0：任何产物都不违规，仅现有载频点禁用；相邻载频点 [100,100] 与
    # [101,101] 合并为 [100,101]
    client.post("/sites", json=make_payload(
        carriers_hz=[100, 101, 105], guard_interval_hz=0,
    ))
    body = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 1000},
    ).json()
    assert body["exclusion_ranges"] == [
        {"start_hz": 100, "end_hz": 101},
        {"start_hz": 105, "end_hz": 105},
    ]


def test_carrier_exclusion_ranges_empty_when_nothing_forbidden(client):
    # 候选范围避开全部禁用位置时返回空列表
    client.post("/sites", json=make_payload(
        carriers_hz=[1000], scan_end_hz=100000, guard_interval_hz=10,
    ))
    body = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 2000, "candidate_end_hz": 3000},
    ).json()
    assert body["exclusion_ranges"] == []


def test_carrier_exclusion_ranges_404(client):
    resp = client.get(
        "/sites/999/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 100},
    )
    assert resp.status_code == 404


def test_carrier_exclusion_ranges_param_validation(client):
    client.post("/sites", json=make_payload())
    base = "/sites/1/carrier-exclusion-ranges"
    # 缺参 / 非正整数 / 非整数 -> 422
    assert client.get(base).status_code == 422
    assert client.get(base, params={"candidate_start_hz": 1}).status_code == 422
    for bad in (
        {"candidate_start_hz": 0, "candidate_end_hz": 100},
        {"candidate_start_hz": 1, "candidate_end_hz": -5},
        {"candidate_start_hz": "abc", "candidate_end_hz": 100},
        {"candidate_start_hz": 1.5, "candidate_end_hz": 100},
        {"candidate_start_hz": 200, "candidate_end_hz": 100},  # 起点大于终点
    ):
        resp = client.get(base, params=bad)
        assert resp.status_code == 422, bad
    # 起点等于终点合法（闭区间退化为单点候选范围）
    ok = client.get(base, params={"candidate_start_hz": 300, "candidate_end_hz": 300})
    assert ok.status_code == 200
    assert ok.json()["exclusion_ranges"] == [{"start_hz": 300, "end_hz": 300}]


def test_carrier_exclusion_ranges_does_not_save_analysis(client):
    # 该端点不执行也不保存分析：调用后仍无分析记录
    client.post("/sites", json=make_payload())
    resp = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 10000},
    )
    assert resp.status_code == 200
    assert client.get("/sites/1/analysis").status_code == 404
    assert client.get("/sites/1/analyses").json() == []




def _create_and_analyze(client, **payload_overrides):
    client.post("/sites", json=make_payload(**payload_overrides))
    return client.post("/sites/1/analysis").json()["analysis_id"]


def test_mitigation_plan_404(client):
    assert client.get("/analyses/999/mitigation-plan", params={"max_disabled": 3}).status_code == 404


def test_mitigation_plan_max_disabled_validation(client):
    analysis_id = _create_and_analyze(client)
    # 缺少必填参数 / 越界 / 非整数 -> 422
    assert client.get(f"/analyses/{analysis_id}/mitigation-plan").status_code == 422
    for bad in (0, 9, -1):
        resp = client.get(
            f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": bad}
        )
        assert resp.status_code == 422, bad
    assert (
        client.get(
            f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": "x"}
        ).status_code
        == 422
    )


def test_mitigation_plan_empty_when_no_violation(client):
    # guard_interval_hz=0：无违规产物，无需停用任何载频
    analysis_id = _create_and_analyze(client, guard_interval_hz=0)
    resp = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 1}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "analysis_id": analysis_id,
        "max_disabled": 1,
        "feasible": True,
        "disabled_carriers_hz": [],
        "eliminated_product_freqs_hz": [],
        "remaining_product_freqs_hz": [],
    }


def test_mitigation_plan_single_carrier_solution(client):
    # 载频 [1000, 1100]：违规产物 900 只有来源 (1000,1100)，停用其一即可，
    # 多个最小解时按字典序停用较小载频 1000
    analysis_id = _create_and_analyze(
        client,
        carriers_hz=[1000, 1100],
        guard_bands=[{"start_hz": 880, "end_hz": 920}],
        guard_interval_hz=50,
    )
    resp = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 1}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "analysis_id": analysis_id,
        "max_disabled": 1,
        "feasible": True,
        "disabled_carriers_hz": [1000],
        "eliminated_product_freqs_hz": [900],
        "remaining_product_freqs_hz": [],
    }


def test_mitigation_plan_lexicographic_tie_break(client):
    # 违规边 {100,200}/{200,400}/{400,100}/{400,800}：最小点覆盖大小为 2，
    # 多个最小解（{100,400}、{200,400}）中按升序列表字典序取 [100, 400]；
    # 上限 1 时无解，尽力方案停用 400 可消除 3 个产物、仅剩 300
    analysis_id = _create_and_analyze(
        client,
        carriers_hz=[100, 200, 400, 800],
        scan_end_hz=2000,
        guard_interval_hz=450,
    )
    infeasible = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 1}
    ).json()
    assert infeasible["feasible"] is False
    assert infeasible["disabled_carriers_hz"] == [400]
    assert infeasible["eliminated_product_freqs_hz"] == [600, 700, 1200]
    assert infeasible["remaining_product_freqs_hz"] == [300]

    feasible = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 2}
    ).json()
    assert feasible == {
        "analysis_id": analysis_id,
        "max_disabled": 2,
        "feasible": True,
        "disabled_carriers_hz": [100, 400],
        "eliminated_product_freqs_hz": [300, 600, 700, 1200],
        "remaining_product_freqs_hz": [],
    }


def test_mitigation_plan_all_sources_must_be_covered(client):
    # 产物 700 有两个互不共享载频的来源 (400,100) 与 (500,300)：只停一个载频
    # 不能消除它（必须两个来源组合各停用至少一个载频）
    analysis_id = _create_and_analyze(
        client,
        carriers_hz=[100, 300, 400, 500],
        scan_start_hz=695,
        scan_end_hz=710,
        guard_bands=[{"start_hz": 698, "end_hz": 702}],
        guard_interval_hz=10,
    )
    one = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 1}
    ).json()
    assert one["feasible"] is False
    # 只停一个载频最多覆盖一个来源组合，700 仍未消除；字典序最小的尽力选择是 100
    assert one["disabled_carriers_hz"] == [100]
    assert one["eliminated_product_freqs_hz"] == []
    assert one["remaining_product_freqs_hz"] == [700]

    two = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 2}
    ).json()
    assert two["feasible"] is True
    # 两条来源边 {100,400}、{300,500} 各停一个端点，字典序最小为 [100, 300]
    assert two["disabled_carriers_hz"] == [100, 300]
    assert two["eliminated_product_freqs_hz"] == [700]
    assert two["remaining_product_freqs_hz"] == []


def test_mitigation_plan_three_disjoint_edges(client):
    # 三对互不相连的载频各有一个定点违规产物（小保护间隔 + 窄保护频段，
    # 不规则间隔避免跨对产物恰好落在其他载频上而产生连边）：需恰好停用 3 个
    analysis_id = _create_and_analyze(
        client,
        carriers_hz=[1000, 1150, 4700, 4900, 12300, 12600],
        scan_start_hz=1,
        scan_end_hz=40000,
        guard_bands=[
            {"start_hz": 845, "end_hz": 855},
            {"start_hz": 4495, "end_hz": 4505},
            {"start_hz": 11995, "end_hz": 12005},
        ],
        guard_interval_hz=10,
    )
    two = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 2}
    ).json()
    assert two["feasible"] is False
    # 上限 2 只能消除两对中的产物；消除数并列时按字典序取较小两对的小端点
    assert two["disabled_carriers_hz"] == [1000, 4700]
    assert two["eliminated_product_freqs_hz"] == [850, 4500]
    assert two["remaining_product_freqs_hz"] == [12000]

    three = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 3}
    ).json()
    assert three["feasible"] is True
    assert three["disabled_carriers_hz"] == [1000, 4700, 12300]
    assert three["eliminated_product_freqs_hz"] == [850, 4500, 12000]
    assert three["remaining_product_freqs_hz"] == []

    # 上限 8 同样返回基数最小（3）的解，不多停用载频
    eight = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 8}
    ).json()
    assert eight["feasible"] is True
    assert eight["disabled_carriers_hz"] == [1000, 4700, 12300]


def test_mitigation_plan_uses_persisted_result(client):
    # 仅使用已保存结果：同一 analysis_id 重复查询（含不同上限）结果确定可复现
    analysis_id = _create_and_analyze(client)
    first = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 2}
    ).json()
    second = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 2}
    ).json()
    assert first == second


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


# ---------------------------------------------------------------- 跨站点互调冲突

def make_sites_and_analyze(client, carriers_by_name):
    """按给定 {name: carriers} 依次创建站点并执行分析，返回 [site_id, ...]。"""
    ids = []
    for name, carriers in carriers_by_name.items():
        resp = client.post(
            "/sites", json=make_payload(name=name, carriers_hz=list(carriers))
        )
        assert resp.status_code == 201, resp.text
        site_id = resp.json()["id"]
        assert client.post(f"/sites/{site_id}/analysis").status_code == 201
        ids.append(site_id)
    return ids


def _make_sites_without_analysis(client, names):
    ids = []
    for name in names:
        resp = client.post("/sites", json=make_payload(name=name))
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["id"])
    return ids


def test_cross_site_conflicts_single_pair_basic(client):
    # 站点 a 产物含 500，站点 b 产物含 510：容差 20 配对，10 也配对（<= 边界），
    # 9 不配对
    ids = make_sites_and_analyze(client, {"a": [100, 300, 400], "b": [1000, 1490]})
    resp = client.post(
        "/cross-site-conflicts", json={"site_ids": ids, "tolerance_hz": 20}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tolerance_hz"] == 20
    assert body["site_ids"] == ids  # 按请求顺序回显
    assert body["cluster_count"] == 1
    assert body["pair_count"] == 1
    assert body["clusters"] == [
        {
            "cluster_id": 1,
            "site_ids": ids,
            "products": [
                {"site_id": ids[0], "freq_hz": 500, "analysis_id": 1},
                {"site_id": ids[1], "freq_hz": 510, "analysis_id": 2},
            ],
            "min_delta_hz": 10,
            "pairs": [
                {
                    "site_a_id": ids[0],
                    "freq_a_hz": 500,
                    "site_b_id": ids[1],
                    "freq_b_hz": 510,
                    "delta_hz": 10,
                }
            ],
        }
    ]

    boundary = client.post(
        "/cross-site-conflicts", json={"site_ids": ids, "tolerance_hz": 10}
    ).json()
    assert boundary["cluster_count"] == 1  # 差恰好等于容差也算配对（闭区间语义）

    none = client.post(
        "/cross-site-conflicts", json={"site_ids": ids, "tolerance_hz": 9}
    ).json()
    assert none == {
        "tolerance_hz": 9,
        "site_ids": ids,
        "cluster_count": 0,
        "pair_count": 0,
        "clusters": [],
    }


def test_cross_site_conflicts_chain_merges_into_one_cluster(client):
    # 500@a - 510@b - 520@c：500 与 520 差 20 但隔了站点 b，经链式配对仍归一簇
    ids = make_sites_and_analyze(
        client,
        {"a": [100, 300, 400], "b": [1000, 1490], "c": [480, 500]},
    )
    body = client.post(
        "/cross-site-conflicts", json={"site_ids": ids, "tolerance_hz": 10}
    ).json()
    assert body["cluster_count"] == 1
    assert body["pair_count"] == 2
    cluster = body["clusters"][0]
    assert cluster["site_ids"] == ids
    assert cluster["min_delta_hz"] == 10
    assert [(p["site_id"], p["freq_hz"]) for p in cluster["products"]] == [
        (ids[0], 500),
        (ids[1], 510),
        (ids[2], 520),
    ]
    # 配对按 (freq_a, site_a, freq_b, site_b) 升序
    assert [
        (pair["freq_a_hz"], pair["site_a_id"], pair["freq_b_hz"], pair["site_b_id"])
        for pair in cluster["pairs"]
    ] == [
        (500, ids[0], 510, ids[1]),
        (510, ids[1], 520, ids[2]),
    ]


def test_cross_site_conflicts_two_clusters_via_shared_site(client):
    # 站点 a 产物 500、700；站点 b 产物 510、1980；站点 c 产物 1900。
    # 容差 80：a500-b510 成一簇；b1980-c1900 成另一簇；a700 无配对不入簇。
    # 两簇都涉及站点 b 但彼此没有可传递的配对，仍为两个独立分量。
    ids = make_sites_and_analyze(
        client,
        {"a": [100, 300, 400], "b": [1000, 1490], "c": [300, 1100]},
    )
    body = client.post(
        "/cross-site-conflicts", json={"site_ids": ids, "tolerance_hz": 80}
    ).json()
    assert body["cluster_count"] == 2
    assert body["pair_count"] == 2
    first, second = body["clusters"]
    assert first["site_ids"] == [ids[0], ids[1]]
    assert [(p["site_id"], p["freq_hz"]) for p in first["products"]] == [
        (ids[0], 500),
        (ids[1], 510),
    ]
    assert first["min_delta_hz"] == 10
    assert second["site_ids"] == [ids[1], ids[2]]
    assert [(p["site_id"], p["freq_hz"]) for p in second["products"]] == [
        (ids[1], 1980),
        (ids[2], 1900),
    ]
    assert second["min_delta_hz"] == 80
    # 容差 9：500/510（差 10）与 1900/1980（差 80）都不再配对，无冲突簇
    tight = client.post(
        "/cross-site-conflicts", json={"site_ids": ids, "tolerance_hz": 9}
    ).json()
    assert tight["cluster_count"] == 0


def test_cross_site_conflicts_two_disjoint_clusters_numbering(client):
    # 独立构造两簇：簇1 频率 ~500（站点 1/2），簇2 频率 ~700（站点 3/4），
    # 各站点只用一对载频精确产生目标产物，避免同站点额外产物桥接。
    ids = make_sites_and_analyze(
        client,
        {
            "s1": [300, 100],     # 500
            "s2": [300, 90],      # 510
            "s3": [400, 100],     # 700（同时产生 2*100-400=-200 越界被过滤）
            "s4": [400, 95],      # 705
        },
    )
    body = client.post(
        "/cross-site-conflicts", json={"site_ids": ids, "tolerance_hz": 10}
    ).json()
    assert body["cluster_count"] == 2
    assert body["pair_count"] == 2
    first, second = body["clusters"]
    assert first["cluster_id"] == 1
    assert first["site_ids"] == [ids[0], ids[1]]
    assert [p["freq_hz"] for p in first["products"]] == [500, 510]
    assert first["min_delta_hz"] == 10
    assert second["cluster_id"] == 2
    assert second["site_ids"] == [ids[2], ids[3]]
    assert [p["freq_hz"] for p in second["products"]] == [700, 705]
    assert second["min_delta_hz"] == 5
    # 调小容差只剩 700/705 一簇（500/510 差 10 被拆开）
    tight = client.post(
        "/cross-site-conflicts", json={"site_ids": ids, "tolerance_hz": 7}
    ).json()
    assert tight["cluster_count"] == 1
    assert tight["clusters"][0]["cluster_id"] == 1
    assert tight["clusters"][0]["products"][0]["freq_hz"] == 700


def test_cross_site_conflicts_same_site_products_never_pair(client):
    # 站点 a 产物 500 与 700（同站差 200 <= 容差也不得配对/桥接）；
    # 站点 b 产物 510：跨站对为 a500-b510(10) 与 a700-b510(190)，200@a 不入簇
    ids = make_sites_and_analyze(client, {"a": [100, 300, 400], "b": [1000, 1490]})
    body = client.post(
        "/cross-site-conflicts", json={"site_ids": ids, "tolerance_hz": 300}
    ).json()
    cluster = body["clusters"][0]
    assert sorted((p["site_id"], p["freq_hz"]) for p in cluster["products"]) == [
        (ids[0], 500),
        (ids[0], 700),
        (ids[1], 510),
    ]
    # 每一对的两端必须来自不同站点
    for pair in cluster["pairs"]:
        assert pair["site_a_id"] != pair["site_b_id"]
    assert {
        (pair["freq_a_hz"], pair["freq_b_hz"], pair["delta_hz"])
        for pair in cluster["pairs"]
    } == {(500, 510, 10), (510, 700, 190)}
    assert cluster["min_delta_hz"] == 10


def test_cross_site_conflicts_pair_orientation_on_equal_freq(client):
    # 两站点产生完全同频产物 500：delta=0，频率相同时站点 id 小者为 a
    ids = make_sites_and_analyze(client, {"a": [300, 100], "b": [300, 100]})
    body = client.post(
        "/cross-site-conflicts", json={"site_ids": ids, "tolerance_hz": 0}
    ).json()
    assert body["cluster_count"] == 1
    pair = body["clusters"][0]["pairs"][0]
    assert pair == {
        "site_a_id": ids[0],
        "freq_a_hz": 500,
        "site_b_id": ids[1],
        "freq_b_hz": 500,
        "delta_hz": 0,
    }


def test_cross_site_conflicts_request_order_independence(client):
    ids = make_sites_and_analyze(
        client,
        {"s1": [300, 100], "s2": [300, 90], "s3": [400, 100], "s4": [400, 95]},
    )
    first = client.post(
        "/cross-site-conflicts", json={"site_ids": ids, "tolerance_hz": 10}
    ).json()
    reversed_ids = list(reversed(ids))
    second = client.post(
        "/cross-site-conflicts", json={"site_ids": reversed_ids, "tolerance_hz": 10}
    ).json()
    # site_ids 按各自请求顺序回显；簇结构与编号完全一致
    assert first["site_ids"] == ids
    assert second["site_ids"] == reversed_ids
    assert first["clusters"] == second["clusters"]


def test_cross_site_conflicts_uses_latest_analysis(client, monkeypatch):
    # 读取每个站点“最近一次”分析：直接向库中写入一条更新的空结果分析后，
    # 端点应基于它判定无冲突；补一次正式分析后冲突恢复
    ids = make_sites_and_analyze(client, {"a": [300, 100], "b": [300, 90]})
    before = client.post(
        "/cross-site-conflicts", json={"site_ids": ids, "tolerance_hz": 10}
    ).json()
    assert before["cluster_count"] == 1

    from app import database

    database.insert_analysis(ids[1], [])  # 站点 b 的最新分析无产物
    mid = client.post(
        "/cross-site-conflicts", json={"site_ids": ids, "tolerance_hz": 10}
    ).json()
    assert mid["cluster_count"] == 0

    client.post(f"/sites/{ids[1]}/analysis")  # 新的正式分析成为最新
    after = client.post(
        "/cross-site-conflicts", json={"site_ids": ids, "tolerance_hz": 10}
    ).json()
    assert after["cluster_count"] == 1


def test_cross_site_conflicts_does_not_write_anything(client):
    ids = make_sites_and_analyze(client, {"a": [300, 100], "b": [300, 90]})
    metas_before = [client.get(f"/sites/{i}/analyses").json() for i in ids]
    for _ in range(2):
        resp = client.post(
            "/cross-site-conflicts", json={"site_ids": ids, "tolerance_hz": 10}
        )
        assert resp.status_code == 200
    metas_after = [client.get(f"/sites/{i}/analyses").json() for i in ids]
    assert metas_before == metas_after  # 纯读端点：不新增分析记录


def test_cross_site_conflicts_missing_sites_404(client):
    ids = make_sites_and_analyze(client, {"a": [300, 100]})
    # 全部不存在
    resp = client.post(
        "/cross-site-conflicts", json={"site_ids": [998, 999], "tolerance_hz": 1}
    )
    assert resp.status_code == 404
    assert "998" in resp.json()["detail"] and "999" in resp.json()["detail"]
    # 部分不存在：明确列出缺失 id，且不进行分析检查
    resp = client.post(
        "/cross-site-conflicts", json={"site_ids": [ids[0], 999], "tolerance_hz": 1}
    )
    assert resp.status_code == 404
    assert "999" in resp.json()["detail"]


def test_cross_site_conflicts_sites_without_analysis_404(client):
    analyzed = _make_sites_without_analysis(client, ["a", "b"])
    client.post(f"/sites/{analyzed[0]}/analysis")  # 只有站点 a 有分析
    resp = client.post(
        "/cross-site-conflicts",
        json={"site_ids": analyzed, "tolerance_hz": 1},
    )
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert str(analyzed[1]) in detail  # 明确指出缺分析的站点
    assert str(analyzed[0]) not in detail


def test_cross_site_conflicts_validation_422(client):
    ids = make_sites_and_analyze(client, {"a": [300, 100], "b": [300, 90]})
    url = "/cross-site-conflicts"

    def post(body):
        return client.post(url, json=body)

    # 站点数少于 2 / 多于 8
    assert post({"site_ids": [ids[0]], "tolerance_hz": 1}).status_code == 422
    assert post(
        {"site_ids": [1, 2, 3, 4, 5, 6, 7, 8, 9], "tolerance_hz": 1}
    ).status_code == 422
    # 重复站点 id
    assert post({"site_ids": [ids[0], ids[0]], "tolerance_hz": 1}).status_code == 422
    # 非正站点 id
    assert post({"site_ids": [0, ids[0]], "tolerance_hz": 1}).status_code == 422
    assert post({"site_ids": [ids[0], -3], "tolerance_hz": 1}).status_code == 422
    # 容差缺失 / 负数
    assert post({"site_ids": ids}).status_code == 422
    assert post({"site_ids": ids, "tolerance_hz": -1}).status_code == 422
    # 请求体缺失 / 字段类型错误
    assert client.post(url, json={}).status_code == 422
    assert post({"site_ids": ["a", "b"], "tolerance_hz": 1}).status_code == 422
    assert post({"site_ids": ids, "tolerance_hz": "x"}).status_code == 422
    # 恰好 8 个站点的数量上限本身合法（存在性另算 404）；2 个下限已由其它用例覆盖
    assert post(
        {"site_ids": [ids[0], ids[1], 3, 4, 5, 6, 7, 8], "tolerance_hz": 1}
    ).status_code == 404
