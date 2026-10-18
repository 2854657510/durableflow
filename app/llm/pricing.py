from dataclasses import dataclass


@dataclass
class ModelPricing:
    model: str
    input_per_1m: float
    output_per_1m: float


# 这里只是默认值，真实价格以供应商账单为准。接新模型时改这里或环境变量。
DEFAULT_PRICING: dict[str, ModelPricing] = {
    "deepseek-chat": ModelPricing("deepseek-chat", 2.0, 8.0),
    "gpt-4o-mini": ModelPricing("gpt-4o-mini", 0.15, 0.60),
    "mock": ModelPricing("mock", 1.0, 2.0),
}


def estimate_cost(
    prompt_tokens: int,
    completion_tokens: int,
    input_per_1m: float,
    output_per_1m: float,
) -> float:
    return (prompt_tokens / 1_000_000) * input_per_1m + (completion_tokens / 1_000_000) * output_per_1m


def pricing_for(model: str, input_per_1m: float | None = None, output_per_1m: float | None = None) -> ModelPricing:
    base = DEFAULT_PRICING.get(model, ModelPricing(model, 0.0, 0.0))
    return ModelPricing(
        model=model,
        input_per_1m=base.input_per_1m if input_per_1m is None else input_per_1m,
        output_per_1m=base.output_per_1m if output_per_1m is None else output_per_1m,
    )
