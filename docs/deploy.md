# 部署与运维

## 1. 本地已验证路径（conda）

```bash
cd durableflow
conda create -n durableflow python=3.12 -y
conda activate durableflow
pip install -r requirements-dev.txt

conda install -n durableflow -c conda-forge postgresql=18 -y
initdb -D .pgdata -U durableflow --auth=trust --encoding=UTF8
pg_ctl -D .pgdata -o "-p 5432" -l .pgdata/postgres.log start
createdb -h localhost -p 5432 -U durableflow durableflow

cp .env.example .env
python scripts/seed.py
uvicorn app.main:app --host 127.0.0.1 --port 8000
python -m app.worker.main
```

健康检查：

```bash
curl http://127.0.0.1:8000/healthz
# {"status":"ok","database":"ok","redis":"degraded","rate_limit_backend":"memory"}
```

Redis 未启动时显示 `degraded`，任务和租约仍可用。

## 2. Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
docker compose logs -f api worker
```

服务：

| 服务 | 端口 | 说明 |
|---|---|---|
| `api` | 8000 | FastAPI + UI + `/metrics` |
| `worker` | - | 可水平扩容 |
| `postgres` | 5432 | 状态/事件/Outbox |
| `redis` | 6379 | 限流/锁/缓存 |
| `prometheus` | 9090 | 抓取 `/metrics` |

> 本机 Docker daemon 当前未启动，Compose 配置已通过 `docker compose config` 校验；conda 本地路径已实际跑通。网络恢复后可补一次真实 `up`。

## 3. Nginx / SSE

完整配置：`deploy/nginx.conf`。关键项：

```nginx
proxy_buffering off;
proxy_cache off;
gzip off;
proxy_read_timeout 3600s;
add_header X-Accel-Buffering no always;
```

- SSE 路径必须单独匹配，否则默认缓冲会让前端看不到实时事件。
- 心跳 15 秒一次，读超时要大于心跳间隔。
- 多实例不需要 sticky session；事件游标在 PostgreSQL。

## 4. 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | 本地 PostgreSQL | SQLAlchemy DSN |
| `REDIS_URL` | `redis://localhost:6379/0` | 限流/锁/缓存 |
| `REDIS_ENABLED` | `true` | 关掉后限流降级进程内 |
| `API_KEY` | 空 | 为空不校验；生产必须设置 |
| `RATE_LIMIT_PER_MINUTE` | 120 | 每 IP 每分钟 |
| `LLM_PROVIDER` | `mock` | `mock` 或 OpenAI 兼容 |
| `LLM_API_KEY` | 空 | 真实模型密钥，不要提交 |
| `AUTO_CREATE_TABLES` | `true` | 生产设 `false`，用 Alembic |
| `LEASE_SECONDS` | 30 | 租约时长 |
| `HEARTBEAT_SECONDS` | 10 | 心跳间隔 |
| `SSE_POLL_SECONDS` | 1.0 | 事件轮询间隔 |
| `SSE_HEARTBEAT_SECONDS` | 15 | SSE 心跳 |

## 5. 数据库迁移

```bash
export DATABASE_URL=postgresql+psycopg://user:pass@host:5432/durableflow
alembic upgrade head
alembic current
```

当前 head：`9f1c2a7b4d10`（新增部分索引 `idx_steps_claim_partial`）。
生产建索引建议用 `CREATE INDEX CONCURRENTLY`，避免锁表。

## 6. 监控

- `/metrics`：Prometheus 文本格式。
- 关键指标：
  - `durableflow_runs_total{status}`
  - `durableflow_steps_total{status}`
  - `durableflow_queue_depth`
  - `durableflow_step_duration_seconds`
  - `durableflow_lease_expired_total`
  - `durableflow_retry_total{reason}`
  - `durableflow_outbox_lag_seconds`
- 日志是结构化 JSON，带 `request_id` / `run_id` / `step_id`。

## 7. 发布与回滚

1. 先跑 `pytest -q`，再 `alembic upgrade head`。
2. 滚动重启 API；Worker 优雅停机（等待当前步骤完成或租约释放）。
3. 回滚代码用上一镜像；数据库迁移只做向前兼容，破坏性变更拆成两步。
4. 观察 `queue_depth`、`lease_expired_total`、P95 和错误率 10 分钟。

## 8. 尚未验证

- Docker Compose 实跑（Docker daemon/网络）。
- Nginx 代理链路压测。
- Kubernetes、TLS、域名、公网部署。
- 多租户/RBAC 和完整审计日志。
