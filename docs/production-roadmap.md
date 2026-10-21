# 生产化路线与迁移说明

> 这份文档写“要上生产还缺什么、怎么迁”，不是“已经做过”。
> 面试时用它回答 Celery/Kafka、K8s、pgvector/Milvus 的追问，不要说成项目实践。

## 1. 当前状态

- 部署：本地 conda PostgreSQL + uvicorn/Worker；Docker Compose、Nginx 配置已提供，未做生产集群验证。
- 异步：Outbox + Worker 轮询；没有独立消息队列。
- 监控：Prometheus 指标、Grafana Dashboard/告警配置、OTel Trace（Console/OTLP）。
- 向量库：标书平台用 Chroma；没有 pgvector/Milvus/FAISS 的实测对比。
- 评测：只有 9 条招标编号对照，没有 A/B、Badcase 回归和人工标注集。

## 2. 消息队列迁移

| 方案 | 适合 | 迁移方式 | 代价 |
|---|---|---|---|
| Outbox + Worker（当前） | 已有 PostgreSQL、任务量中等 | 无需迁移 | 轮询有延迟；Worker 自己扩缩容 |
| Redis Stream | 已有 Redis、想要消费组/ACK/重试 | 新增 producer 写 `XADD`，consumer group `XREADGROUP`，`XACK`，`XAUTOCLAIM` 处理宕机，超过重试进死信 Stream | 消息不持久到磁盘级别；要自己写监控 |
| Celery + RabbitMQ | 成熟任务队列、定时任务、社区方案 | 任务拆成 `@app.task`，broker/result backend 配置；`acks_late=True` + 幂等键 | 引入 broker；步骤级状态/审批仍要自己做 |
| Kafka | 高吞吐、可回放、多消费方 | 事件按 topic 分区，consumer group + offset；Outbox relay 改为 Kafka producer | 运维重；个人项目过度设计 |

**迁移顺序建议：** 先 Redis Stream（最小改动，补消费组/ACK/死信），任务量或跨团队需求上来再评估 Kafka。不要为了关键词直接上 Kafka。

## 3. Kubernetes 部署

当前只有 Dockerfile/Compose。上 K8s 至少需要：

- `Deployment`：api 和 worker 分开；worker 副本按队列深度扩缩容。
- `Service` + `Ingress`：SSE 路径关闭缓冲、读超时 > 心跳；多实例不需要 sticky session。
- `ConfigMap` / `Secret`：数据库、Redis、API Key、LLM Key 分离；不要把 `.env` 打进镜像。
- `readinessProbe`：`/healthz`；`livenessProbe` 单独做，避免 DB 慢时被杀。
- `Job`：`alembic upgrade head`，与 API 启动解耦。
- `PodDisruptionBudget`：滚动更新时至少保留一个 Worker。
- HPA：默认 CPU 指标不够，建议 KEDA 按 `durableflow_queue_depth` 扩 worker。
- 日志/追踪：stdout JSON → Loki/ELK；OTLP → Collector → Tempo/Jaeger。

**没有做的原因：** 个人项目没有集群；写 manifests 也无法真实验证，容易变成“纸面 K8s”。面试时讲清 above 的探针、扩缩容和迁移 Job 即可。

## 4. 向量库迁移

标书平台当前用 Chroma。迁移前先固定评测集，否则换库无法判断好坏。

### pgvector

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE chunks (
  id bigserial PRIMARY KEY,
  doc_id bigint NOT NULL,
  content text NOT NULL,
  embedding vector(1024) NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);
```

- 优点：和业务表同库，事务/备份/权限统一；SQL 过滤 metadata 方便。
- 代价：向量索引和 OLTP 抢资源；数据量大时要做分区/只读副本。

### Milvus

- 适合千万级以上向量、独立扩缩容、多索引类型。
- 代价：独立集群、etcd/MinIO 依赖；个人项目运维成本高。

### FAISS

- 适合离线/单机评测和召回实验；`IndexFlatIP`/`IVF`/`HNSW` 可对比。
- 不适合直接做在线多租户服务；要自己包一层持久化/更新/删除。

**迁移步骤：**

1. 统一 `VectorStore` 接口：`upsert/delete/search`。
2. 用同一批 30–50 条 query 跑 Recall@5/10、MRR、P95。
3. 双写/影子读：新库先影子查询，对比结果一致率。
4. 灰度切流，保留回滚开关。
5. 删除旧库前确认引用和备份。

## 5. AI 质量评测

需要补的闭环：

1. **评测集**：30–50 条 query，标注正确 chunk / 无答案；覆盖精确编号、条款定位、跨段汇总、干扰项。
2. **离线指标**：Recall@5/10、MRR、Rerank 前后差值、引用支持率。
3. **A/B**：同一 query 分流到新旧 prompt/模型/检索配置，比较指标和成本。
4. **Badcase 回归**：失败样本分类为切分/召回/排序/生成，修完跑同一套集。
5. **人工标注**：两人独立标注 + 第三人仲裁；记录标注规范和一致性。
6. **上线门禁**：核心指标下降超过阈值则阻断发布。

在这些跑出来之前，简历不写具体提升百分比。

## 6. 安全与审计补强

- 单 API Key → RBAC（角色/资源/动作）+ key 轮换。
- 审计事件外送（不可变存储/日志平台），增加签名链。
- 数据脱敏从正则升级为字段级策略/DLP。
- 提示词注入从规则升级为“规则 + 分类器 + 输出过滤 + 工具权限”。
- 增加越权、注入、敏感信息泄露的自动化安全测试。
