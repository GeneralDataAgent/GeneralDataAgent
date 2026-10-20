"""Run one bounded structured-output call against a selected CC Switch or Magpie provider."""

import argparse
import json
from time import monotonic

from generaldataagent.llm import (
    CcSwitchProvider,
    LangChainNarrator,
    LlmInvocationError,
    MagpieProvider,
    OpenAiCcSwitchProvider,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-id", required=True)
    parser.add_argument("--app-type", choices=("claude", "codex", "magpie"), default="claude")
    parser.add_argument("--model", help="Explicit Magpie model ID")
    parser.add_argument("--timeout-seconds", type=float, default=20)
    arguments = parser.parse_args()

    if arguments.app_type == "magpie":
        if not arguments.model:
            parser.error("Magpie requires --model")
        provider = MagpieProvider.load(provider_id=arguments.provider_id, model=arguments.model)
    else:
        provider = (
            CcSwitchProvider.load(provider_id=arguments.provider_id)
            if arguments.app_type == "claude"
            else OpenAiCcSwitchProvider.load(provider_id=arguments.provider_id)
        )
    model = provider.build_chat_model(
        timeout_seconds=arguments.timeout_seconds,
        max_retries=0,
    )
    if arguments.app_type == "magpie":
        model.max_tokens = 3000
    narrator = LangChainNarrator(
        model,
        model_name=provider.model,
        structured_output_method="function_calling" if arguments.app_type == "magpie" else None,
    )
    started = monotonic()
    try:
        narrative = narrator.generate(
            {
                "metric": "trip_count@v1",
                "columns": ["pickup_borough", "metric_value"],
                "rows": [["Queens", 10], ["Manhattan", 20]],
                "row_count": 2,
            }
        )
        output = {
            "status": "completed",
            "provider": provider.provider_name,
            "model": provider.model,
            "elapsed_seconds": round(monotonic() - started, 2),
            "summary": narrative.summary,
        }
    except LlmInvocationError as exc:
        output = {
            "status": "failed",
            "provider": provider.provider_name,
            "model": provider.model,
            "elapsed_seconds": round(monotonic() - started, 2),
            "error": str(exc),
        }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
