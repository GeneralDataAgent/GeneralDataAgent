#!/usr/bin/env bash
# Run the paired rule-Supervisor validation once the model gateway is available.
#
# Both runs use the same commit, dataset, protocol and 10-question denominator;
# the only difference is the pinned document treatment. Outputs are never
# overwritten: an existing report stops the run instead of merging results.
set -euo pipefail

cd "$(dirname "$0")/.."
root=".artifacts/stage10-document-20261026"
config="$root/treatment/document-extension.json"
model="workbuddy/deepseek-v4.1-flash"
common=(
  --app-type magpie --provider-id magpie --model "$model"
  --limit 10 --no-empty-list-is-wildcard
  --max-runtime-seconds 120 --request-interval-seconds 3.5
)

export LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false
export UV_CACHE_DIR="$PWD/.cache/uv"
export TMPDIR="$PWD/$root/tmp"
mkdir -p "$TMPDIR"

run() {
  local name="$1"
  shift
  if [ -e "$root/$name/report.json" ]; then
    echo "refusing to overwrite existing run: $root/$name/report.json" >&2
    exit 1
  fi
  echo "== $name" >&2
  uv run python evals/run_stage10_rule_supervisor.py \
    --output "$root/$name/report.json" "${common[@]}" "$@" \
    > "$root/$name.log" 2>&1
}

# Probe the gateway before spending the denominator. A blocked gateway must not
# open the circuit mid-run and leave part of the denominator unattempted.
uv run python - <<'PY'
import sys
from generaldataagent.evaluation.stage_six_models import (
    build_evaluation_model,
    resolve_evaluation_model,
)

provider, route = resolve_evaluation_model(
    app_type="magpie",
    provider_id="magpie",
    model_override="workbuddy/deepseek-v4.1-flash",
    transport="openai-chat",
    thinking_mode="default",
    json_mode=False,
)
try:
    build_evaluation_model(provider, route).invoke("Reply with the single word: ok")
except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
    print(f"gateway unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(3)
PY

run baseline3
run treatment3 --document-extension "$config"

uv run python - "$root" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
for name in ("baseline3", "treatment3"):
    report = json.loads((root / name / "report.json").read_text())
    summary = report["summary"]
    print(
        json.dumps(
            {
                "run": name,
                "run_status": report["run_status"],
                "revision_independent": {
                    key: report[key]
                    for key in ("supervisor_hash", "role_controller_hash", "document_context_hash")
                },
                "document_extension": report["document_extension"],
                "n": summary["n"],
                "processed_n": summary["processed_n"],
                "attempted_n": summary["attempted_n"],
                "refused_by_parser_n": summary["refused_by_parser_n"],
                "supervisor_error_n": summary["supervisor_error_n"],
                "passed": summary["passed_over_all_questions"],
                "model_calls": summary["model_calls"],
                "usage": summary["usage"],
                "usage_telemetry_complete": summary["usage_telemetry_complete"],
            },
            ensure_ascii=False,
        )
    )
PY
