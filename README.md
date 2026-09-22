# RF Intermodulation Analysis API（射频互调分析 API）

基于 **FastAPI + SQLite** 的射频三阶互调（`2·fa − fb`）分析服务。

创建站点（频率方案）并录入整数 Hz 表示的载频、扫描范围、保护频段与保护间隔；
服务端枚举不同载频组合的三阶互调产物，过滤扫描范围外的结果，对同频产物聚合全部来源组合，
并计算每个产物到载频或保护频段的最短距离。分析结果按频率稳定排序并持久化，可随时查询。

## 快速开始（单条命令）

```bash
docker compose up --build
```

启动后：

- **API 地址**：`http://localhost:8000`
- **交互式文档（Swagger UI）**：`http://localhost:8000/docs`
- **OpenAPI JSON**：`http://localhost:8000/openapi.json`
- **端口**：`8000`（宿主机 `8000` → 容器 `8000`，可在 `docker-compose.yml` 中修改）

数据库文件在容器启动时自动初始化（幂等建表），无需手动迁移；
数据持久化在命名卷 `imd-data`（容器内路径 `/data/intermod.db`）中。

停止并清理：`docker compose down`（加 `-v` 同时删除数据卷）。

## 分析语义

- **产物枚举**：对全部有序载频对 `(fa, fb)` 且 `fa ≠ fb` 计算 `2·fa − fb`。
- **范围过滤**：仅保留落在 `[scan_start_hz, scan_end_hz]`（闭区间）内的产物。
- **同频聚合**：同一产物频率的全部来源组合 `(fa, fb)` 聚合到一条结果中，按 `(fa, fb)` 升序排列。
- **最短距离与命中对象**：
  - 到载频的距离：`|f − fc|`；
  - 到保护频段的距离：落在带内为 `0`，否则为到最近边界的距离；
  - 取二者中的最小值作为 `distance_hz`，对应对象即 `hit`（`carrier` 或 `guard_band`）；
  - 距离相同时按确定性规则决胜：载频优先于保护频段，其次按参考频率升序。
- **保护间隔**：`violates_guard_interval = (distance_hz < guard_interval_hz)`，
  即产物到命中对象的间距小于保护间隔时判定为违规。
- **排序**：结果按产物频率升序；同频产物已聚合，频率唯一，排序稳定确定。

## 风险分组（risk groups）

`GET /analyses/{analysis_id}/risk-groups` 仅基于**已持久化**的分析结果计算，不重新扫描：

- **风险节点**：每个 `violates_guard_interval = true` 的违规产物是一个节点；
  非违规产物不参与（不会把不同簇桥接起来）。
- **连边**：节点的全部来源组合 `(fa_hz, fb_hz)` 中的载频都与该节点相连，
  构成"产物 ↔ 载频"二部图；同频产物的多来源已聚合在同一节点。
- **归组**：二部图的每个连通分量即一个风险组——共享载频的产物沿链式边传递归组。
- **每组字段**：`carriers_hz`（升序）、`product_freqs_hz`（升序）、
  `source_pair_count`（组内来源组合总数）、`distance_hz`（组内最小距离）。
- **稳定编号**：组按 `(最小载频, 最小产物频率)` 升序排序后，`group_id` 从 1 连续编号；
  各组载频集合互不相交，排序键唯一，编号稳定可复现。
- 分析不存在 → `404`；无违规产物 → `200` 返回空列表 `[]`。

## 停用方案（mitigation plan）

`GET /analyses/{analysis_id}/mitigation-plan?max_disabled=k` 仅基于**已持久化**的分析结果计算，不重新扫描。
查询参数 `max_disabled` 为 **1～8 的整数**（缺失、越界或非整数 → `422`）。

- **消除判定**：把每个违规产物的每个来源组合 `(fa_hz, fb_hz)` 看作连接载频 `fa_hz`、`fb_hz`
  的一条无向边；一个违规产物被消除，当且仅当它的**全部**来源边都至少有一个端点载频被停用。
  因此停用集合必须是全部违规来源边的**点覆盖**（vertex cover）。
- **精确最小解**：服务端在停用数不超过 `max_disabled` 的前提下求**基数最小**的点覆盖
  （FPT 分支限界，按"选一条未覆盖边 → 停用其两个端点之一"递归，上限 8 时至多
  2⁸ 个叶节点，配极大匹配下界剪枝，保证精确而非贪心近似）；迭代加深先保证停用数最少。
- **字典序择优**：存在多个基数相同的最小解时，按停用载频**升序列表的字典序**取最小者
  （例如 `[100, 400]` 优先于 `[200, 400]`）。结果恒为升序，重复查询稳定可复现。
- **可行响应**：`feasible=true`，`disabled_carriers_hz` 为最小停用集合，
  `eliminated_product_freqs_hz` 为全部违规产物频率（升序），`remaining_product_freqs_hz` 恒为 `[]`。
- **上限内无解**：`feasible=false`。此时返回上限内能**消除违规产物最多**的尽力方案
  （并列时覆盖的来源组合数更多、再并列时停用数更少、最后按升序列表字典序择优），
  `disabled_carriers_hz` 给出该方案，`remaining_product_freqs_hz` 列出仍未消除的产物频率（升序）。
  提高 `max_disabled` 后可能变为可行；可行解的停用数只取决于数据，不会因放宽上限而多停载频。
- 分析不存在 → `404`；无违规产物 → 任意上限都返回 `feasible=true` 与空集合。

## 新增载频禁用区间（carrier exclusion ranges）

`GET /sites/{site_id}/carrier-exclusion-ranges?candidate_start_hz=a&candidate_end_hz=b`
直接基于站点配置计算，**不执行也不保存分析**。查询参数为**正整数**且
`candidate_start_hz ≤ candidate_end_hz`（缺失、非正、非整数或起点大于终点 → `422`；
站点不存在 → `404`）。

- **禁用判定**：在整数位置 `x` 新增载频，会与每个现有载频 `c` 产生两类新三阶产物
  `2·x − c` 与 `2·c − x`。任一产物落在扫描范围 `[scan_start_hz, scan_end_hz]` 内，
  且到任一**现有**载频或保护频段的距离**严格小于** `guard_interval_hz` 时
  （与分析语义一致），`x` 即不宜新增；**现有载频点本身也视为禁用**。
- **整数 Hz 反解**：违规产物位置是开区间 `(ref − g, ref + g)`（`ref` 为载频或保护频段
  边界，`g` 为保护间隔），整数点端点各内缩 1 Hz；对 `2·x − c` 反解时除以 2
  起点向上取整、终点向下取整，对 `2·c − x` 取镜像区间。`g = 0` 时无违规产物位置，
  仅载频点禁用。
- **最小并集**：全部禁用位置裁剪到候选闭区间后，合并重叠或相邻（整数 Hz 下起点
  相差 ≤ 1）的区间，按起点升序返回 `exclusion_ranges`；无禁用位置时返回空列表。

响应示例（载频 `[1000]`、保护频段 `[480, 520]`、`guard_interval_hz=150`，候选范围 `[1, 10000]`）：

```json
{
  "site_id": 1,
  "candidate_start_hz": 1,
  "candidate_end_hz": 10000,
  "exclusion_ranges": [
    {"start_hz": 666, "end_hz": 834},
    {"start_hz": 851, "end_hz": 1149},
    {"start_hz": 1331, "end_hz": 1669}
  ]
}
```

## 跨站点互调冲突（cross-site conflicts）

`POST /cross-site-conflicts` 接收 **2～8 个已保存站点 id** 与非负频率容差
`tolerance_hz`，读取每个站点**最近一次**分析结果（纯读端点：不重新扫描、不保存任何数据）。

请求体：

```json
{"site_ids": [1, 2, 3], "tolerance_hz": 20}
```

- **配对规则**：在**不同站点**的产物之间，凡频率差 `|fa − fb| ≤ tolerance_hz`
  即形成一对匹配（闭区间语义：差恰好等于容差也算配对）。**同一站点内的产物不配对**，
  因而同站点关系不会把两个簇桥接起来；容差为 `0` 时只匹配严格同频的跨站点产物。
- **连通分量聚簇**：以匹配关系为无向边、`(站点, 产物频率)` 为节点取连通分量，
  每个连通分量即一个冲突簇——`500@A ↔ 510@B ↔ 520@C` 这类链式匹配全部归入同一簇，
  即使 `500` 与 `520` 的差已超出容差。未参与任何配对的产物不属于任何簇；无配对时返回空簇列表。
- **每簇字段**：
  - `site_ids`：簇内涉及的站点 id（升序）；
  - `products`：簇内全部产物，含各自的 `site_id`、`freq_hz` 与其来源 `analysis_id`，
    按 `(site_id, freq_hz)` 升序；
  - `min_delta_hz`：簇内全部配对的**最小频率差**（最接近的一对）；
  - `pairs`：全部配对明细，频率低者为 a、频率相同则站点 id 小者为 a，
    按 `(freq_a_hz, site_a_id, freq_b_hz, site_b_id)` 升序。
- **稳定编号**：簇按簇内最小 `(site_id, freq_hz)` 升序排序后 `cluster_id` 从 1 连续编号；
  结果只取决于各站点数据，与请求中站点 id 的排列顺序无关（`site_ids` 顶层字段按请求顺序回显）。
- **错误响应**：
  - 站点 id 少于 2、多于 8、非正整数或重复，`tolerance_hz` 缺失/负数/非整数 → `422`；
  - 站点不存在 → `404`，`detail` 列出不存在的 id；
  - 站点存在但尚无分析 → `404`，`detail` 列出缺少分析的站点 id
    （先 `POST /sites/{site_id}/analysis`）。

响应示例（站点 1 产物含 500 Hz、站点 2 产物含 510 Hz，容差 20 Hz）：

```json
{
  "tolerance_hz": 20,
  "site_ids": [1, 2],
  "cluster_count": 1,
  "pair_count": 1,
  "clusters": [
    {
      "cluster_id": 1,
      "site_ids": [1, 2],
      "products": [
        {"site_id": 1, "freq_hz": 500, "analysis_id": 1},
        {"site_id": 2, "freq_hz": 510, "analysis_id": 4}
      ],
      "min_delta_hz": 10,
      "pairs": [
        {"site_a_id": 1, "freq_a_hz": 500, "site_b_id": 2, "freq_b_hz": 510, "delta_hz": 10}
      ]
    }
  ]
}
```

## 校验规则（整批拒绝）

- 载频必须为正整数 Hz 且**不得重复**；至少 1 个载频。
- 扫描范围：`scan_end_hz > scan_start_hz`，均为正整数。
- 保护频段：`end_hz > start_hz`，均为正整数。
- 保护间隔：非负整数。
- 任一校验失败 → `422`，**整批拒绝**，不写入任何数据（多表写入在单个事务中完成，失败即回滚）。
- 站点名称全局唯一，重名 → `409`。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/sites` | 创建站点（频率方案），`201` |
| `GET` | `/sites` | 列出全部已保存方案 |
| `GET` | `/sites/{site_id}` | 查询单个已保存方案 |
| `GET` | `/sites/{site_id}/carrier-exclusion-ranges?candidate_start_hz=a&candidate_end_hz=b` | 候选闭区间内不宜新增载频的最小区间并集（不执行或保存分析） |
| `POST` | `/sites/{site_id}/analysis` | 执行互调分析并保存结果，`201` |
| `GET` | `/sites/{site_id}/analysis` | 查询该方案最近一次分析结果 |
| `GET` | `/sites/{site_id}/analyses` | 列出该方案的全部分析记录 |
| `GET` | `/analyses/{analysis_id}` | 按 id 查询已保存的分析结果 |
| `GET` | `/analyses/{analysis_id}/risk-groups` | 按已保存分析结果生成风险分组（违规产物 ↔ 来源载频的二部图连通分量） |
| `GET` | `/analyses/{analysis_id}/mitigation-plan?max_disabled=k` | 按已保存分析结果求最小停用载频集合（精确最小点覆盖，`max_disabled` 取 1～8；上限内无解返回 `feasible=false` 与未消除产物频率） |
| `POST` | `/cross-site-conflicts` | 跨站点冲突检测：不同站点最近分析产物按 `tolerance_hz` 配对，连通分量聚合为冲突簇（2～8 个站点 id；站点不存在或无分析 → 404，参数非法/重复 → 422） |

### 示例

```bash
# 创建方案
curl -X POST http://localhost:8000/sites \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "site-a",
        "scan_start_hz": 1,
        "scan_end_hz": 10000,
        "guard_interval_hz": 150,
        "carriers_hz": [100, 300, 400],
        "guard_bands": [{"start_hz": 480, "end_hz": 520}]
      }'

# 执行分析（载频 100/300/400 时，2*300-100 与 2*400-300 同为 500 Hz，会聚合为一条）
curl -X POST http://localhost:8000/sites/1/analysis

# 查询已保存的方案与分析结果
curl http://localhost:8000/sites/1
curl http://localhost:8000/sites/1/analysis

# 查询风险分组（违规产物与来源载频的连通分量）
curl http://localhost:8000/analyses/1/risk-groups

# 求最小停用方案（最多停用 2 个载频；仅使用已保存的分析结果）
curl 'http://localhost:8000/analyses/1/mitigation-plan?max_disabled=2'

# 查询候选范围 [1, 10000] 内不宜新增载频的区间（不执行或保存分析）
curl 'http://localhost:8000/sites/1/carrier-exclusion-ranges?candidate_start_hz=1&candidate_end_hz=10000'

# 跨站点冲突检测（先确保各站点都已执行过分析）
curl -X POST http://localhost:8000/cross-site-conflicts \
  -H 'Content-Type: application/json' \
  -d '{"site_ids": [1, 2, 3], "tolerance_hz": 20}'
```

分析响应节选：

```json
{
  "analysis_id": 1,
  "site_id": 1,
  "product_count": 3,
  "products": [
    {
      "freq_hz": 500,
      "distance_hz": 0,
      "hit": {"type": "guard_band", "carrier_hz": null,
              "guard_band": {"start_hz": 480, "end_hz": 520}},
      "violates_guard_interval": true,
      "sources": [{"fa_hz": 300, "fb_hz": 100}, {"fa_hz": 400, "fb_hz": 300}]
    }
  ]
}
```

风险分组响应示例（上例中违规产物 200 与 500 共享载频 300/400，归为一组）：

```json
[
  {
    "group_id": 1,
    "carriers_hz": [100, 300, 400],
    "product_freqs_hz": [200, 500],
    "source_pair_count": 3,
    "distance_hz": 0
  }
]
```

停用方案响应示例（`max_disabled=2`，停用 `[100, 400]` 即覆盖全部违规来源边）：

```json
{
  "analysis_id": 1,
  "max_disabled": 2,
  "feasible": true,
  "disabled_carriers_hz": [100, 400],
  "eliminated_product_freqs_hz": [200, 500],
  "remaining_product_freqs_hz": []
}
```

上限内无解时（例如三条互不相连的违规来源边但 `max_disabled=2`）：

```json
{
  "analysis_id": 1,
  "max_disabled": 2,
  "feasible": false,
  "disabled_carriers_hz": [1000, 4700],
  "eliminated_product_freqs_hz": [850, 4500],
  "remaining_product_freqs_hz": [12000]
}
```

## 本地开发（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 运行测试（48 个用例，覆盖创建、校验拒绝、聚合、距离、持久化查询、风险分组、最小停用方案、新增载频禁用区间、跨站点冲突检测）
pytest

# 启动服务，API 位于 http://127.0.0.1:8000
uvicorn app.main:app --reload
```

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `IMD_DB_PATH` | `./intermod.db`（容器内 `/data/intermod.db`） | SQLite 数据库文件路径 |

## 项目结构

```
app/
  main.py       # FastAPI 应用与路由，启动时初始化数据库
  analysis.py   # 三阶互调枚举、同频聚合、最近命中对象计算、风险分组（连通分量）、停用方案（精确最小点覆盖）、新增载频禁用区间反解、跨站点冲突配对聚簇
  database.py   # SQLite 建表与读写（单事务批量写入，批量读取多站点最近分析）
  schemas.py    # Pydantic 请求/响应模型与校验
tests/
  test_api.py   # 端到端测试（TestClient + 临时数据库）
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
