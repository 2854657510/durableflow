"""开工前自检：DB / Redis / LLM / 表结构。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, text

from app.config import get_settings
from app.db import SessionLocal, engine
from app.llm.client import get_llm_client


def main() -> int:
    settings = get_settings()
    print("=" * 60)
    print("DurableFlow 环境自检")
    print("=" * 60)
    print(f"环境: {settings.environment}")
    print(f"数据库方言: {engine.dialect.name}")
    print(f"Redis: {'启用' if settings.redis_enabled else '禁用'}")
    print(f"LLM: {settings.llm_provider} / {settings.llm_model}")

    ok = True
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
            tables = inspect(engine).get_table_names()
        print(f"[OK] PostgreSQL/SQLite 可连接，表数量: {len(tables)}")
        if not tables:
            print("[WARN] 还没有表，运行 python scripts/seed.py 或启动 API 自动建表")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[FAIL] 数据库连接失败: {exc}")

    if settings.redis_enabled:
        try:
            import redis

            redis.Redis.from_url(settings.redis_url, socket_timeout=2).ping()
            print("[OK] Redis 可连接")
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Redis 不可用，将降级到进程内限流: {exc}")

    client = get_llm_client(settings)
    print(f"[OK] LLM 客户端: {type(client).__name__}")
    if type(client).__name__ == "MockLLMClient":
        print("      当前是 mock 模式，不会调用真实模型、不产生费用")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
