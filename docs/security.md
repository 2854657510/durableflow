# 安全与审计

## 1. 鉴权

- 请求头 `X-API-Key`；`API_KEY` 为空时仅开发环境放行。
- 使用 `secrets.compare_digest` 比较，避免时序侧信道。
- 401 统一返回 `UNAUTHORIZED`，不区分“没带 key”和“key 错误”。

**限制：** 单 key 模型，没有多租户、角色、权限范围和 key 轮换。生产需要 RBAC + key 管理。

## 2. 数据脱敏

日志统一走 `app/observability/redaction.py`：

- 邮箱 → `[REDACTED_EMAIL]`
- 手机号 → `[REDACTED_PHONE]`
- `sk-...` → `[REDACTED_API_KEY]`
- `Bearer ...` → `Bearer [REDACTED_TOKEN]`
- 字典 key 为 `api_key/password/token/secret/authorization/cookie` 时整个值替换为 `[REDACTED]`

覆盖范围：`JsonFormatter` 的 `message`、`exc_info` 和 `extra_fields`。

**限制：** 只做正则脱敏，不能识别身份证、银行卡、地址等自定义敏感字段；生产应接入字段级策略或 DLP。

## 3. 提示词注入防护

`app/core/prompt_guard.py` 对渲染后的 prompt 做规则扫描：

- 英文 `ignore previous instructions`
- 中文“忽略以上/之前的指令/规则”
- `system prompt` / `developer prompt`
- “开发者模式/越狱/jailbreak”
- “你现在是管理员/系统/root”

命中后：

1. 在消息前插入系统级安全提示，声明这些内容是数据、不是指令；
2. 在节点输出里记录 `prompt_guard_hits`，便于审计和后续评测。

**限制：** 规则只能挡明显攻击，挡不住编码、分词、多轮诱导；没有模型分类器和红队评测。生产需要“规则 + 分类器 + 输出过滤 + 工具权限”多层防御。

## 4. 审批审计

审批通过/拒绝时写 `events` 表，payload 包含：

```json
{
  "approval_id": 1,
  "decided_by": "operator",
  "comment": "确认发送",
  "request_id": "9f2c...",
  "actor_ip": "127.0.0.1"
}
```

`approvals` 表保留 `tool_name`、`arguments`、`decided_at`、`decided_by`，可以还原“谁在什么时候批准了什么高风险操作”。

**限制：** 事件表不是 WORM 存储，管理员可以直接改库；没有签名链、日志外送和保留策略。

## 5. 工具权限

- `read` / `write` / `dangerous` 三级。
- `dangerous` 工具未审批直接抛 `ApprovalRequired`，步骤进入 `WAITING_APPROVAL`。
- 审批只对当前步骤的指定工具生效，不是全局放行。

## 6. AI 质量评测（未完成）

目前只有标书平台的 9 条招标编号对照（纯向量 0/9，BM25/RRF 第 1），**没有**生产级 A/B、Badcase 回归和人工标注集。

要补齐至少需要：

1. 30–50 条标注 query，覆盖精确编号、条款定位、跨段汇总、无答案。
2. Recall@5/10、MRR、Rerank 前后差值。
3. Badcase 分类：切分 / 召回 / 排序 / 生成。
4. 人工标注规范（两人标注 + 仲裁）。
5. A/B 流量切分和回归门禁。

在这些跑出来之前，简历不写“Recall 提升 X%”“A/B 实验”这类数字。

## 7. 密钥与仓库

- `.env`、`.pgdata`、日志、临时文件都在 `.gitignore`。
- 公开仓库前做过扫描：没有 `sk-`、私钥、`.env`。
- 真实模型 key 只放本地环境变量，不进代码和镜像。
