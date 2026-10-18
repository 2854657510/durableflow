from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置。所有字段都可以通过环境变量或 .env 覆盖。"""

    app_name: str = "DurableFlow"
    environment: str = "dev"

    # 数据库 / 缓存
    database_url: str = "postgresql+psycopg://durableflow:durableflow@localhost:5432/durableflow"
    redis_url: str = "redis://localhost:6379/0"
    redis_enabled: bool = True

    # API
    api_key: str = ""
    rate_limit_per_minute: int = 120

    # LLM
    llm_provider: str = "mock"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_input_price_per_1m: float = 2.0
    llm_output_price_per_1m: float = 8.0
    max_cost_per_run: float = 1.0

    # Worker
    worker_id: str = "worker-1"
    worker_concurrency: int = 2
    batch_size: int = 5
    lease_seconds: int = 30
    heartbeat_seconds: int = 10
    reaper_interval_seconds: int = 15
    poll_interval_seconds: float = 1.0

    # 上下文 / 记忆
    context_max_tokens: int = 4000
    context_keep_last_messages: int = 6
    memory_compact_max_items: int = 50

    # 观测
    log_level: str = "INFO"
    otel_enabled: bool = False

    # 开发便利：启动时自动建表。生产环境应改为 false，使用 Alembic。
    auto_create_tables: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
