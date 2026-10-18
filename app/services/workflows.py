from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import WorkflowDefinition

ALLOWED_NODE_TYPES = {"function", "llm", "approval", "agent", "memory", "noop"}


class WorkflowSpecError(ValueError):
    pass


def validate_workflow_spec(spec: dict) -> dict:
    if not isinstance(spec, dict) or not isinstance(spec.get("steps"), list) or not spec["steps"]:
        raise WorkflowSpecError("workflow spec 必须包含非空 steps 列表")

    names: set[str] = set()
    steps = spec["steps"]
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise WorkflowSpecError(f"第 {i + 1} 个 step 不是对象")
        name = step.get("name")
        if not name or not isinstance(name, str):
            raise WorkflowSpecError(f"第 {i + 1} 个 step 缺少 name")
        if name in names:
            raise WorkflowSpecError(f"step 名称重复: {name}")
        names.add(name)
        node_type = step.get("type", "function")
        if node_type not in ALLOWED_NODE_TYPES:
            raise WorkflowSpecError(f"step {name} 的 type 不支持: {node_type}")
        depends_on = step.get("depends_on", [])
        if not isinstance(depends_on, list) or any(not isinstance(d, str) for d in depends_on):
            raise WorkflowSpecError(f"step {name} 的 depends_on 必须是字符串列表")
        max_attempts = step.get("max_attempts", 3)
        if not isinstance(max_attempts, int) or max_attempts < 1:
            raise WorkflowSpecError(f"step {name} 的 max_attempts 必须是 >=1 的整数")

    for step in steps:
        for dep in step.get("depends_on", []):
            if dep not in names:
                raise WorkflowSpecError(f"step {step['name']} 依赖不存在的 step: {dep}")
            if dep == step["name"]:
                raise WorkflowSpecError(f"step {step['name']} 不能依赖自己")

    # 简单拓扑排序，顺便检测环
    indegree = {s["name"]: len(s.get("depends_on", [])) for s in steps}
    graph: dict[str, list[str]] = {s["name"]: [] for s in steps}
    for step in steps:
        for dep in step.get("depends_on", []):
            graph[dep].append(step["name"])
    queue = [name for name, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        current = queue.pop()
        visited += 1
        for nxt in graph[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if visited != len(steps):
        raise WorkflowSpecError("workflow 存在循环依赖")

    return spec


def register_workflow(
    session: Session,
    name: str,
    spec: dict,
    *,
    version: int = 1,
) -> WorkflowDefinition:
    validate_workflow_spec(spec)
    existing = session.scalar(
        select(WorkflowDefinition).where(
            WorkflowDefinition.name == name,
            WorkflowDefinition.version == version,
        )
    )
    if existing is not None:
        return existing
    definition = WorkflowDefinition(name=name, version=version, spec=spec)
    session.add(definition)
    session.commit()
    session.refresh(definition)
    return definition


def get_workflow(session: Session, workflow_id: int) -> WorkflowDefinition | None:
    return session.get(WorkflowDefinition, workflow_id)


def get_workflow_by_name(session: Session, name: str, version: int | None = None) -> WorkflowDefinition | None:
    stmt = select(WorkflowDefinition).where(WorkflowDefinition.name == name)
    if version is not None:
        stmt = stmt.where(WorkflowDefinition.version == version)
    else:
        stmt = stmt.order_by(WorkflowDefinition.version.desc())
    return session.scalar(stmt)


BUILTIN_WORKFLOWS: dict[str, dict] = {
    "demo_pipeline": {
        "description": "最小可运行链路：准备 -> 计算 -> 报告",
        "steps": [
            {
                "name": "prepare",
                "type": "function",
                "function": "echo",
                "params": {"message": "hello durableflow"},
                "depends_on": [],
            },
            {
                "name": "calculate",
                "type": "function",
                "function": "add",
                "params": {"a": 1, "b": 2},
                "depends_on": ["prepare"],
            },
            {
                "name": "report",
                "type": "function",
                "function": "echo",
                "params": {"message": "done"},
                "depends_on": ["calculate"],
            },
        ],
    },
    "agent_review": {
        "description": "规划 -> 审批 -> Agent 执行 -> 审查 -> 报告",
        "steps": [
            {
                "name": "plan",
                "type": "function",
                "function": "plan_task",
                "params": {"goal": "整理项目评审材料并给出结论"},
                "depends_on": [],
            },
            {
                "name": "approve_plan",
                "type": "approval",
                "config": {"prompt": "确认执行计划"},
                "depends_on": ["plan"],
            },
            {
                "name": "execute",
                "type": "function",
                "function": "run_agent",
                "params": {"goal": "整理项目评审材料并给出结论", "namespace": "review"},
                "depends_on": ["approve_plan"],
            },
            {
                "name": "review",
                "type": "llm",
                "config": {
                    "prompt": "请审查以下内容并输出 JSON：\n{{ previous.execute.output.answer }}",
                    "response_schema": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "risk": {"type": "string"},
                        },
                        "required": ["summary", "risk"],
                    },
                    "prompt_version": "v1",
                },
                "depends_on": ["execute"],
            },
            {
                "name": "report",
                "type": "function",
                "function": "send_report",
                "params": {"namespace": "review"},
                "depends_on": ["review"],
            },
        ],
    },
    "dangerous_agent": {
        "description": "Agent 调用高风险工具，触发人工审批后继续",
        "steps": [
            {
                "name": "execute",
                "type": "function",
                "function": "run_agent",
                "params": {"goal": "整理评审结论并发送邮件给 ops@example.com", "namespace": "danger"},
                "depends_on": [],
            },
            {
                "name": "report",
                "type": "function",
                "function": "send_report",
                "params": {"namespace": "danger"},
                "depends_on": ["execute"],
            },
        ],
    },
    "fail_retry": {
        "description": "前两次失败、第三次成功，用于验证重试/退避",
        "steps": [
            {
                "name": "flaky",
                "type": "function",
                "function": "flaky",
                "params": {},
                "depends_on": [],
                "max_attempts": 3,
                "retry": {"initial_delay_s": 0.2, "multiplier": 2, "max_delay_s": 2, "jitter": 0.1},
            },
            {
                "name": "done",
                "type": "function",
                "function": "echo",
                "params": {"message": "retry ok"},
                "depends_on": ["flaky"],
            },
        ],
    },
    "crash_recovery": {
        "description": "长步骤执行中杀掉 Worker，验证租约过期后自动恢复",
        "steps": [
            {
                "name": "slow",
                "type": "function",
                "function": "sleep",
                "params": {"seconds": 6},
                "depends_on": [],
                "max_attempts": 2,
            },
            {
                "name": "done",
                "type": "function",
                "function": "echo",
                "params": {"message": "recovered"},
                "depends_on": ["slow"],
            },
        ],
    },
    "memory_context": {
        "description": "写入记忆 -> 检索 -> 压缩，验证长期记忆和上下文治理",
        "steps": [
            {
                "name": "write",
                "type": "function",
                "function": "write_memory",
                "params": {
                    "namespace": "memory_demo",
                    "key": "project",
                    "content": "项目需要在两周内完成评审并提交结论",
                    "importance": 8,
                },
                "depends_on": [],
            },
            {
                "name": "search",
                "type": "function",
                "function": "search_memory",
                "params": {"namespace": "memory_demo", "query": "评审"},
                "depends_on": ["write"],
            },
            {
                "name": "compact",
                "type": "function",
                "function": "compact_memory",
                "params": {"namespace": "memory_demo", "max_items": 10},
                "depends_on": ["search"],
            },
        ],
    },
}


def seed_builtin_workflows(session: Session) -> list[WorkflowDefinition]:
    definitions = []
    for name, spec in BUILTIN_WORKFLOWS.items():
        definitions.append(register_workflow(session, name, spec))
    return definitions
