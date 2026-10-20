# DurableFlow 架构与关键链路

## 1. 组件图

```mermaid
flowchart LR
    subgraph Client["客户端"]
        UI["Web UI / curl"]
        ES["EventSource<br/>Last-Event-ID"]
    end

    subgraph API["FastAPI 实例（可水平扩展）"]
        R1["REST API<br/>创建 / 查询 / 审批"]
        S1["SSE 端点<br/>按 id 轮询事件表"]
    end

    PG[("PostgreSQL<br/>运行 / 步骤 / 事件<br/>Outbox / 幂等键")]
    Redis[("Redis<br/>限流 / 分布式锁<br/>缓存（非正确性依赖）")]

    subgraph Worker["Worker 池（可水平扩展）"]
        W1["领取：FOR UPDATE<br/>SKIP LOCKED"]
        W2["执行：函数 / LLM / Agent"]
        W3["心跳 + 租约续期"]
        R["Reaper：回收过期租约"]
    end

    LLM["LLM / 工具"]

    UI --> R1
    ES --> S1
    R1 --> PG
    R1 --> Redis
    S1 --> PG
    W1 --> PG
    W2 --> PG
    W2 --> LLM
    W3 --> PG
    R --> PG
    Redis -. 限流降级 .-> R1
```

关键点：

- PostgreSQL 是唯一事实来源；Redis 挂了不影响任务正确性。
- API 和 Worker 都可以多实例；领取靠数据库行锁，不靠 Redis 锁。
- SSE 端点只读事件表，不参与调度。

## 2. 一次运行的生命周期

```mermaid
sequenceDiagram
    participant C as Client
    participant A as API
    participant P as PostgreSQL
    participant W as Worker

    C->>A: POST /api/workflows/{id}/runs
    A->>P: INSERT run + steps + event + outbox（同事务）
    A-->>C: 201 run_id
    C->>A: GET /api/runs/{id}/events/stream
    A->>P: SELECT events WHERE id > cursor
    A-->>C: id/event/data

    W->>P: UPDATE ... FOR UPDATE SKIP LOCKED
    W->>P: 提交领取（短事务）
    W->>W: 执行步骤（事务外）
    W->>P: 写步骤结果 + 事件 + 释放依赖
    A->>P: 下一轮轮询
    A-->>C: step_succeeded / step_released
    A-->>C: event: done
```

## 3. 崩溃恢复

```mermaid
sequenceDiagram
    participant W1 as Worker A
    participant P as PostgreSQL
    participant W2 as Worker B
    participant R as Reaper

    W1->>P: 领取步骤，租约 30s，心跳 10s
    Note over W1: 进程被 kill
    R->>P: 扫描 lease_expires_at < now()
    R->>P: 步骤重置为 PENDING
    W2->>P: SKIP LOCKED 领取同一步骤
    W2->>W2: 重新执行（幂等键去重）
    W2->>P: 写结果 + 事件
```

## 4. SSE 断线续传

```mermaid
sequenceDiagram
    participant B as Browser
    participant A1 as API 实例 1
    participant A2 as API 实例 2
    participant P as PostgreSQL

    B->>A1: GET /events/stream
    A1->>P: SELECT id > 0
    P-->>A1: 451, 452
    A1-->>B: id:452
    Note over B,A1: 网络断开
    B->>A2: 重连，Last-Event-ID: 452
    A2->>P: SELECT id > 452
    P-->>A2: 453, 454
    A2-->>B: id:453, id:454
```

换实例不影响续传：游标是数据库主键，不是内存状态。

## 5. 异步批量 LLM

```mermaid
flowchart LR
    P["prompts[]"] --> Q["asyncio.Queue / gather"]
    Q --> S{"Semaphore<br/>max_concurrency"}
    S --> C1["AsyncClient.call"]
    S --> C2["AsyncClient.call"]
    S --> C3["AsyncClient.call"]
    C1 --> R["汇总 BatchResult"]
    C2 --> R
    C3 --> R
    C1 -. 可重试错误 .-> B["指数退避 + 抖动"]
    B --> S
```

- 信号量限制在途请求数，避免打爆上游 429。
- 退避期间释放信号量，不占并发槽。
- 单请求独立超时、独立重试；失败只影响自己。

## 6. 相关文档

- [SSE 细节与 Nginx 配置](sse.md)
- [技术选型取舍](tradeoffs.md)
- 项目证据表与评测计划：本地 `优化方案/05-项目证据与取舍.md`（不随仓库发布）
