from app.core.context import ContextManager, estimate_tokens


def test_estimate_tokens_non_zero():
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello") >= 1
    assert estimate_tokens("你好，世界") >= 1


def test_compress_keeps_system_and_recent_messages():
    messages = [{"role": "system", "content": "system rules"}]
    for i in range(30):
        messages.append({"role": "user", "content": f"message {i} " + "x" * 200})
        messages.append({"role": "assistant", "content": f"answer {i} " + "y" * 200})

    manager = ContextManager(max_tokens=300, keep_last_messages=4)
    result = manager.build(messages)
    assert result.compressed
    assert result.final_tokens <= 300
    assert result.messages[0]["content"] == "system rules"
    assert result.messages[-1]["content"].startswith("answer 29")
    assert any("历史上下文摘要" in m["content"] for m in result.messages)


def test_short_context_not_compressed():
    messages = [{"role": "user", "content": "hi"}]
    result = ContextManager(max_tokens=100).build(messages)
    assert not result.compressed
    assert result.messages == messages
