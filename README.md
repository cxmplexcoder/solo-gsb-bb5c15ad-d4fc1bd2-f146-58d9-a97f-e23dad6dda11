# 设备告警事件段聚合服务

基于 FastAPI + SQLite 的告警汇聚服务：客户端上报离散告警事件，服务按 **source + 可配置静默间隔** 将乱序到达的事件归入同一事件段（segment），并保证 **event_id 幂等去重**——重复提交不改变任何计数。误报事件可通过 `DELETE /events/{source}/{event_id}` 撤回，服务在单事务内删除事件并按静默间隔重算其原段（删除/收缩/拆分）。`GET /events` 提供事件审计查询：在单一只读快照内按 `(occurred_at, event_id)` 稳定排序、支持时间窗与 severity 多值过滤及游标分页。每次段变更都会同事务写入**单调递增的变更流**，下游可通过 `GET /segment-changes` 做增量同步。运维人员还可通过 `POST /segments/{id}/acknowledgements` 对段追加**不可变的人工确认审计记录**（revision 乐观锁 + 幂等键），并通过 `GET /segments/{id}/acknowledgements` 按确认序号升序查询。

## 快速开始

一条命令完成依赖安装、数据库初始化与服务启动：

```bash
docker compose up --build
```

服务地址：**http://localhost:8000**（交互式 API 文档：http://localhost:8000/docs）

数据库文件在首次启动时自动建表初始化，并通过命名卷 `alerts-data` 持久化。升级新版本时，新增表（如人工确认审计表）会在启动阶段以 `CREATE TABLE IF NOT EXISTS` 幂等补齐，**已有持久化卷上的历史数据原样保留**，无需手动迁移。

## 配置项

均通过环境变量配置（可在 `docker-compose.yml` 的 `environment` 中修改）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SILENCE_INTERVAL_SECONDS` | `300` | 静默间隔（秒）。同一 source 下，事件时间与事件段边界相距不超过该值即归入同一段；超过则开新段 |
| `DATABASE_PATH` | `/data/alerts.db`（容器）/ `data/alerts.db`（本地） | SQLite 数据库文件路径 |

## API

### 提交事件 `POST /events`

```bash
curl -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "edge-gateway-01",
    "event_id": "evt-0001",
    "occurred_at": "2026-09-20T08:00:00Z",
    "severity": "warning"
  }'
```

- `severity` 取值：`debug` / `info` / `warning` / `error` / `critical`（大小写不敏感，`warn`、`fatal` 为别名）
- `occurred_at` 为 ISO 8601 时间；不带时区时按 UTC 处理
- 响应：`{"deduplicated": false, "segment": {...}}`；重复提交同一 `(source, event_id)` 时 `deduplicated: true`，计数不变

### 撤回误报事件 `DELETE /events/{source}/{event_id}`

将误报事件撤回：在**单事务**内删除该事件，并按当前静默间隔对其**原段的剩余事件重新切分**。未知 `(source, event_id)` 返回 `404`；任一步失败整体回滚，事件、段与变更流都保持撤回前状态。

```bash
curl -X DELETE http://localhost:8000/events/edge-gateway-01/evt-0001
```

```json
{
  "source": "edge-gateway-01",
  "event_id": "evt-0001",
  "result": "split_segment",
  "segments": [ { "...": "最早子段（沿用原段 id，revision 递增）" },
                { "...": "其余子段（新建，revision=1）" } ]
}
```

`result` 与段的处理规则：

- **`deleted_segment`**：该事件是原段的唯一事件 → 删除事件与原段，`segments` 为空，变更流写一条 `deleted` 墓碑。
- **`shrunk_segment`**：移除后剩余事件仍为一段（边界/最高级别可能重算）→ 段**沿用原 id**，`revision` 一律 **+1**（即使边界未变），`segments` 含 1 个段。
- **`split_segment`**：该事件原本桥接了两侧事件（迟到事件造成的合并），移除后按静默间隔重新切成多段 → **最早子段沿用原段 id**（`revision` 在原值上 +1），**其余子段全部新建**（`revision=1`，不复用历史段 id），`segments` 按时间序返回全部子段。
- 重算内容：子段的起止时间、`event_count` 与 `max_severity` 均由剩余事件重新计算（例如撤回唯一的 `critical` 事件后最高级别回落）。撤回仅影响该事件所在段，不触碰同一 source 的其他段、更不影响其他 source。
- 撤回释放了 `(source, event_id)` 幂等键，之后可重新提交同一事件并照常归段。

### 事件审计查询 `GET /events`

分页审计查询已入库的原始事件。整个请求在**单一只读快照事务**（`query_only` 连接）内完成，排序键为 `(occurred_at, event_id)` **稳定升序**（同一时刻按 `event_id` 决定次序）；与 `POST /events` 同路径、HTTP 方法不同，互不影响。

```bash
curl 'http://localhost:8000/events?source=edge-gateway-01&start_at=2026-09-20T00:00:00Z&end_at=2026-09-21T00:00:00Z&severity=error&severity=critical&limit=100'
```

```json
{
  "events": [
    {
      "source": "edge-gateway-01",
      "event_id": "evt-0001",
      "occurred_at": "2026-09-20T08:00:00+00:00",
      "severity": "critical",
      "segment_id": 7
    }
  ],
  "next_after": "WzE3ODk4OTEyMDAuMCwiZXZ0LTAwMDEiXQ"
}
```

查询参数：

| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `source` | 是 | — | 只返回该来源的事件（1–256 字符） |
| `start_at` | 否 | — | 发生时间下界（**含边界**），ISO 8601；无时区按 UTC 处理；不得晚于 `end_at` |
| `end_at` | 否 | — | 发生时间上界（**含边界**），ISO 8601；无时区按 UTC 处理 |
| `severity` | 否 | — | 级别多值过滤，**可重复传入**（`?severity=error&severity=critical`），大小写不敏感并接受 `warn`/`fatal` 别名；不传表示不过滤 |
| `after` | 否 | 从头开始 | 上一页返回的 `next_after` **不透明游标**（请勿自行构造或解析） |
| `limit` | 否 | `100` | 每页条数，范围 1–**1000** |

- **`segment_id` 是查询时刻的实时归属**：事件归段可能因迟到事件合并、误报撤回重算而变化，该字段始终反映当前快照中的所属段 id（不保证与事件首次入库时的段一致）。
- **分页约定**：用上一页的 `next_after` 作为下一页的 `after`；游标编码的是 `(occurred_at, event_id)` 复合位置，分页期间新写入的事件只会获得更靠后的位置——**不漏读、不重复**。
- **空页游标保持不变**：到达末尾时返回 `{"events": [], "next_after": "<同一个 after>"}`，可直接用该游标继续轮询；首次请求（不带 `after`）且无数据时 `next_after` 为 `null`。
- **快照一致性**：全部过滤与分页读取在同一只读事务快照内完成，请求期间的并发写入不影响本次结果；接口纯读，不会改写事件、段或变更流。
- 参数校验失败（缺 `source`、`limit` 越界、非法 `severity`/时间、`start_at > end_at`、伪造游标）统一返回 `422`。

### 查询事件段详情 `GET /segments/{id}`

```bash
curl http://localhost:8000/segments/1
```

```json
{
  "id": 1,
  "source": "edge-gateway-01",
  "started_at": "2026-09-20T08:00:00+00:00",
  "ended_at": "2026-09-20T08:04:11+00:00",
  "max_severity": "critical",
  "event_count": 3,
  "revision": 2
}
```

### 列出事件段 `GET /segments`（可选 `?source=` 过滤）

```bash
curl 'http://localhost:8000/segments?source=edge-gateway-01'
```

### 人工确认事件段 `POST /segments/{id}/acknowledgements`

值班人员确认某事件段后，服务追加一条**不可变的确认审计记录**（只增、不提供修改/删除）。请求在单个 `BEGIN IMMEDIATE` 写事务内完成「幂等键检查 → 段存在性检查 → revision 乐观锁校验 → 追加记录」，保证并发确认与事件归段/撤回互不踩踏。

```bash
curl -X POST http://localhost:8000/segments/1/acknowledgements \
  -H 'Content-Type: application/json' \
  -d '{
    "operator": "ops-oncall-01",
    "note": "已电话联系现场确认，正在重启网关联调",
    "expected_revision": 2,
    "idempotency_key": "ack-20260920-0001"
  }'
```

请求字段：

| 字段 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- |
| `operator` | 是 | 1–256 字符 | 操作员标识（人工账号/工号） |
| `note` | 是 | 1–4096 字符 | 确认备注（处置说明、工单号等） |
| `expected_revision` | 是 | 整数 ≥ 1 | 客户端读取段时持有的 `revision`，用于乐观并发控制 |
| `idempotency_key` | 是 | 1–512 字符 | 客户端生成的幂等键；作用域为**单个段**（同键不同段互不影响） |

成功追加（HTTP `201`）：

```json
{
  "replayed": false,
  "acknowledgement": {
    "ack_id": 1,
    "segment_id": 1,
    "seq": 1,
    "operator": "ops-oncall-01",
    "note": "已电话联系现场确认，正在重启网关联调",
    "expected_revision": 2,
    "acknowledged_revision": 2,
    "idempotency_key": "ack-20260920-0001",
    "acknowledged_at": "2026-09-21T01:10:00+00:00"
  }
}
```

- **`seq`（确认序号）**：段内从 1 起独立计数、严格递增；`ack_id` 为全局内部主键。
- **`expected_revision` / `acknowledged_revision`**：前者是操作员提交时断言的版本，后者是落库时段的实际版本（校验通过时二者相同），一并留存以便审计"确认时看到的是哪一版"。
- 确认**不修改段本身**：段的计数、边界与 `revision` 都不因确认而变化。

**revision 不匹配（HTTP `409`）**：段在客户端读取后被新事件扩展、合并或撤回重算过，当前 `revision` 与 `expected_revision` 不一致。响应体携带段当前快照，客户端可展示差异后以新 revision 重试（需使用**新的幂等键**）：

```json
{
  "detail": {
    "error": "revision_mismatch",
    "message": "expected_revision 2 与段当前 revision 3 不一致",
    "expected_revision": 2,
    "current_segment": {
      "id": 1,
      "source": "edge-gateway-01",
      "started_at": "2026-09-20T08:00:00+00:00",
      "ended_at": "2026-09-20T08:11:40+00:00",
      "max_severity": "critical",
      "event_count": 5,
      "revision": 3
    }
  }
}
```

**幂等重放**：网络超时/客户端重试时，以**相同的幂等键与完全相同的载荷**（`operator`、`note`、`expected_revision` 三者逐字一致）重放，返回**最初那条原记录**（HTTP `200`，`replayed: true`），不会追加新记录、不会重新校验 revision——即使段在此期间已被改动（甚至该确认之后段已被撤回删除/合并），原记录仍原样返回。

**同键不同载荷（HTTP `409`）**：幂等键被复用于内容不同的请求（防止误把同一键当随机串），返回已存在的原记录，原记录保持不可变：

```json
{
  "replayed": true,
  "error": "idempotency_key_payload_mismatch",
  "acknowledgement": { "…": "已落库的原记录" }
}
```

- 段不存在（从未创建或已被撤回删除/合并消失）返回 `404`；字段校验失败（缺字段、空字符串、`expected_revision < 1` 等）返回 `422`。
- 幂等键冲突检测优先于 revision 校验：同键重放不同载荷时一律 409，与段当前 revision 无关。
- 段事后被合并或撤回删除，其历史确认记录**仍保留**（审计记录不随段删除），但因其段已不存在，不再能通过该段的列表接口读取，也不能再对其追加确认（404）。

### 查询确认记录 `GET /segments/{id}/acknowledgements`

按段内确认序号 `seq` **升序**返回某段的全部人工确认记录：

```bash
curl http://localhost:8000/segments/1/acknowledgements
```

```json
{
  "segment_id": 1,
  "acknowledgements": [
    {
      "ack_id": 1,
      "segment_id": 1,
      "seq": 1,
      "operator": "ops-oncall-01",
      "note": "已电话联系现场确认，正在重启网关联调",
      "expected_revision": 2,
      "acknowledged_revision": 2,
      "idempotency_key": "ack-20260920-0001",
      "acknowledged_at": "2026-09-21T01:10:00+00:00"
    }
  ]
}
```

- 尚无确认记录的存在段返回 `{"segment_id": 1, "acknowledgements": []}`；段不存在返回 `404`。
- 记录只追加、永不修改，列表即该段完整的人工确认审计轨迹。

### 静默间隔重分段预演 `GET /segments/resegmentation-preview`

用**候选静默间隔**对某来源的全部原始事件重新归段，预览"如果改了间隔会变成什么样"。全程**只读**：在同一只读事务（`query_only` 连接）内读取该来源全部事件并按 `occurred_at`、`event_id` 稳定排序后重算，**不会改写事件、现有段或变更流**；模拟段仅以本次响应内有效的临时编号 `temp_id` 标识。

```bash
curl 'http://localhost:8000/segments/resegmentation-preview?source=edge-gateway-01&silence_interval_seconds=120'
```

```json
{
  "source": "edge-gateway-01",
  "silence_interval_seconds": 120.0,
  "event_count": 4,
  "simulated_segments": [
    {
      "temp_id": 1,
      "source": "edge-gateway-01",
      "started_at": "2026-09-20T08:00:00+00:00",
      "ended_at": "2026-09-20T08:02:30+00:00",
      "max_severity": "critical",
      "event_count": 4
    }
  ],
  "mappings": [
    {"segment_id": 1, "relation": "merged", "simulated_temp_ids": [1], "event_count": 2},
    {"segment_id": 2, "relation": "merged", "simulated_temp_ids": [1], "event_count": 2}
  ],
  "summary": {
    "changed_existing_segment_count": 2,
    "split_existing_segment_count": 0,
    "merged_existing_segment_count": 2,
    "unchanged_existing_segment_count": 0,
    "existing_segment_count": 2,
    "simulated_segment_count": 1
  }
}
```

- **`simulated_segments`**：按候选间隔重算出的模拟段（不落库），`temp_id` 从 1 起按时间顺序编号。
- **`mappings`**：每个**现有段**到模拟段的映射，按现有段 `id` 升序，依据事件成员关系判定：
  - `unchanged`：现有段与唯一一个模拟段的事件集合完全相同（候选间隔与现状等价）；
  - `split`：该现有段的事件被拆散到**多个**模拟段（候选间隔**更小**，切分更细）；
  - `merged`：**多个**现有段的事件并入同一个模拟段（候选间隔**更大**，段被合并）。
- **`summary`**：受影响（`split` + `merged`）现有段数，以及 split/merged/unchanged 各自计数、现有段总数与模拟段总数。
- 切分规则：事件按 `(occurred_at, event_id)` 排序后，相邻事件间隔 **大于**候选间隔处切分（恰好等于间隔仍归同段，与归段规则一致）。
- 相同入参的重复请求结果**确定一致**；`silence_interval_seconds >= 0`（`0` 表示仅同一时刻的事件归同段），未知来源返回各列表为空、计数为 0 的结果。

### 增量同步变更流 `GET /segment-changes?after=&limit=`

下游系统可按 `change_id` 升序增量拉取事件段变更，实现增量同步：

```bash
curl 'http://localhost:8000/segment-changes?after=0&limit=100'
```

```json
{
  "changes": [
    {
      "change_id": 1,
      "type": "upsert",
      "segment_id": 1,
      "merged_into": null,
      "revision": 1,
      "segment": {
        "id": 1,
        "source": "edge-gateway-01",
        "started_at": "2026-09-20T08:00:00+00:00",
        "ended_at": "2026-09-20T08:00:00+00:00",
        "max_severity": "warning",
        "event_count": 1,
        "revision": 1
      }
    },
    {
      "change_id": 4,
      "type": "merged",
      "segment_id": 2,
      "merged_into": 1,
      "revision": null,
      "segment": null
    },
    {
      "change_id": 5,
      "type": "deleted",
      "segment_id": 3,
      "merged_into": null,
      "revision": null,
      "segment": null
    }
  ],
  "next_after": 5
}
```

- **何时产生记录**：每次创建段、边界或最高级别变化（`revision` 递增）、多段合并，都会在事件写入的**同一事务**内追加变更记录；`change_id` 全局单调递增。
- **`upsert`**：段的新状态，`segment` 为变更时刻的完整快照（含 `event_count`、`revision`）。
- **`merged`**：合并墓碑，表示 `segment_id` 段已被删除并并入 `merged_into` 指向的保留段；每个被删除段各产生一条。
- **`deleted`**：撤回墓碑，表示 `segment_id` 段因其**唯一事件被撤回**而删除（没有并入目标，`merged_into` 为 `null`）。
- **撤回（`DELETE /events/{source}/{event_id}`）产生的记录**：
  - 唯一事件移除 → 一条 `deleted` 墓碑；
  - 未拆段 → 一条保留段 `upsert`（沿用原 id，`revision` 递增）；
  - 桥接事件移除导致拆段 → 先一条保留段（最早子段，沿用原 id、`revision` 递增）`upsert`，再按时间序为每个新建子段各写一条 `upsert`（`revision=1`）。记录间 `change_id` 严格有序，且与事件/段的删除更新在同一事务内，要么全部可见、要么整体回滚。
- **何时不产生记录**：仅事件数增加（`revision` 不变）不写 `upsert`；重复提交 `(source, event_id)` 的幂等重放不写任何记录；撤回未知事件返回 404，不写任何记录。
- **分页约定**：用上一页返回的 `next_after` 作为下一页的 `after` 继续拉取；`change_id` 单调递增保证分页期间新写入的变更只会出现在后续页——不漏读、不重复。返回空页时 `next_after` 与入参 `after` 相同，可直接继续轮询。
- `after` 默认 `0`（从头开始）；`limit` 默认 `100`，最大 `1000`。

### 健康检查 `GET /healthz`

## 归段与 revision 规则

- 新事件的 `occurred_at` 落在某段 `[start - silence, end + silence]` 窗口内即并入该段；同时命中多个段（迟到事件桥接）时自动合并为一段，事件数累加。
- 段的 `revision` 初始为 1；**仅当**新事件导致段的起止边界或最高级别发生变化时递增，仅事件数增加不递增。
- `(source, event_id)` 唯一约束保证幂等：重复提交直接返回原段，不改计数、不改 revision。
- **误报撤回**（`DELETE /events/{source}/{event_id}`）在单事务内删除事件并对原段剩余事件重新切分：无剩余事件则删段（`deleted` 墓碑）；未拆段则沿用原 id 且 `revision` +1；拆段时最早子段沿用原 id（`revision` +1）、其余子段新建（`revision=1`）。
- 上述每次段创建、边界/最高级别变化、合并与撤回都会写入变更流（见 `GET /segment-changes`），供下游增量同步；历史段（变更流上线前已存在）不补录，订阅从部署后的新变更开始。
- **人工确认**（`POST /segments/{id}/acknowledgements`）不改段、不改 revision：它是对"某 revision 已被人工看过并处置"的只追加审计，按 `expected_revision` 做乐观锁（不符返回 409 与当前快照），按段内 `idempotency_key` 做幂等重放。段事后消失（合并/撤回删除）后历史确认仍保留在库。
- 想评估调整 `SILENCE_INTERVAL_SECONDS` 的影响时，调用重分段预演接口（见 `GET /segments/resegmentation-preview`），可在不改动任何数据的前提下查看模拟段与现有段的 split/merged 映射。

## 本地开发与测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest tests/ -v                 # 全部测试（核心逻辑 + API）
uvicorn app.main:app --reload    # 本地起服务，默认 http://127.0.0.1:8000
```

核心归段逻辑（`app/service.py`）仅依赖标准库，无第三方依赖也可验证：

```bash
python3 -m unittest tests.test_service -v
```

## 项目结构

```
app/
  main.py      # FastAPI 路由与生命周期（启动时幂等建表/升级）
  service.py   # 归段/合并/去重/撤回、事件审计查询、人工确认审计核心逻辑
  schemas.py   # 请求/响应模型与校验
  severity.py  # 告警级别定义与排序
  db.py        # SQLite 连接、schema、事务
  config.py    # 环境变量配置
tests/         # 单元测试与 API 测试
Dockerfile / docker-compose.yml
```
