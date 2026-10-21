"""步骤领取队列的索引对比。

在独立的 step_runs_perf 表上造 20 万条步骤，分别测：
1. 无索引（Seq Scan）
2. (status, available_at) 联合索引
3. (available_at, id) WHERE status IN ('PENDING','RETRYING') 部分索引

表结构复制 step_runs 的关键列，但不碰真实数据；跑完自动删表。
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings

ROWS = 200_000
BATCH = 10

CLAIM_SQL = """
SELECT id FROM step_runs_perf
WHERE status IN ('PENDING', 'RETRYING')
  AND available_at <= clock_timestamp()
ORDER BY available_at, id
FOR UPDATE SKIP LOCKED
LIMIT {batch}
""".format(batch=BATCH)


def dsn() -> str:
    return get_settings().database_url.replace("postgresql+psycopg://", "postgresql://")


def setup(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS step_runs_perf")
        cur.execute(
            """
            CREATE TABLE step_runs_perf (
                id bigserial PRIMARY KEY,
                run_id bigint NOT NULL,
                step_name varchar(128) NOT NULL,
                node_type varchar(32) NOT NULL,
                config jsonb NOT NULL DEFAULT '{}'::jsonb,
                depends_on jsonb NOT NULL DEFAULT '[]'::jsonb,
                position integer NOT NULL,
                status varchar(32) NOT NULL,
                attempt integer NOT NULL DEFAULT 0,
                max_attempts integer NOT NULL DEFAULT 3,
                input jsonb NOT NULL DEFAULT '{}'::jsonb,
                available_at timestamptz NOT NULL
            )
            """
        )
        cur.execute(
            """
            INSERT INTO step_runs_perf
                (run_id, step_name, node_type, position, status, available_at)
            SELECT
                1,
                'bench-' || g,
                'function',
                g,
                CASE
                    WHEN g %% 100 = 0 THEN 'PENDING'
                    WHEN g %% 100 = 1 THEN 'RETRYING'
                    ELSE 'SUCCEEDED'
                END,
                clock_timestamp() - ((g %% 86400) * interval '1 second')
            FROM generate_series(1, %s) AS g
            """,
            (ROWS,),
        )
        cur.execute("ANALYZE step_runs_perf")


def explain(conn: psycopg.Connection) -> dict:
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {CLAIM_SQL}")
        plan = cur.fetchone()[0][0]
    node = plan["Plan"]
    # 部分索引可能在外层包一层 Limit；找最内层的扫描节点。
    while "Plans" in node and node["Plans"]:
        node = node["Plans"][0]
    return {
        "node": node.get("Node Type"),
        "index": node.get("Index Name"),
        "execution_ms": round(plan["Execution Time"], 3),
        "planning_ms": round(plan["Planning Time"], 3),
        "rows": plan["Plan"].get("Actual Rows"),
        "hit_blocks": node.get("Shared Hit Blocks", 0),
        "read_blocks": node.get("Shared Read Blocks", 0),
    }


def drop_index(conn: psycopg.Connection, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"DROP INDEX IF EXISTS {name}")
        cur.execute("ANALYZE step_runs_perf")


def run_case(conn: psycopg.Connection, label: str, ddl: str | None) -> dict:
    if ddl:
        with conn.cursor() as cur:
            cur.execute(ddl)
            cur.execute("ANALYZE step_runs_perf")

    # 第一次当预热，后三次取中位数，减少 shared buffers 冷热差异。
    explain(conn)
    measured = [explain(conn) for _ in range(3)]
    samples = [item["execution_ms"] for item in measured]
    result = measured[-1]
    result["execution_ms"] = round(statistics.median(samples), 3)
    result["samples_ms"] = samples
    result["label"] = label
    return result


def main() -> None:
    with psycopg.connect(dsn(), autocommit=True) as conn:
        try:
            setup(conn)
            results = [run_case(conn, "无索引", None)]
            results.append(
                run_case(
                    conn,
                    "联合索引 (status, available_at)",
                    "CREATE INDEX idx_perf_status_available ON step_runs_perf (status, available_at)",
                )
            )
            drop_index(conn, "idx_perf_status_available")
            results.append(
                run_case(
                    conn,
                    "部分索引 (available_at, id) WHERE status IN (...)",
                    "CREATE INDEX idx_perf_partial ON step_runs_perf (available_at, id) "
                    "WHERE status IN ('PENDING', 'RETRYING')",
                )
            )

            print(json.dumps(results, ensure_ascii=False, indent=2))
            print()
            print(f"{'方案':<46}{'扫描':<12}{'索引':<28}{'执行ms':<10}{'命中块':<8}{'读盘块'}")
            for row in results:
                print(
                    f"{row['label']:<46}{str(row['node']):<12}"
                    f"{str(row['index'] or '-'):<28}{row['execution_ms']:<10}"
                    f"{row['hit_blocks']:<8}{row['read_blocks']}"
                )
        finally:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS step_runs_perf")


if __name__ == "__main__":
    main()
