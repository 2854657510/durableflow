# SSE 实时事件流

## 1. 为什么用 SSE

运行时间线是典型的“服务端持续推送、客户端只读”场景：

| 方案 | 优点 | 缺点 |
|---|---|---|
| 轮询 | 实现最简单 | 空轮询多，延迟取决于间隔 |
| SSE | 原生 `EventSource`、自动重连、走 HTTP | 单向；每个连接占一个 HTTP 连接 |
| WebSocket | 双向、低延迟 | 需要协议升级、心跳、重连、鉴权都要自己处理 |

这里只需要服务端推送，SSE 足够；浏览器自带断线重连，比手写 WebSocket 重连稳。

## 2. 接口

```http
GET /api/runs/{run_id}/events/stream
Last-Event-ID: 456        # 可选；断线重连时浏览器自动带
?after_id=456             # 可选；curl / 非浏览器客户端用
```

事件格式：

```text
retry: 3000

id: 457
event: step_succeeded
data: {"id":457,"type":"step_succeeded","step_id":225,"payload":{"step":"calculate"},"created_at":"..."}

: ping

event: done
data: {"run_id":72,"status":"SUCCEEDED"}
```

- `id` 是 `events` 表主键，客户端下次重连带回来。
- `event` 是事件类型（`run_created`、`step_succeeded`、`approval_approved`…）。
- `data` 是完整事件 JSON，方便前端直接渲染。
- `: ping` 是 SSE 注释行，不触发 `onmessage`，只用来保活。
- `retry: 3000` 告诉浏览器断线 3 秒后重连。
- 运行进入终态后发送 `done` 并关闭连接。

## 3. 断线重连

浏览器 `EventSource` 的行为：

1. 连接断开后按 `retry` 等待。
2. 自动重连，并带上最后一次收到的 `id` 作为 `Last-Event-ID`。
3. 服务端只查 `Event.id > cursor`，把漏掉的事件补发，不重复推已消费事件。

服务端不依赖内存里的“连接状态”，所以进程重启、换实例都不影响续传。

## 4. 多实例部署

事件写在 PostgreSQL 的 `events` 表里，SSE 端点每次按 `id > cursor` 查库。

- 任意 API 实例都能服务任意运行，**不需要 sticky session**。
- 实例 A 断开后，浏览器重连到实例 B，B 从 `Last-Event-ID` 继续读库。
- 多实例之间不需要广播；代价是每个连接每秒一次索引查询。

当前实现适合中小规模（几十到几百个并发看板连接）。如果连接数上万，改成：

- PostgreSQL `LISTEN/NOTIFY`：事件提交后通知，SSE 连接阻塞等待，减少空轮询；
- 或 Redis Stream / Pub/Sub：所有 API 实例订阅同一频道，连接只持有内存队列；
- 更彻底：SSE 网关单独部署，API 只负责写事件。

## 5. Nginx / 网关配置

完整示例见 `deploy/nginx.conf`，关键项：

```nginx
proxy_buffering off;
proxy_cache off;
gzip off;
proxy_read_timeout 3600s;
add_header X-Accel-Buffering no always;
```

- `proxy_buffering off`：否则 Nginx 会攒满缓冲区才下发，实时性消失。
- `proxy_read_timeout` 必须大于心跳间隔；应用每 15 秒发一次 `: ping`。
- `X-Accel-Buffering: no`：应用层再声明一次，防止上层网关覆盖。
- 如果前面还有 ALB/CLB，同样要关闭响应缓冲，并把空闲超时调到 60 秒以上。

## 6. 背压与限制

- 单次最多补发 200 条事件（`SSE_BATCH_SIZE`），有积压时立即继续查，不额外 sleep。
- 空闲时 1 秒轮询一次（`SSE_POLL_SECONDS`）。
- 每个连接占一个 HTTP 连接和一个轮询协程；不是“无限免费”。
- 事件表会随运行增长；生产需要按时间分区或归档，否则索引会变大。

## 7. 本机验证

```bash
# 终端 1：API
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 终端 2：创建运行并实时看事件
RUN_ID=$(curl -s -X POST http://127.0.0.1:8000/api/workflows/1/runs \
  -H "Content-Type: application/json" \
  -d '{"input":{"question":"sse demo"}}' | python -c "import sys,json;print(json.load(sys.stdin)['run']['id'])")

curl -N http://127.0.0.1:8000/api/runs/$RUN_ID/events/stream

# 模拟断线重连：从第 452 条之后继续
curl -N -H "Last-Event-ID: 452" \
  http://127.0.0.1:8000/api/runs/$RUN_ID/events/stream
```

实测：一次 3 步骤运行收到 `run_created` + 3 个 `step_succeeded` + 2 个 `step_released`，最后 `done`；带 `Last-Event-ID` 重连只补发之后的事件。

## 8. 面试追问

**Q：为什么不用 WebSocket？**
A：只有服务端推送，没有双向需求；SSE 原生支持重连和 `Last-Event-ID`，实现和运维都更简单。

**Q：多实例下怎么保证不丢事件？**
A：事件先和业务状态同事务写 PostgreSQL，SSE 只是读事件表；实例无状态，重连按 id 续读。丢连接不等于丢事件。

**Q：Nginx 为什么看不到实时输出？**
A：默认 `proxy_buffering on` 会攒响应；SSE 路径必须关闭缓冲、缓存和 gzip，并让读超时大于心跳间隔。

**Q：每个连接一秒查一次库，扛得住吗？**
A：中小规模可以，查询走 `(run_id, id)` 索引；上万连接要改成 `LISTEN/NOTIFY` 或 Redis 广播，或者把 SSE 拆成独立网关。
