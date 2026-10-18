# DurableFlow

> 轻量级持久化工作流引擎：PostgreSQL 存状态，Redis 做租约/缓存，Worker 崩溃可恢复，重试/幂等/人工审批可配置，LLM 只是其中一种节点。

![DurableFlow UI](docs/ui.png)

这个项目不是“又一个 RAG 问答 Demo”，而是用来回答面试里的那类问题：

- 任务跑一半服务挂了，怎么恢复？
- 工具超时了，重试还是重新规划？
- 重复投递怎么保证不重复发邮件/扣款？
- 高风险工具怎么加权限和审批？
- Agent 的上下文窗口满了怎么压？压完信息丢了怎么办？
- 一次任务花了多少 token、多少钱？怎么证明版本更新后更好了？

---

## 1. 它解决什么问题

真实业务里的 AI 任务往往是长链路：

```text
上传 300 页标书 → 解析 → OCR → 切分 → Embedding → 条款审查
→ 人工确认 → 生成报告 → 发邮件
```

这条链路可能跑几十分钟，中间任何一步都可能失败。用“一个 HTTP 请求从头跑到尾”的写法，失败就得重来，模型费用也会重复烧。

DurableFlow 把每个步骤变成**可持久化、可重试、可恢复、可审计**的执行单元：

- 步骤执行前后状态落 PostgreSQL；进程重启不丢。
- Worker 通过租约领取任务，崩溃后租约过期，任务自动重新入队。
- 每个步骤可配置重试、超时、指数退避、死信。
- 副作用通过 `Idempotency-Key`、`StepEffect` 和 Outbox 去重。
- 高风险工具必须人工审批，审批通过后从断点继续。
- 每次 LLM 调用记录 token、成本、模型和 prompt 版本。

---

## 2. 面试问题 → 代码位置

| 面试官会问 | 代码/实现 | 关键点 |
|---|---|---|
| Agent 执行链路怎么走？ | `app/agent/loop.py` | 模型决策 → 工具调用 → Observation 回喂 → 再决策 |
| 任务怎么拆？ | `app/agent/planner.py` | 规划器输出子步骤，解析失败有规则兜底 |
| 工具怎么注册、Schema 怎么定义？ | `app/agent/tools.py` | `ToolSpec` + JSON Schema 校验 |
| 哪些工具只读、哪些能写、高风险怎么办？ | `app/agent/tools.py` | `read / write / dangerous` 权限；dangerous 必须审批 |
| 工具超时/失败怎么办？ | `app/agent/loop.py` | 错误作为 Observation 回喂；连续失败上限 |
| 服务挂了怎么恢复？ | `app/worker/lease.py` | 租约 + 心跳 + Reaper；`scripts/crash_recovery.py` 真杀进程验证 |
| 多 Worker 怎么不抢同一任务？ | `app/worker/claim.py` | PostgreSQL `FOR UPDATE SKIP LOCKED` |
| 重复投递怎么不重复副作用？ | `app/agent/loop.py` / `app/models.py` | `Idempotency-Key` + `StepEffect` 唯一键 + Outbox |
| 上下文窗口怎么压？ | `app/core/context.py` | token 估算、阈值、保留最近消息、摘要、超预算继续裁 |
| 长期记忆怎么存、怎么召回、膨胀怎么办？ | `app/agent/memory.py` | 短期 messages + 长期 `memories` 表；关键词召回；compact + 摘要 |
| 不同业务怎么选模型、怎么算钱？ | `app/llm/client.py` / `app/llm/pricing.py` | 模型可配；按 1M token 计价；每次调用记录成本 |
| 成本超预算怎么办？ | `app/nodes/base.py` / `app/agent/loop.py` | 运行级 `max_cost`，超预算直接失败/暂停 |
| 版本更新怎么证明变好？ | `scripts/compare_versions.py` | 两版各跑 N 次，对比成功率/P95/token/成本 |
| 线上怎么观测？ | `app/observability/` | JSON 日志、Prometheus 指标、可选 OTel Trace |
| API 怎么防刷？ | `app/core/rate_limit.py` | Redis 滑动窗口；Redis 挂了退化到进程内 |
| 上传/删除怎么保证一致？ | `app/services/runs.py` / 标书项目里 | 删除顺序、幂等、Outbox |

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

## 6. API 示例

```bash
# 列出工作流
curl http://127.0.0.1:8000/api/workflows

# 启动 demo_pipeline（id=1）
curl -X POST http://127.0.0.1:8000/api/workflows/1/runs \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-001" \
  -d '{"input": {}, "max_cost": 1.0}'

# 查询运行 / 步骤 / 事件 / 成本
curl http://127.0.0.1:8000/api/runs/1
curl http://127.0.0.1:8000/api/runs/1/steps
curl http://127.0.0.1:8000/api/runs/1/events
curl http://127.0.0.1:8000/api/runs/1/cost

# 取消 / 手动重试
curl -X POST http://127.0.0.1:8000/api/runs/1/cancel
curl -X POST http://127.0.0.1:8000/api/runs/steps/3/retry

# 待审批 / 通过 / 拒绝
curl "http://127.0.0.1:8000/api/approvals?status=PENDING"
curl -X POST http://127.0.0.1:8000/api/approvals/2/approve \
  -H "Content-Type: application/json" -d '{"decided_by":"operator","comment":"同意"}'
curl -X POST http://127.0.0.1:8000/api/approvals/2/reject \
  -H "Content-Type: application/json" -d '{"decided_by":"operator","comment":"驳回"}'
```

如果设置了 `API_KEY`，所有 `/api/*` 请求加 `X-API-Key`。

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

# A/B 对比两个版本（需要先注册 v2）
python scripts/compare_versions.py --name demo_pipeline --a 1 --b 2 --runs 30
```

### 本机实测（2026-10-19）

- 环境：Windows 10、Python 3.12、PostgreSQL 18.6、3 个 Worker、每个并发 2。
- `benchmark.py --runs 40 --workers 3`：40/40 成功，**135.32 步骤/秒**，run P50 **0.755s**、P95 **0.796s**。
- 说明：`demo_pipeline` 不含 LLM 调用，这个数字测的是调度/状态机/数据库开销，不是模型延迟。
- `crash_recovery.py`：杀掉执行中的 Worker 后，第二个 Worker 在租约过期后接手，run 成功；kill 到完成 **9.38s**（其中 slow 步骤本身 6s，租约 3s）。

> 压测数字必须连同配置一起说。换机器、换并发、加 LLM 调用，结果都会变。

---

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

## 10. 已知限制（面试时主动说）

- **Agent 内部状态是步骤级恢复**：步骤崩溃后会从该步骤开头重跑；消息级 checkpoint 还没做。生产可以进一步把 messages 持久化到 `step_runs.output` 或单独表。
- **单 PostgreSQL 主库**：不做多主/跨区域多活。
- **至少一次语义**：外部副作用依赖业务幂等；不承诺字面 exactly-once。
- **Redis 只做加速/协调**：Redis 挂了限流退化为进程内，缓存命中率下降，但任务不丢。
- **动态 DAG 未实现**：规划器输出的是数据，工作流步骤在启动时确定；运行中动态加步骤是下一步。
- **UI 是只读时间线**：不做拖拽编排。
- **成本是估算**：按配置的 1M token 单价计算，不是供应商账单级精确。
- **Docker Compose 未在本机验证**：Docker Hub 网络受限；本地 conda PostgreSQL 路径已验证。
- **没有 RBAC**：只有可选 `API_KEY`；企业多租户权限需要再加。

---

## 11. 为什么不用 Celery / Temporal

| 方案 | 能做什么 | 为什么这里不用 |
|---|---|---|
| Celery / RQ | 任务队列、重试、定时 | 偏“一次性任务”，缺少步骤级持久化状态机、人工审批、断点继续、运行时间线 |
| Temporal | 生产级持久化工作流 | 功能很强，但部署和学习成本高；个人项目用它会掩盖底层机制 |
| 本项目 | 状态机 + 租约 + 幂等 + Outbox + Agent 节点 | 代码量可控，能把 `SKIP LOCKED`、租约、幂等、Outbox 讲清楚 |

面试时这么说比“我会用 Celery”更有说服力：**你知道 Celery 解决什么，也知道它不解决什么。**

---

## 12. 面试怎么讲（3 分钟版）

> 这个项目不是又一个聊天机器人，而是一个支撑长任务和 Agent 的持久化执行引擎。
>
> 核心问题是：一条链路要跑几十分钟，中间模型会超时、工具会失败、Worker 会挂。用普通请求从头跑到尾，失败就得重来，钱也白烧。
>
> 我的做法是把每个步骤落 PostgreSQL，Worker 用 `FOR UPDATE SKIP LOCKED` 抢任务，抢到后拿一个租约并定期心跳；Worker 挂了租约过期，Reaper 把任务重新入队。为了避免重复副作用，运行级有 `Idempotency-Key`，步骤级有 `StepEffect` 唯一键，跨系统事件走 Outbox。
>
> Agent 部分是一个 ReAct 循环：模型输出 JSON 决策，工具注册表做 JSON Schema 校验和 read/write/dangerous 权限，危险工具必须走人工审批；工具失败不抛穿，而是作为 Observation 回喂给模型。上下文超预算会压缩旧消息，长期记忆单独存表并定期 compact。
>
> 我还做了 Prometheus 指标、成本明细和 A/B 对比脚本。实测 3 个 Worker、每个并发 2，demo 链路 135 步骤/秒；杀 Worker 后 9 秒左右恢复。数字都连同配置一起说，因为它们只代表这台机器、这个场景。

---

## 13. 下一步

- 消息级 Agent checkpoint（真正从“第 3 轮工具调用”恢复）。
- 运行中动态加步骤（planner 生成 DAG 并持久化）。
- 多租户 + RBAC + 审计日志。
- Grafana dashboard。
- 用真实模型跑一组评测，替换 mock 数字。
