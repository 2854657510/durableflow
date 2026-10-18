from __future__ import annotations

import json

import jsonschema
from jinja2 import StrictUndefined, Template, UndefinedError

from app.core.retry import NonRetryableError, RetryableError
from app.nodes.base import NodeContext, NodeResult, record_llm_call


def execute_llm_node(ctx: NodeContext) -> NodeResult:
    """LLM 节点：模板渲染 -> 模型调用 -> JSON/Schema 校验 -> 失败回喂重试。"""
    config = ctx.step.config.get("config", ctx.step.config)
    prompt_template = config.get("prompt", "")
    try:
        prompt = Template(prompt_template, undefined=StrictUndefined).render(
            input=ctx.run.input,
            previous=ctx.previous,
            run=ctx.run,
        )
    except UndefinedError as exc:
        raise NonRetryableError(f"LLM prompt 模板变量缺失: {exc}") from exc

    messages: list[dict] = []
    if config.get("system"):
        messages.append({"role": "system", "content": config["system"]})
    messages.append({"role": "user", "content": prompt})

    schema = config.get("response_schema")
    response_format = {"type": "json_object"} if schema else None
    max_self_correction = int(config.get("self_correction_attempts", 2))
    last_error = ""

    for attempt in range(1, max_self_correction + 1):
        response = ctx.llm.chat(
            messages,
            model=config.get("model"),
            temperature=float(config.get("temperature", 0.0)),
            max_tokens=config.get("max_tokens"),
            response_format=response_format,
        )
        record_llm_call(ctx, response, prompt_version=config.get("prompt_version", "v1"))

        if not schema:
            return NodeResult(
                output={
                    "text": response.content,
                    "model": response.model,
                    "prompt_tokens": response.prompt_tokens,
                    "completion_tokens": response.completion_tokens,
                    "cost": response.cost,
                }
            )

        try:
            data = json.loads(response.content)
        except json.JSONDecodeError as exc:
            last_error = f"输出不是合法 JSON: {exc}"
        else:
            try:
                jsonschema.validate(instance=data, schema=schema)
                return NodeResult(
                    output={
                        "data": data,
                        "raw": response.content,
                        "model": response.model,
                        "prompt_tokens": response.prompt_tokens,
                        "completion_tokens": response.completion_tokens,
                        "cost": response.cost,
                    }
                )
            except jsonschema.ValidationError as exc:
                last_error = f"字段校验失败: {exc.message}"

        # 把具体错误回喂给模型，让它定向修正，而不是盲目重抽。
        messages.append({"role": "assistant", "content": response.content})
        messages.append(
            {
                "role": "user",
                "content": f"上一次输出不符合要求：{last_error}。请只输出符合 JSON Schema 的 JSON。",
            }
        )

    raise RetryableError(f"LLM 输出在 {max_self_correction} 次修正后仍不符合 Schema: {last_error}")
