from __future__ import annotations

import ast
import operator
from dataclasses import dataclass, field
from typing import Any, Callable

import jsonschema

from app.core.retry import (
    ApprovalRequired,
    NonRetryableError,
    RetryableError,
    ToolArgumentError,
    ToolExecutionError,
)

ToolHandler = Callable[[dict, dict], dict]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    permission: str  # read / write / dangerous
    handler: ToolHandler


@dataclass
class ToolRegistry:
    tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        if spec.permission not in {"read", "write", "dangerous"}:
            raise ValueError(f"未知权限: {spec.permission}")
        self.tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self.tools.get(name)

    def specs_for_prompt(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
                "permission": t.permission,
            }
            for t in self.tools.values()
        ]

    def execute(
        self,
        name: str,
        arguments: dict | None,
        *,
        approved: bool = False,
        context: dict | None = None,
    ) -> dict:
        spec = self.get(name)
        if spec is None:
            raise ToolArgumentError(f"工具不存在: {name}")
        arguments = arguments or {}
        try:
            jsonschema.validate(instance=arguments, schema=spec.parameters)
        except jsonschema.ValidationError as exc:
            raise ToolArgumentError(f"工具参数不合法: {exc.message}") from exc

        # 高风险操作没有审批就直接拒绝，不能靠 prompt 自觉。
        if spec.permission == "dangerous" and not approved:
            raise ApprovalRequired(name, arguments, reason="dangerous tool")

        try:
            result = spec.handler(arguments, context or {})
        except (ApprovalRequired, NonRetryableError, RetryableError):
            raise
        except Exception as exc:  # noqa: BLE001 - 工具失败要回喂给模型
            raise ToolExecutionError(f"工具 {name} 执行失败: {exc}") from exc
        if not isinstance(result, dict):
            return {"result": result}
        return result


# ---------- 内置工具 ----------


def _record_effect(context: dict, key: str, payload: dict) -> dict:
    recorder = context.get("record_effect")
    if recorder is None:
        return payload
    return recorder(key, payload)


def search_docs(args: dict, context: dict) -> dict:
    query = args["query"]
    return {
        "query": query,
        "results": [
            {"id": "doc-1", "title": "项目立项说明", "snippet": f"与“{query}”相关的第一条资料"},
            {"id": "doc-2", "title": "评审流程", "snippet": "评审需要在两周内完成并提交结论"},
            {"id": "doc-3", "title": "风险清单", "snippet": "主要风险是交付时间与数据完整性"},
        ],
    }


def get_order(args: dict, context: dict) -> dict:
    return {"order_id": args["order_id"], "status": "PAID", "amount": 1280.0}


def update_order(args: dict, context: dict) -> dict:
    key = f"order:{args['order_id']}:{args['status']}"
    payload = {"order_id": args["order_id"], "status": args["status"], "updated": True}
    return _record_effect(context, key, payload)


def send_email(args: dict, context: dict) -> dict:
    key = f"email:{args['to']}:{args['subject']}"
    payload = {"to": args["to"], "subject": args["subject"], "sent": True}
    return _record_effect(context, key, payload)


_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


def _eval_expr(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_expr(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_expr(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        left = _eval_expr(node.left)
        right = _eval_expr(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ToolArgumentError("指数过大")
        if isinstance(node.op, (ast.Div, ast.Mod)) and right == 0:
            raise ToolArgumentError("除数不能为 0")
        return _ALLOWED_BINOPS[type(node.op)](left, right)
    raise ToolArgumentError("表达式只支持数字和 + - * / % **")


def calculate(args: dict, context: dict) -> dict:
    try:
        tree = ast.parse(args["expression"], mode="eval")
    except SyntaxError as exc:
        raise ToolArgumentError(f"表达式语法错误: {exc.msg}") from exc
    return {"result": _eval_expr(tree)}


def default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="search_docs",
            description="检索企业知识库文档，只读。",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "检索关键词"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            permission="read",
            handler=search_docs,
        )
    )
    registry.register(
        ToolSpec(
            name="get_order",
            description="按订单号查询订单状态，只读。",
            parameters={
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
                "additionalProperties": False,
            },
            permission="read",
            handler=get_order,
        )
    )
    registry.register(
        ToolSpec(
            name="update_order",
            description="修改订单状态，写操作，具备幂等性。",
            parameters={
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["PAID", "SHIPPED", "CANCELED"]},
                },
                "required": ["order_id", "status"],
                "additionalProperties": False,
            },
            permission="write",
            handler=update_order,
        )
    )
    registry.register(
        ToolSpec(
            name="send_email",
            description="发送邮件，高风险写操作，必须人工审批。",
            parameters={
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["to", "subject", "body"],
                "additionalProperties": False,
            },
            permission="dangerous",
            handler=send_email,
        )
    )
    registry.register(
        ToolSpec(
            name="calculate",
            description="计算简单算术表达式，只读。",
            parameters={
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
                "additionalProperties": False,
            },
            permission="read",
            handler=calculate,
        )
    )
    return registry
