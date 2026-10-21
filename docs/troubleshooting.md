# 线上排障手册

按“现象 → 先看什么 → 常见原因 → 处理”组织。命令默认在项目根目录执行。

## 1. 运行一直 PENDING

**先看：**

```sql
SELECT status, count(*) FROM step_runs GROUP BY status;
SELECT id, status, available_at, lease_owner, lease_expires_at
FROM step_runs WHERE status IN ('PENDING','RETRYING') ORDER BY available_at LIMIT 20;
```

```bash
curl -s localhost:8000/api/v1/workers
curl -s localhost:8000/metrics | grep durableflow_queue_depth
```

**常见原因：**

- Worker 没启动/心跳过期：`workers.last_heartbeat_at` 不再更新。
- `available_at` 在未来：重试退避还没到。
- 步骤是 `BLOCKED`：上游依赖失败，不会释放。
- 数据库连接池耗尽：API/Worker 日志里有 `QueuePool limit`。

**处理：** 先恢复 Worker；不要手工把 `RUNNING` 改成 `PENDING`，等租约过期由 Reaper 回收。

## 2. 租约反复过期 / 同一步骤重复执行

**先看：**

```sql
SELECT id, step_name, attempt, lease_owner, lease_expires_at, heartbeat_at
FROM step_runs WHERE status = 'RUNNING';
```

```bash
curl -s localhost:8000/metrics | grep durableflow_lease_expired_total
```

**常见原因：**

- 步骤执行时间 > `LEASE_SECONDS`，心跳线程没续上。
- Worker 进程被 kill/容器被驱逐。
- 机器时钟漂移。

**处理：** 调大租约或缩短步骤；确保副作用有幂等键。重复执行是至少一次语义的正常代价，不是“丢任务”。

## 3. 外部副作用重复

**先看：**

```sql
SELECT key, step_id, created_at FROM step_effects ORDER BY id DESC LIMIT 20;
SELECT event_id, status, attempts FROM outbox WHERE status != 'SENT';
```

**原因：** 消费端没按 `event_id` 去重，或业务写操作没有唯一键。

**处理：** 副作用入口统一走 `StepEffect`/业务唯一键；Outbox 消费端先查去重表再执行。

## 4. Redis 挂了

**现象：** `/healthz` 返回 `redis: degraded`，限流 backend 变成 `memory`。

**影响：** 限流退化为单进程；分布式锁不可用；任务和租约不受影响（PostgreSQL 是事实来源）。

**处理：** 先确认任务是否正常；Redis 恢复后不需要迁移数据。不要在故障时把 Redis 改成正确性依赖。

## 5. Outbox 延迟升高

```bash
curl -s localhost:8000/metrics | grep durableflow_outbox_lag_seconds
```

**原因：** relay 没跑、消费端慢、数据库连接不足、事件积压。

**处理：** 看 `outbox.status/attempts`；失败的按退避重试；持续失败进死信并告警。

## 6. SSE 没有实时输出

**排查顺序：**

1. `curl -N` 直连 API 是否能收到事件；能收到说明是网关问题。
2. Nginx 是否 `proxy_buffering off`、`gzip off`、读超时 > 15s。
3. 是否有中间层缓存/压缩。
4. 客户端是否在重连时带 `Last-Event-ID`。

**注意：** SSE 断线不等于事件丢失；事件在 `events` 表，按 id 补发。

## 7. 数据库慢查询

```sql
SELECT pid, state, wait_event_type, wait_event, query_start, query
FROM pg_stat_activity
WHERE state != 'idle' ORDER BY query_start;
```

```sql
EXPLAIN (ANALYZE, BUFFERS) <SQL>;
```

**重点看：** Seq Scan、Rows Removed by Filter、Shared Read、临时文件、`idle in transaction`。

**处理：** 先修长事务和统计信息，再考虑索引；上线索引用 `CREATE INDEX CONCURRENTLY`。参考 `docs/db-performance.md`。

## 8. LLM 429 / 超时 / 成本超预算

- 429：降低 `max_concurrency`，检查指数退避和抖动。
- 超时：单请求 `timeout_seconds` 是否大于上游 P99；不要无限重试。
- 成本：看 `llm_calls` 的 token/成本明细；预算上限触发后运行会失败，不会继续烧钱。
- 脏 JSON：JSON Schema 校验 + 字段级错误回喂；连续失败进死信。

## 9. API 401/422/429

- 401：`X-API-Key` 缺失或错误；确认 `API_KEY` 配置。
- 422：看响应 `error.details` 的字段级错误。
- 429：看 `Retry-After` 和 `X-RateLimit-Limit`；压测时临时调大 `RATE_LIMIT_PER_MINUTE`。

统一错误结构见 `docs/api-conventions.md`。
