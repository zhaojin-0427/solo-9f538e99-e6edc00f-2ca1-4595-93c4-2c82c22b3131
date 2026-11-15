# 数据血缘影响分析与分区就绪核查 API（FastAPI + SQLite）

本地部署的数据血缘 / 日批就绪服务：批量登记**源字段 / 派生字段 / 报表列**的
依赖关系，提交拟下线字段后递归计算**确实失效的报表列**；并可批量记录源字段
在业务日期的**已发布 / 已撤回分区状态与修订号**，在启动日批前核查报表列是否
存在完整可用的输入路径。

## 语义规则

- 每个下游列可登记**多个候选输入组**（`candidate_groups`，组的数组）。
- 一个候选组可用 ⇔ 组内字段**全部**可用。
- 一个列可产出 ⇔ **任一**候选组可用；仍有可用组的列不会被误报。
- 源字段（`source`）没有输入；派生 / 报表列必须至少有一个非空候选组。
- 提交拟下线字段后沿血缘图递归传播：
  - 只有所有候选组都不可用的列才计入失效；
  - 返回失效报表列的**每一个**失效候选组及组内缺失字段；
  - 每个缺失字段附带 `root_causes`（追溯到最深层的拟下线字段，而非中间节点）。
- 登记批次若引入循环依赖（含自环），**整批回滚不生效**。

### 分区就绪规则

- 每个源字段、业务日期可上报多个**修订号**（`revision`，从 1 递增），
  状态为 `published` 或 `withdrawn`；**没有任何记录视为未发布**。
- 同一字段、日期只采用**最高修订**：
  - 低修订重传不会回退当前状态（`superseded`）；
  - 同修订号但状态不同属于冲突，**整批拒绝**（409）；
  - 同修订号同状态重传幂等，不改变结果（`unchanged`）。
- 引用**未登记字段或非源字段**时整批拒绝（422），批次内任何记录都不落库。
- 就绪核查沿血缘递归：源字段分区已发布才可用；更高修订撤回后按**备用候选组**
  重新判断，并列出每个受阻组缺少的具体源字段分区（未发布 / 已撤回 + 修订号）。
- 查询与血缘登记、分区更新并发时，只能看到**同一完整快照**。

## 目录结构

```
lineage-api/
├── app/
│   ├── main.py         # FastAPI 入口、异常映射、生命周期
│   ├── service.py      # 整批事务登记、循环检测、递归失效 / 就绪分析、分区修订规则
│   ├── db.py           # SQLite 连接 / WAL / 快照与写事务 / 分区表结构
│   ├── models.py       # Pydantic 请求与响应模型
│   └── exceptions.py   # 领域错误（422/409/404）
├── tests/              # pytest：功能用例 + 真实 uvicorn 并发用例
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── examples/           # 可直接调用的请求样例
```

## Docker Compose 启动

```bash
cd lineage-api
docker compose up -d --build
docker compose ps          # health: healthy
curl http://localhost:8000/health
```

- 服务监听 `8000`，SQLite 数据库落在命名卷 `lineage-data` 的
  `/data/lineage.db`（WAL 模式），容器重建后数据保留。
- 交互式文档：http://localhost:8000/docs （OpenAPI 3.1）。
- 停止与清理：

```bash
docker compose down          # 停止，保留数据卷
docker compose down -v       # 同时删除数据卷
```

## 本地（无 Docker）运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
LINEAGE_DB_PATH=./lineage.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 入口

### 1. 批量登记血缘 `POST /lineage/register`

同一批次内的节点可以互相引用；重复登记同名节点会**整体替换**其候选组。
任何校验失败、类型冲突或循环依赖都返回错误且整批不落库。

```bash
curl -s -X POST http://localhost:8000/lineage/register \
  -H 'Content-Type: application/json' \
  -d @examples/register.json
```

请求体：

```json
{
  "nodes": [
    {"name": "order_amt", "kind": "source"},
    {"name": "coupon_amt", "kind": "source"},
    {"name": "net_amt", "kind": "derived",
     "candidate_groups": [["order_amt", "coupon_amt"]]},
    {"name": "report_gmv", "kind": "report",
     "candidate_groups": [["net_amt"], ["order_amt"]]}
  ]
}
```

错误约定：

| HTTP | error                     | 场景                                       |
|------|---------------------------|--------------------------------------------|
| 422  | `validation_error`        | 空候选组、源字段带输入、引用未登记字段等   |
| 409  | `cycle_detected`          | 批次将引入循环依赖，整批回滚（附 cycle 路径）|
| 409  | `conflict`                | 同名节点已存在但类型不同                   |

### 2. 下线影响分析 `POST /lineage/impact`

```bash
curl -s -X POST http://localhost:8000/lineage/impact \
  -H 'Content-Type: application/json' \
  -d '{"retiring": ["coupon_amt"]}'
```

响应（节选）：

```json
{
  "retiring": ["coupon_amt"],
  "failed_report_columns": [
    {
      "name": "report_coupon_rate",
      "failed_groups": [
        {
          "group_index": 0,
          "inputs": ["coupon_amt", "order_amt"],
          "missing": [
            {"name": "coupon_amt", "kind": "source", "root_causes": ["coupon_amt"]}
          ]
        }
      ]
    }
  ],
  "unavailable_count": 3,
  "unavailable_nodes": ["coupon_amt", "net_amt", "report_coupon_rate"]
}
```

- `failed_report_columns` 只包含**所有候选组均失效**的报表列；
  仍有可用组（如上例中的 `report_gmv`，备用组 `[order_amt]` 可用）不会出现。
- `unavailable_nodes` 列出全部不可用节点（含中间派生列），方便逐级排查。
- 拟下线字段未登记时返回 `404 not_found`。

### 3. 查看当前血缘 `GET /lineage/graph`

返回最近一次已提交的完整血缘（节点类型 + 全部候选组）。

### 4. 健康检查 `GET /health`

### 5. 批量记录分区状态 `POST /readiness/partitions`

记录源字段在业务日期的发布 / 撤回状态及修订号，整批原子提交。

```bash
curl -s -X POST http://localhost:8000/readiness/partitions \
  -H 'Content-Type: application/json' \
  -d @examples/partitions.json
```

请求体：

```json
{
  "records": [
    {"field_name": "order_amt", "biz_date": "2026-11-14",
     "revision": 1, "status": "published"},
    {"field_name": "coupon_amt", "biz_date": "2026-11-14",
     "revision": 2, "status": "withdrawn"}
  ]
}
```

响应中每条记录给出 `outcome`：

| outcome       | 含义                                                 |
|---------------|------------------------------------------------------|
| `applied`     | 新写入的修订（高于此前最高修订或首次上报）           |
| `unchanged`   | 同修订同状态重传，幂等无变化                         |
| `superseded`  | 低于当前最高修订，被忽略且不回退状态                 |

错误约定：

| HTTP | error              | 场景                                                 |
|------|--------------------|------------------------------------------------------|
| 422  | `validation_error` | 引用未登记字段或非源字段（`source` 以外的节点）      |
| 409  | `conflict`         | 同字段 / 日期 / 修订号已存在不同状态，整批拒绝       |

### 6. 分区就绪核查 `POST /readiness/check`

按报表列的候选输入组，判断业务日期是否存在**完整可用路径**。

```bash
curl -s -X POST http://localhost:8000/readiness/check \
  -H 'Content-Type: application/json' \
  -d @examples/readiness.json
```

响应（所有源分区均未到达时）：

```json
{
  "report_column": "report_gmv",
  "biz_date": "2026-11-14",
  "ready": false,
  "available_groups": [],
  "blocked_groups": [
    {
      "group_index": 0,
      "inputs": ["net_amt"],
      "missing_partitions": [
        {"name": "coupon_amt", "status": "unpublished", "revision": null},
        {"name": "order_amt", "status": "unpublished", "revision": null}
      ]
    },
    {
      "group_index": 1,
      "inputs": ["order_amt"],
      "missing_partitions": [
        {"name": "order_amt", "status": "unpublished", "revision": null}
      ]
    }
  ]
}
```

- `ready=true` 时 `available_groups` 给出所有可用候选组下标；
- 受阻组的 `missing_partitions` 沿血缘追溯到**源字段分区**：
  无记录为 `unpublished`（`revision=null`），最高修订为撤回则为
  `withdrawn` 并带上生效修订号；
- 高修订撤回导致主组受阻但备用组完整时，`ready` 仍为 `true`，
  只有所有组都受阻才为 `false`；
- 报表列未登记返回 `404 not_found`，目标不是 `report` 节点返回 `422`。

## 并发与一致性设计

1. **WAL 模式**：读写不互相阻塞。
2. **登记串行化**：血缘登记与分区批次都使用 `BEGIN IMMEDIATE` 先拿写锁，
   并发写事务排队执行，配合 `busy_timeout` 避免 `database is locked`。
3. **查询快照隔离**：影响分析 / 血缘查询 / 就绪核查均在单连接显式事务内
   完成，SQLite 的快照读保证请求只看到**登记或分区更新前 / 后**的完整状态，
   不可能读到应用了一半的批次；就绪核查在同一快照内同时读取血缘与分区表。
4. **整批原子性**：节点与候选组分两阶段写入（先全部节点、再全部边），
   写入后做全图 DFS 环检测，发现环则抛错触发 `ROLLBACK`，批次内任何数据都不留存。
   分区批次先在事务内完成全部校验与逐条判定（冲突 / 低修订 / 幂等），
   再统一插入，任何冲突都整批回滚。
5. **修订历史保留**：`partition_status` 保留每个修订号的原始记录，
   生效状态按 `(field_name, biz_date)` 的 `MAX(revision)` 计算，
   低修订无法覆盖或删除高修订。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

覆盖：候选组 AND / OR 语义、递归失效与根因追溯、备用组存活不误报、
循环 / 自环整批回滚、各类 422/409/404、重复登记替换；
分区批次的整批拒绝（未登记 / 非源 / 同修订冲突）、低修订不回退、
同修订幂等、撤回后备用组重判、撤回 / 重发布修订演进、无记录视为未发布；
以及在真实 uvicorn 服务上持续 4 秒的并发读写（读者只观测到完整快照，
低修订写永远不生效，冲突批次不留残余状态）。
