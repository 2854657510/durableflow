# DurableFlow

> 轻量级持久化工作流引擎：PostgreSQL 存状态，Redis 做租约/缓存，Worker 崩溃可恢复，重试/幂等/人工审批可配置，LLM 只是其中一种节点。


## 1. 它解决什么问题

真实业务里的 AI 任务往往是长链路：
上传 300 页标书 → 解析 → OCR → 切分 → Embedding → 条款审查
→ 人工确认 → 生成报告 → 发邮件
这条链路可能跑几十分钟，中间任何一步都可能失败。用“一个 HTTP 请求从头跑到尾”的写法，失败就得重来，模型费用也会重复烧。

DurableFlow 把每个步骤变成**可持久化、可重试、可恢复、可审计**的执行单元：

- 步骤执行前后状态落 PostgreSQL；进程重启不丢。
- Worker 通过租约领取任务，崩溃后租约过期，任务自动重新入队。
- 每个步骤可配置重试、超时、指数退避、死信。
- 副作用通过 `Idempotency-Key`、`StepEffect` 和 Outbox 去重。
- 高风险工具必须人工审批，审批通过后从断点继续。
- 每次 LLM 调用记录 token、成本、模型和 prompt 版本。

---

## 3. 架构

```text
                    ┌─────────────────────────────┐
                    │        Web UI / CLI          │
                    │  工作流 · 运行时间线 · 审批    │
                    └──────────────┬──────────────┘
                                   │ HTTP
                    ┌──────────────▼──────────────┐
                    │          FastAPI             │
                    │  启动 / 查询 / 取消 / 审批     │
                    └───────┬──────────────┬──────┘
                            │              │
              ┌─────────────▼───┐   ┌──────▼─────────────┐
              │   PostgreSQL     │   │       Redis         │
              │ 状态/事件/Outbox │   │ 租约提示/缓存/限流   │
              │ 幂等键/副作用     │   │ （非正确性依赖）      │
              └─────────────┬───┘   └──────┬─────────────┘
                            │              │
                    ┌───────▼──────────────▼──────┐
                    │          Worker 池           │
                    │ 抢占 → 执行 → 心跳 → 落库    │
                    └───────┬──────────────┬──────┘
                            │              │
                 ┌──────────▼───┐   ┌──────▼──────────┐
                 │ 函数/HTTP 节点 │   │   LLM / Agent   │
                 │               │   │ 工具/记忆/审批   │
                 └──────────────┘   └─────────────────┘
```

### 关键设计

1. **PostgreSQL 是唯一事实来源**：状态、租约、重试次数都以库为准。
2. **Redis 不是正确性依赖**：Redis 挂了，Worker 仍能靠数据库租约工作；限流退化到进程内。
3. **先提交领取，再执行**：领取是短事务，执行在事务外，避免长事务持锁。
4. **至少一次 + 幂等**：外部副作用不承诺字面 exactly-once；用唯一键和 Outbox 保证“重跑不产生第二次副作用”。
5. **LLM 只是一个节点**：模型会超时、限流、返回脏 JSON；它和普通函数一样需要重试、超时、成本控制和降级。

---

## 4. 功能

- 工作流定义：`steps` + `depends_on`，DAG 依赖；启动时校验环和缺失依赖。
- 状态机：`PENDING / RUNNING / WAITING_APPROVAL / SUCCEEDED / FAILED / CANCELED`；步骤级 `BLOCKED / RETRYING / DEAD`。
- 多 Worker 抢占：PostgreSQL `FOR UPDATE SKIP LOCKED`；SQLite 单测用进程内锁。
- 租约/心跳/Reaper：租约过期自动回收；最大尝试次数后进 `DEAD`。
- 重试：指数退避 + 抖动；区分可重试/不可重试异常；支持死信和手动重试。
- 幂等：运行级 `Idempotency-Key`；步骤级 `StepEffect` 唯一键。
- Outbox：业务变更和事件同事务写入；relay 异步投递；消费端按 event_id 去重。
- 人工审批：审批节点 / 高风险工具触发；通过后从断点继续；拒绝则失败并审计。
- Agent：ReAct 循环、规划器、工具注册、JSON Schema 权限、Observation 回喂、最大步数。
- 记忆：长期记忆表、关键词召回、importance/recency 排序、compact + 摘要。
- 上下文压缩：token 估算、保留最近 N 条、旧消息摘要、预算不够继续裁。
- LLM 节点：Jinja2 模板、JSON Schema 校验、失败回喂修正、token/成本记录、预算上限。
- 观测：结构化 JSON 日志（request_id/run_id/step_id）、Prometheus 指标、可选 OpenTelemetry。
- API：工作流、运行、步骤、事件、取消、手动重试、审批、成本、Worker、健康检查。
- UI：工作流启动、运行列表、步骤时间线、事件、审批按钮。
- 测试：30 个测试覆盖状态机、重试、上下文、工具权限、记忆、抢占、租约、幂等、Outbox、Agent、API。
- 压测/演示：`benchmark.py`、`crash_recovery.py`、`demo.py`、`compare_versions.py`。

---

## 5. 快速开始

> 本机已经配好一版：见 `docs/handoff.md`（包含当前运行状态、重启命令、推送 GitHub 的步骤）。

### 方式 A：Docker Compose（推荐，网络能拉镜像时）

```bash
cp .env.example .env
docker compose up -d --build
```

- UI: http://localhost:8000/ui
- API 文档: http://localhost:8000/docs
- Prometheus: http://localhost:9090

> 当前开发机访问 Docker Hub 受限，镜像未能拉取；Compose 配置已提供，网络恢复后可直接用。下面的本地 PostgreSQL 路径已实际跑通。

### 方式 B：本地 PostgreSQL + conda（本机已验证）

```bash
# 1. 环境
conda create -n durableflow python=3.12 -y
conda activate durableflow
pip install -r requirements-dev.txt

# 2. 本地 PostgreSQL
conda install -n durableflow -c conda-forge postgresql=18 -y
initdb -D .pgdata -U durableflow --auth=trust --encoding=UTF8
pg_ctl -D .pgdata -o "-p 5432" -l .pgdata/postgres.log start
createdb -h localhost -p 5432 -U durableflow durableflow

# 3. 配置
cp .env.example .env
# .env 默认：postgresql+psycopg://durableflow:durableflow@localhost:5432/durableflow
# LLM_PROVIDER=mock，不花钱

# 4. 建表 + 注册内置工作流
python scripts/seed.py
python scripts/check_env.py

# 5. 启动 API（终端 1）
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 6. 启动 Worker（终端 2）
python -m app.worker.main
```

打开 http://127.0.0.1:8000/ui 。

### 方式 C：迁移（生产）

```bash
# 全新数据库
export DATABASE_URL=postgresql+psycopg://durableflow:durableflow@localhost:5432/durableflow
alembic upgrade head
```

`AUTO_CREATE_TABLES=true` 只用于开发；生产设为 `false`，用 Alembic。

---

## 7. 演示脚本

```bash
# 完整 Agent 审批链路：规划 → 审批 → Agent → 审查 → 报告
python scripts/demo.py --workflow agent_review

# 高风险工具：Agent 想发邮件，停在审批；通过后继续
python scripts/demo.py --workflow dangerous_agent \
  --goal "整理结论并发送邮件给 ops@example.com"

# 重试/退避：前两次失败，第三次成功
python scripts/demo.py --workflow fail_retry

# 真杀 Worker，验证租约过期后自动恢复
python scripts/crash_recovery.py

# 引擎开销压测
python scripts/benchmark.py --runs 40 --workers 3

## 8. 测试

```bash
conda run --no-capture-output -n durableflow pytest -q
# 30 passed
```

覆盖：

- 状态机合法/非法迁移。
- 退避计算、抖动、可重试分类。
- 上下文压缩：保留 system 和最近消息，超预算继续裁。
- 工具权限：dangerous 未审批必须拦截；`calculate` 不用 `eval`。
- 记忆写入/召回/compact。
- 抢占互斥、依赖释放、租约回收、最大尝试进 DEAD。
- 运行级幂等、步骤级副作用去重。
- Outbox 成功/失败重试。
- Agent 工具调用、最终回答、危险工具暂停。
- API 健康检查、工作流、创建运行、幂等、取消、UI。
- Worker 端到端：demo_pipeline、agent_review + 审批。

---

## 9. 目录结构

```text
durableflow/
├── app/
│   ├── main.py                 # FastAPI 入口、中间件、/ui、/metrics
│   ├── config.py               # pydantic-settings
│   ├── db.py                   # engine / session / init_db
│   ├── models.py               # 11 张表：运行/步骤/事件/Outbox/审批/记忆/LLM 调用...
│   ├── state_machine.py        # 运行/步骤状态迁移
│   ├── api/                    # workflows / runs / approvals / workers
│   ├── core/                   # retry / context / outbox / rate_limit / events
│   ├── worker/                 # claim / lease / executor / main
│   ├── nodes/                  # function / llm / approval / agent
│   ├── agent/                  # loop / planner / tools / memory
│   ├── llm/                    # client / pricing
│   ├── observability/          # logging / metrics / tracing
│   └── templates/index.html    # 无框架 Web UI
├── alembic/                    # 初始迁移
├── scripts/                    # seed / demo / crash_recovery / benchmark / compare_versions
├── tests/                      # 27 个测试
├── docker-compose.yml
├── Dockerfile
├── prometheus.yml
└── README.md
```

---

## 10. 已知限制

- **Agent 内部状态是步骤级恢复**：步骤崩溃后会从该步骤开头重跑；消息级 checkpoint 还没做。生产可以进一步把 messages 持久化到 `step_runs.output` 或单独表。
- **单 PostgreSQL 主库**：不做多主/跨区域多活。
- **至少一次语义**：外部副作用依赖业务幂等；不承诺字面 exactly-once。
- **Redis 只做加速/协调**：Redis 挂了限流退化为进程内，缓存命中率下降，但任务不丢。
- **动态 DAG 未实现**：规划器输出的是数据，工作流步骤在启动时确定；运行中动态加步骤是下一步。
- **UI 是只读时间线**：不做拖拽编排。
- **成本是估算**：按配置的 1M token 单价计算，不是供应商账单级精确。
- **Docker Compose 未在本机验证**：Docker Hub 网络受限；本地 conda PostgreSQL 路径已验证。
- **没有 RBAC**：只有可选 `API_KEY`；企业多租户权限需要再加。



## 13. 下一步

- 消息级 Agent checkpoint（真正从“第 3 轮工具调用”恢复）。
- 运行中动态加步骤（planner 生成 DAG 并持久化）。
- 多租户 + RBAC + 审计日志。
- Grafana dashboard。
- 用真实模型跑一组评测，替换 mock 数字。
