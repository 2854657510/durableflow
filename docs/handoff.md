# DurableFlow 交接说明

> 这份文档记录“现在本机已经配好什么、怎么启动、还差什么”。面向的是你自己，不是面试官。

---

## 1. 当前本机状态

| 项 | 状态 |
|---|---|
| 项目目录 | `C:\Users\Firebat\Desktop\面试\durableflow` |
| Python 环境 | conda 环境 `durableflow`（Python 3.12） |
| PostgreSQL | 已用 conda 安装在 `durableflow` 环境；数据目录 `.pgdata/`，端口 5432 |
| 数据库 | `durableflow` |
| 配置文件 | `.env`（已从 `.env.example` 复制，`LLM_PROVIDER=mock`，不花钱） |
| 数据表 | 已由 `scripts/seed.py` 创建并注册 7 个内置工作流 |
| 测试 | 30 个，全部通过 |
| API | 已启动：http://127.0.0.1:8000 |
| UI | 已启动：http://127.0.0.1:8000/ui |
| Worker | 已启动（后台），会处理新运行 |
| Prometheus 指标 | http://127.0.0.1:8000/metrics |
| GitHub | 私有仓库：https://github.com/2854657510/durableflow（CI 已绿，待整理后公开） |
| Redis | 本机未启动；API 自动降级到进程内限流，任务不受影响 |

> 如果 API/Worker 的后台进程被关掉，按第 3 节重新启动即可。

---

## 2. 已验证的功能

| 命令/路径 | 结果 |
|---|---|
| `pytest -q` | 30 passed |
| `python scripts/demo.py --workflow agent_review` | 规划 → 审批 → Agent → 审查 → 报告，SUCCEEDED |
| `python scripts/demo.py --workflow dangerous_agent --goal "Send email to ops@example.com"` | 高风险工具触发审批，通过后继续，SUCCEEDED |
| `python scripts/demo.py --workflow fail_retry` | 前两次失败，第三次成功 |
| `python scripts/crash_recovery.py` | kill Worker 后 9.38s 自动恢复，SUCCEEDED |
| `python scripts/benchmark.py --runs 40 --workers 3` | 40/40 成功，135.32 步骤/秒，P50 0.755s，P95 0.796s |
| `python scripts/compare_versions.py --name demo_pipeline --a 1 --b 2 --runs 12 --workers 2` | A/B 对比输出成功率、P95、token、成本 |
| `alembic upgrade head` | 在 `durableflow_migrate` 库验证通过 |
| GitHub Actions | run 39157108506，test job 41s 绿 |

压测数字的含义：`demo_pipeline` 不含 LLM 调用，测的是调度/状态机/数据库开销；3 个 Worker，每个并发 2。面试时必须连同配置一起说。

---

## 3. 怎么重新启动

### 3.1 启动 PostgreSQL

```bash
cd C:/Users/Firebat/Desktop/面试/durableflow
conda run -n durableflow pg_ctl -D .pgdata -o "-p 5432" -l .pgdata/postgres.log start
```

如果提示 already running，就不用再启动。

### 3.2 启动 API

```bash
cd C:/Users/Firebat/Desktop/面试/durableflow
conda run --no-capture-output -n durableflow uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 3.3 启动 Worker

```bash
cd C:/Users/Firebat/Desktop/面试/durableflow
conda run --no-capture-output -n durableflow python -m app.worker.main
```

两个终端都要保持开着。打开 http://127.0.0.1:8000/ui 。

### 3.4 停止 PostgreSQL

```bash
conda run -n durableflow pg_ctl -D .pgdata stop
```

---

## 4. 还差什么

### 4.1 GitHub 仓库

- 私有仓库已创建：https://github.com/2854657510/durableflow
- 代码已通过 GitHub API 上传（86 个文件，`.env` / `.pgdata` / 日志未上传）。
- `.github/workflows/ci.yml` 已上传；GitHub Actions run **39157108506** 已跑绿（test job 41s）。
- 本机 `git push` 被网络重置，所以本地提交和远程快照的 SHA 不同；内容一致。
  后续网络恢复后，重新 `git fetch` / 对齐即可；现在以本地工作区为准。
- 目标版简历标签改为“待公开”，等仓库 Public 后再改链接。

推送/同步后检查：

- [ ] 确认 README 里的 UI 截图能显示。
- [ ] 确认 `.env`、`.pgdata`、日志没有被推送。
- [ ] 确认 GitHub Actions CI 跑绿。
- [ ] 把 `README.md` 里的“Docker Compose 未在本机验证”更新为实际状态（如果之后用 Docker 验证过）。

### 4.2 更新目标版简历

- [ ] 自己检查/修改 README、代码、演示，确认没有不想公开的内容。
- [ ] 把 GitHub 仓库从 Private 改为 Public。
- [ ] 打开 `../优化方案/简历-目标版.html`。
- [ ] 把 `github.com/2854657510/durableflow（私有仓库，待公开）` 改成可点击链接。
- [ ] 删除名字旁的“目标版 · 待公开”标签。
- [ ] 用 `04-目标版使用说明.md` 里的 Edge 命令重新生成 PDF。
- [ ] 确认仍然是一页。

### 4.3 面试准备

- [ ] 读 `docs/interview-notes.md`，至少能不看稿答 35/46 题。
- [ ] 自己跑一遍 `scripts/demo.py`、`scripts/crash_recovery.py`，能解释每一步。
- [ ] 能白板画出：状态机、`SKIP LOCKED` 抢占、租约/心跳/Reaper、Outbox、Agent 循环。
- [ ] 能说清三个限制：步骤级恢复、至少一次 + 幂等、单 PostgreSQL 主库。

---

## 5. 常见问题

### Q: 为什么 Redis 显示 degraded？

本机没有启动 Redis。项目设计上 Redis 不是正确性依赖：限流会降级到进程内，任务和租约照常工作。要用 Redis 可以：

```bash
docker run -d -p 6379:6379 redis:7-alpine
```

（当前 Docker Hub 网络受限；网络恢复后可用，或安装本地 Redis。）

### Q: 为什么 Docker Compose 没跑起来？

当前网络无法访问 Docker Hub，`postgres:16-alpine` / `redis:7-alpine` 拉不下来。Compose 配置已通过 `docker compose config` 校验；网络恢复后：

```bash
docker compose up -d --build
```

### Q: 真实模型怎么接？

编辑 `.env`：

```bash
LLM_PROVIDER=openai
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=你的key
LLM_MODEL=deepseek-chat
```

然后重启 API 和 Worker。测试仍用 mock，不花钱。

### Q: 数据想清空重来？

```bash
# 停 API/Worker 后
dropdb -h localhost -p 5432 -U durableflow durableflow
createdb -h localhost -p 5432 -U durableflow durableflow
python scripts/seed.py
```

---

## 6. 项目里最值得先读的文件

1. `README.md` — 项目定位、架构、快速开始、实测数字。
2. `app/worker/claim.py` — `SKIP LOCKED` 抢占。
3. `app/worker/lease.py` — 租约/心跳/Reaper。
4. `app/agent/loop.py` — Agent 执行链路。
5. `app/core/outbox.py` / `app/models.py` — 幂等与 Outbox。
6. `app/core/context.py` / `app/agent/memory.py` — 上下文压缩与长期记忆。
7. `docs/interview-notes.md` — 46 道追问。
