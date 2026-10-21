# 数据库性能：领取队列索引实测

> 环境：本机 PostgreSQL 18.6，单机，数据为合成数据。
> 脚本：`scripts/db_benchmark.py`。
> 日期：2026-10-21。

## 1. 测试方法

在独立的 `step_runs_perf` 表上造 **200,000** 条步骤，其中 2% 为 `PENDING/RETRYING`，其余为终态；`available_at` 分布在过去 24 小时内。

领取查询与生产一致：

```sql
SELECT id FROM step_runs_perf
WHERE status IN ('PENDING', 'RETRYING')
  AND available_at <= clock_timestamp()
ORDER BY available_at, id
FOR UPDATE SKIP LOCKED
LIMIT 10;
```

每种方案先跑一次预热，再取 3 次 `EXPLAIN (ANALYZE, BUFFERS)` 的中位数。

## 2. 结果

| 方案 | 扫描方式 | 索引 | 执行时间 | Shared Hit | Shared Read |
|---|---|---|---:|---:|---:|
| 无索引 | Seq Scan | - | **21.083 ms** | 2858 | 0 |
| 联合索引 | Bitmap Index Scan | `(status, available_at)` | **2.869 ms** | 20 | 0 |
| 部分索引 | Index Scan | `(available_at, id) WHERE status IN (...)` | **0.030 ms** | 12 | 0 |

相对无索引：联合索引约 **7.3×**，部分索引约 **700×**。
相对联合索引：部分索引约 **95×**。

原始样本：

```text
无索引:      21.083 / 21.085 / 20.909 ms
联合索引:     2.869 /  2.892 /  2.680 ms
部分索引:     0.028 /  0.044 /  0.030 ms
```

## 3. 为什么部分索引更快

- 联合索引把 `status` 放第一列，但 `status IN ('PENDING','RETRYING')` 是两个离散值，PostgreSQL 选择 Bitmap Index Scan，再按 `available_at` 排序。
- 部分索引只包含 2% 的待执行行，并且按 `(available_at, id)` 有序；查询可以直接 Index Scan + LIMIT，不需要排序，也不需要回表过滤终态行。
- 代价：每次步骤状态变化都要维护这个索引；终态行不进入索引，索引体积小。

## 4. 落地

- 新增索引：`idx_steps_claim_partial`，迁移 `alembic/versions/9f1c2a7b4d10_add_partial_claim_index.py`。
- 保留原联合索引 `idx_steps_claim`：其他按状态统计/排查的查询仍会用到。
- 迁移验证：在 `durableflow_migrate` 库执行 `alembic upgrade head`，`\d step_runs` 能看到部分索引。

## 5. 事务与锁

- 隔离级别：默认 **READ COMMITTED**；领取是短事务，`UPDATE ... FROM picked` 原子地把状态改为 `RUNNING`。
- `FOR UPDATE SKIP LOCKED`：多个 Worker 不会互相等待同一行；领不到就跳过，避免排队。
- 锁顺序：领取按 `available_at, id` 固定排序，减少交叉加锁。
- 死锁：当前没有自动重试 SQLSTATE `40P01` 的封装；靠短事务、固定排序和单行更新降低概率。生产建议在事务边界加一次死锁重试。
- 长事务：执行步骤在事务外，数据库连接不长期持有行锁；否则 Worker 崩溃会把锁拖到连接超时。

## 6. 慢查询排查流程

1. 看 `pg_stat_activity` 是否有 `idle in transaction` / 长事务。
2. 对可疑 SQL 跑 `EXPLAIN (ANALYZE, BUFFERS)`，重点看 Seq Scan、Rows Removed by Filter、Shared Read。
3. 检查统计信息是否过期，必要时 `ANALYZE`。
4. 再考虑加索引；索引不是越多越好，写放大和膨胀都要算。
5. 上线用 `CREATE INDEX CONCURRENTLY`，避免锁表。

## 7. 口径说明

这是**单机合成数据**的索引对比，不是生产压测。真实收益取决于待执行行占比、表膨胀、缓存命中率和写入频率；面试时要连同数据量和配置一起说。
