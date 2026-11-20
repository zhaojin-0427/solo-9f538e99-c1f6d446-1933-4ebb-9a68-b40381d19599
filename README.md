# 空运危险品保管链 API

面向断网后乱序补传场景的保管链（chain of custody）服务。数据员可以据此判断：
在某个时刻之前，哪些交接仍然可信、断链发生在哪里、当前可确认的保管方是谁。

技术栈：**FastAPI + SQLite（WAL 模式）+ Uvicorn**，可通过 `docker compose` 一键启动。

## 核心规则

1. **固定起始保管方**：创建运单时登记 `origin_party`，初始可确认保管方即该方。
2. **批量提交**：每笔扫描携带 `scan_id`、正整数 `revision`、UTC 时刻 `ts_utc`、
   交出方 `giver`、接收方 `receiver`。时刻必须带时区偏移（如 `...Z`、`+08:00`），统一归一化为 UTC 存储。
3. **撤回**：只提交 `scan_id` 与更高的 `revision`，`event_type=WITHDRAWAL`，
   不得携带时刻或当事方；撤回只能针对已存在的扫描 ID。
4. **修订裁决**：
   - 同一 `scan_id` 仅采用**最高修订号**的版本；更低修订号提交返回 `409 stale`。
   - 同修订号、异文（时刻/交出方/接收方/类型不同）**整批拒绝**（`409`），已提交数据原样保留。
   - 同修订号、同文重复提交幂等，不计入写入数。
5. **链核对**（按 UTC 时刻升序，乱序补传自动归位）：
   - 交接的 `giver` 必须等于当前保管方（首笔对照起始保管方，其后对照上一笔的 `receiver`），
     不匹配即为 **GAP 断链**。
   - **同一时刻存在多笔交接时不猜测顺序**：该时刻整组标记为 **SIMULTANEOUS** 断链，组内全部未证实。
   - 断链之后的所有扫描一律 `verified=false`，直到补报、更正或撤回补齐缺口；
     重算后断链位置可以前移、后移或消失。
6. **交接计划与会合核对**：见下节。计划、扫描与核对结果始终从同一已提交快照读取；
   提交失败（整批回滚）时计划与扫描都保持原状，照常可继续接收扫描。
7. **原子性与一致性**：
   - 每个批次在单个 `BEGIN IMMEDIATE` 事务内完成，任何冲突整批回滚，**失败保留旧链**。
   - 查询只读已提交快照（WAL），交错的扫描提交与查询永远看不到半写状态或混合修订。
   - 数据持久化在 SQLite 文件中（含计划表），进程重启后状态不变。

## 启动方式

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务： http://localhost:8000
# 文档： http://localhost:8000/docs
# 控制台：http://localhost:8000/
```

数据库保存在命名卷 `chain-data`（容器内 `/data/chain.db`），容器重启不丢数据。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 可选：APP_DB_PATH=/custom/path/chain.db
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/waybills` | 创建运单并固定起始保管方 |
| GET | `/api/waybills` | 列出全部运单 |
| POST | `/api/waybills/{waybill_id}/scans/batch` | 批量提交交接 / 更正 / 撤回，回执含计划版号与核对结果 |
| GET | `/api/waybills/{waybill_id}/chain` | 查询可信前缀、断链原因、当前保管方及核对结果 |
| PUT | `/api/waybills/{waybill_id}/plan` | 登记 / 整版替换交接计划（`POST` 同义） |
| GET | `/api/waybills/{waybill_id}/plan` | 读取当前计划；未登记时 `revision` 为 `null` |
| GET | `/api/waybills/{waybill_id}/reconciliation` | 查询计划核对结果 |
| GET | `/docs` | Swagger UI，可直接录入与查询 |

`/plan` 也可写作 `/handover-plan`、`/handover_plan` 或 `/plans`（同样的请求与响应）。

## 交接计划与核对

运单到终点后，可先登记一份交接计划，再用扫描核对途中是否完成备案交接。
计划按运单整体登记、整版替换；成功回执带上本次提交对应的计划版号和核对结果。

```json
{
  "revision": 1,
  "items": [
    {"receiver": "CX-CARGO", "window_start": "2026-11-18T07:00:00Z",
     "window_end": "2026-11-18T09:00:00Z", "required": true},
    {"receiver": "PVG-WAREHOUSE", "window_start": "2026-11-18T09:00:00Z",
     "window_end": "2026-11-18T11:00:00Z", "required": false}
  ]
}
```

* 最多 60 项；逐项给出接收方、含首尾的 UTC 时间窗（`start <= end`）和是否必经。
  接收方可以重复。
* `revision` 是本次提交对应的计划版号（回执原样带回）；省略时自动取当前版号 + 1
  （首次登记为 1）。整版替换总是**整体替换**项目列表，不做合并。
* 字段也接受常见别名（`plan_revision`/`version`、`plan`/`entries`、`party`/`custodian`、
  `start`/`end`/`from_utc`/`to_utc`、`mandatory`，以及嵌套的 `window: {start, end}`）。
* 核对只使用**可信前缀**（链核对已证实、按时间排序的交接扫描），并读取同一已提交快照
  —— 计划与扫描绝不会来自不同提交。

### 对应规则

按扫描顺序，每个扫描各对应一个**不同**的计划项，且序号递增；接收方必须相同，
扫描时刻必须落在该项**含首尾**的时间窗内（UTC 归一化后比较）。

* 首笔之前、两笔之间只允许略过**非必经**项；必经项不可略过。
* 最后一笔之后的计划项可以留待后续扫描（必经与否均可）。

### 结果

**无合法对应时**，返回最早使扫描前缀无法对应的扫描：`unresolved.scan_id`、
`unresolved.revision`、`unresolved.prefix_length`（同时以
`earliest_unmatched_scan_id` / `earliest_unmatched_revision` 冗余给出便于直接取用）。
此时逐项/逐扫描状态为空，`complete` 为假。

**存在合法对应时**，按所有合法对应逐项与逐扫描标注：

| 状态 | 含义 |
| --- | --- |
| `CONFIRMED` | 确定经过：在**每一**合法对应中都落到该项 / 该扫描只可能对应这一项 |
| `POSSIBLE` | 可能经过：只在部分合法对应中出现 |
| `UNSUPPORTED` | 尚无可信依据：不出现在任何合法对应中（计划项）/ 无法被任何对应匹配（扫描） |

`match.correspondence_count` 给出合法对应总数（按扫描→计划项的不同指派计数）。

**走完（`complete`）** 需同时满足：`chain_intact` 为真，且**每种**合法对应都覆盖
全部必经项（即每个必经项都是 `CONFIRMED`）。链断或仍有可能漏掉必经项时都不算走完。


### 交接批次示例

```json
{
  "scans": [
    {"scan_id": "S1", "revision": 1, "ts_utc": "2026-11-18T08:00:00Z",
     "giver": "HKG-WAREHOUSE", "receiver": "CX-CARGO"},
    {"scan_id": "S2", "revision": 1, "ts_utc": "2026-11-18T10:30:00Z",
     "giver": "CX-CARGO", "receiver": "PVG-WAREHOUSE"}
  ]
}
```

### 撤回示例（修订号必须更高）

```json
{"scans": [{"scan_id": "S2", "revision": 2, "event_type": "WITHDRAWAL"}]}
```

### 查询响应示例（发生断链时）

```json
{
  "waybill_id": "AWB-001",
  "origin_party": "HKG-WAREHOUSE",
  "current_custodian": "CX-CARGO",
  "verified_prefix": [ {"scan_id": "S1", "verified": true, "...": "..."} ],
  "unverified_scans": [ {"scan_id": "S3", "verified": false,
                         "unverified_reason": "GAP"} ],
  "chain_intact": false,
  "break_reason": "GAP",
  "break_detail": "giver 'X' does not hold custody; expected 'CX-CARGO' ...",
  "break_scan_ids": ["S3"],
  "plan_revision": 1,
  "reconciliation": {
    "plan_revision": 1,
    "has_plan": true,
    "scans_considered": 1,
    "match": {"correspondence_count": 1, "max_scans": 1},
    "items": [{"index": 0, "receiver": "CX-CARGO", "required": true,
               "status": "CONFIRMED", "supporting_scan_ids": ["S1"]}],
    "scans": [{"scan_id": "S1", "revision": 1,
               "status": "CONFIRMED", "plan_indices": [0]}],
    "unresolved": null,
    "chain_intact": false,
    "required_covered": true,
    "complete": false
  }
}
```

`break_reason` 取值：`NONE` / `GAP`（保管方不承接）/ `SIMULTANEOUS`（同时刻多笔，顺序不可判定）。
`current_custodian` 只沿已证实交接推进，因此断链后仍可信地停留在最后一个已证实接收方。

## 测试

```bash
pip install pytest httpx
pytest -q
```

共 49 个用例，覆盖：乱序补传归位、时区归一化、最高修订裁决、同修订异文整批回滚、
撤回与补报修复断链、GAP/SIMULTANEOUS 检测、输入校验、真实线程下的写串行化、
交错读不到混合修订、进程重启持久化，以及计划登记/整版替换、版号回传、
含首尾时间窗、必经项略过规则、合法对应计数与 CONFIRMED/POSSIBLE/UNSUPPORTED 标注、
最早无法对应的扫描定位、提交回执与查询的一致性、重启后仍可核对。
