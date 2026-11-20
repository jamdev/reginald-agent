"""Translate cumulative Codex thread usage and tool lifecycles into Reginald telemetry."""

from ...core.observability import logger
from ..base import RunnerResult


def update_usage(result: RunnerResult, total: dict, model: str, api_billing: bool) -> None:
    counts = {"input_tokens": total["input_tokens"], "output_tokens": total["output_tokens"],
              "cache_read.input_tokens": total["cached_input_tokens"]}
    if total.get("cache_write_input_tokens") is not None:
        counts["cache_creation.input_tokens"] = total["cache_write_input_tokens"]
    result.token_usage = counts
    if api_billing:
        try:
            from genai_prices import Usage, calc_price

            usage = Usage(input_tokens=counts["input_tokens"], output_tokens=counts["output_tokens"],
                          cache_read_tokens=total["cached_input_tokens"],
                          cache_write_tokens=total.get("cache_write_input_tokens"))
            result.cost_usd = float(calc_price(usage, model, provider_id="openai").total_price)
        except Exception as exc:  # noqa: BLE001 — optional offline pricing
            logger.debug("Could not estimate Codex cost for %s: %s", model, exc)
