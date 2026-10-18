from __future__ import annotations

from datetime import timedelta
from typing import Callable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.models import Memory

Summarizer = Callable[[list[str]], str]


def _default_summary(contents: list[str], max_chars: int = 1200) -> str:
    text = " | ".join(c.strip().replace("\n", " ") for c in contents if c.strip())
    return text[:max_chars] + ("…" if len(text) > max_chars else "")


class MemoryStore:
    """短期记忆放 run 的 messages；长期记忆放这张表。

    这里用一个简单的关键词检索 + importance/recency 排序，够个人项目用。
    生产环境可以换成向量检索或 PostgreSQL 全文索引。
    """

    def write(
        self,
        session: Session,
        namespace: str,
        key: str,
        content: str,
        *,
        tags: list[str] | None = None,
        importance: int = 5,
        ttl_seconds: int | None = None,
    ) -> Memory:
        row = session.scalar(
            select(Memory).where(Memory.namespace == namespace, Memory.key == key)
        )
        expires_at = utcnow() + timedelta(seconds=ttl_seconds) if ttl_seconds else None
        if row is None:
            row = Memory(
                namespace=namespace,
                key=key,
                content=content,
                tags=tags or [],
                importance=importance,
                expires_at=expires_at,
            )
            session.add(row)
        else:
            row.content = content
            row.tags = tags or row.tags
            row.importance = importance
            row.expires_at = expires_at
            row.updated_at = utcnow()
        session.flush()
        return row

    def search(self, session: Session, namespace: str, query: str = "", limit: int = 10) -> list[Memory]:
        now = utcnow()
        stmt = select(Memory).where(
            Memory.namespace == namespace,
            or_(Memory.expires_at.is_(None), Memory.expires_at > now),
        )
        if query:
            like = f"%{query}%"
            stmt = stmt.where(or_(Memory.content.ilike(like), Memory.key.ilike(like)))
        stmt = stmt.order_by(Memory.importance.desc(), Memory.updated_at.desc()).limit(limit)
        return list(session.scalars(stmt))

    def context_for(self, session: Session, namespace: str, query: str = "", limit: int = 5) -> str:
        rows = self.search(session, namespace, query, limit)
        if not rows:
            return ""
        return "\n".join(f"- [{row.key}] {row.content}" for row in rows)

    def compact(
        self,
        session: Session,
        namespace: str,
        *,
        max_items: int = 50,
        summarizer: Summarizer | None = None,
    ) -> dict:
        now = utcnow()
        rows = list(
            session.scalars(
                select(Memory)
                .where(
                    Memory.namespace == namespace,
                    or_(Memory.expires_at.is_(None), Memory.expires_at > now),
                )
                .order_by(Memory.importance.desc(), Memory.updated_at.desc())
            )
        )
        if len(rows) <= max_items:
            return {"kept": len(rows), "removed": 0, "summary_created": False}

        keep = rows[:max_items]
        removed = [r for r in rows[max_items:] if r.key != "__summary__"]
        if not removed:
            return {"kept": len(rows), "removed": 0, "summary_created": False}

        contents = [r.content for r in removed]
        summary = summarizer(contents) if summarizer else _default_summary(contents)
        for row in removed:
            session.delete(row)
        self.write(
            session,
            namespace,
            "__summary__",
            f"历史记忆摘要（{len(removed)} 条）：{summary}",
            tags=["summary"],
            importance=3,
        )
        return {"kept": len(keep), "removed": len(removed), "summary_created": True}
