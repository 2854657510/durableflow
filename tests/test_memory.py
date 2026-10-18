from app.agent.memory import MemoryStore


def test_write_search_and_compact(session):
    store = MemoryStore()
    for i in range(6):
        store.write(
            session,
            "demo",
            f"k{i}",
            f"记忆内容 {i} 评审 项目",
            importance=i,
        )
    session.commit()

    results = store.search(session, "demo", "评审", limit=10)
    assert len(results) == 6
    assert results[0].importance >= results[-1].importance

    compacted = store.compact(session, "demo", max_items=2)
    session.commit()
    assert compacted["removed"] > 0
    assert compacted["summary_created"] is True

    rows = store.search(session, "demo", "", limit=20)
    assert any(r.key == "__summary__" for r in rows)
    assert len(rows) <= 3
