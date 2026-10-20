"""One answer-blind evaluation worker. Only repository-owned code is executed."""

import json
import sys
from pathlib import Path

from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task
from generaldataagent.llm import CcSwitchProvider, OpenAiCcSwitchProvider


def main():
    payload = json.loads(sys.stdin.read())
    provider = (
        CcSwitchProvider if payload["app_type"] == "claude" else OpenAiCcSwitchProvider
    ).load(provider_id=payload["provider_id"])
    model = provider.build_chat_model(timeout_seconds=30, max_retries=0).bind(max_tokens=3000)
    result = BoundedSQLAgent(model).run(
        Task(**payload["task"]), Path(payload["database"]), documents=payload["documents"]
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
