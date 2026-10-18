import pytest

from app.agent.tools import default_registry
from app.core.retry import ApprovalRequired, ToolArgumentError


def test_dangerous_tool_requires_approval():
    registry = default_registry()
    with pytest.raises(ApprovalRequired):
        registry.execute(
            "send_email",
            {"to": "a@example.com", "subject": "s", "body": "b"},
            approved=False,
        )


def test_dangerous_tool_with_approval():
    registry = default_registry()
    result = registry.execute(
        "send_email",
        {"to": "a@example.com", "subject": "s", "body": "b"},
        approved=True,
    )
    assert result["sent"] is True


def test_calculate_is_safe():
    registry = default_registry()
    assert registry.execute("calculate", {"expression": "1 + 2 * 3"})["result"] == 7
    with pytest.raises(ToolArgumentError):
        registry.execute("calculate", {"expression": "__import__('os').system('dir')"})


def test_invalid_arguments_are_rejected():
    registry = default_registry()
    with pytest.raises(ToolArgumentError):
        registry.execute("search_docs", {})
    with pytest.raises(ToolArgumentError):
        registry.execute("unknown_tool", {})
