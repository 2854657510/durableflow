from datetime import datetime, timezone


def utcnow() -> datetime:
    """统一使用带时区的 UTC 时间，避免本地时区和夏令时问题。"""
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """把数据库里可能没有时区的时间补成 UTC。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
