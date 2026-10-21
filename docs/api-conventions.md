# API 约定

## 1. 版本管理

- 新接口统一走 `/api/v1/...`。
- `/api/...` 保留给现有 UI 和旧脚本，`include_in_schema=False`，不出现在 OpenAPI 文档里。
- 破坏性变更时新增 `/api/v2`，旧版本至少保留一个迭代周期。

示例：

```http
GET /api/v1/workflows
GET /api/v1/runs?limit=20&offset=0
POST /api/v1/workflows/{workflow_id}/runs
```

OpenAPI：`/docs`、`/openapi.json` 只展示 v1。

## 2. 鉴权

- 请求头：`X-API-Key: <key>`。
- `API_KEY` 为空时开发环境不校验；生产必须配置。
- 比较使用 `secrets.compare_digest`，避免按字符提前返回的时序差异。
- 缺失或错误返回 `401`，错误码 `UNAUTHORIZED`。

> 当前是单 key 模型，没有多租户/RBAC。审批人记录在 `decided_by`，但还不是完整审计日志。

## 3. 分页

`GET /api/v1/runs`：

| 参数 | 默认 | 约束 | 说明 |
|---|---|---|---|
| `limit` | 50 | 1–500 | 每页条数 |
| `offset` | 0 | ≥0 | 跳过条数 |
| `status` | 空 | 状态枚举字符串 | 可选过滤 |

响应头：

```http
X-Total-Count: 123
```

事件列表用游标分页：`GET /api/v1/runs/{id}/events?after_id=456`，避免 offset 在持续写入时漂移。

## 4. 统一错误结构

所有错误保留旧字段 `detail`，同时增加 `error`：

```json
{
  "detail": "run not found",
  "error": {
    "code": "NOT_FOUND",
    "message": "run not found",
    "details": null,
    "request_id": "9f2c..."
  }
}
```

| HTTP | code | 场景 |
|---|---|---|
| 400 | `BAD_REQUEST` / `RUN_ERROR` / `WORKFLOW_SPEC_INVALID` | 业务参数或工作流定义错误 |
| 401 | `UNAUTHORIZED` | API Key 缺失/错误 |
| 404 | `NOT_FOUND` | 运行、步骤、审批不存在 |
| 422 | `VALIDATION_ERROR` | Pydantic 校验失败，`details` 是字段级错误 |
| 429 | `RATE_LIMITED` | 触发限流，带 `Retry-After` |
| 500 | `HTTP_ERROR` | 未归类异常 |

`request_id` 同时出现在响应头 `X-Request-ID`，排查日志时直接搜这个值。

## 5. 参数校验

- `WorkflowCreate.name`：1–128 字符；`version` ≥ 1。
- `RunCreate.max_cost`：≥ 0；不传用默认预算。
- `ApprovalDecision.decided_by`：1–128 字符；`comment` ≤ 1000 字符。
- 校验失败统一 `422 VALIDATION_ERROR`，不把内部异常栈返回给客户端。

## 6. 限流

- 默认 120 次/分钟/IP（`RATE_LIMIT_PER_MINUTE`）。
- Redis 可用时滑动窗口；Redis 不可用降级到进程内。
- 超限返回 `429`、`Retry-After: 60`、`X-RateLimit-Limit`。

## 7. 兼容性约定

- 新增字段是兼容变更；删除/改名/改类型是破坏性变更，必须进 v2。
- 错误码只增不改；客户端按 `code` 分支，不解析 `message`。
- `X-Total-Count` 是当前过滤条件下的总数，不是当前页条数。
