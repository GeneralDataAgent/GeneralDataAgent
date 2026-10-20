"""Build a source-linked iteration packet from complete measured reports, without model calls."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from generaldataagent.evaluation.stage_six import digest
from generaldataagent.evaluation.stage_six_comparison import compare_runs
from generaldataagent.evaluation.stage_six_gates import release_diagnostics, validate_report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    base = root / ".artifacts/stage6"
    specification = json.loads(args.manifest.read_text())
    pairs, reports, baselines, replays = [], [], [], []

    def read_run(name):
        path = (base / name / "report.json").resolve(strict=True)
        if not path.is_relative_to(base.resolve()):
            raise ValueError("report path escapes evidence root")
        report = json.loads(path.read_text())
        validate_report(report)
        replay = path.parent / "replay.json"
        subprocess.run(
            [
                sys.executable,
                str(root / "evals/replay_stage6.py"),
                "--report",
                str(path),
                "--output",
                str(replay),
            ],
            check=True,
            capture_output=True,
        )
        result = json.loads(replay.read_text())
        if not result["all_agree"] or result["report_sha256"] != digest(path):
            raise ValueError("replay does not bind the complete report")
        replays.append(
            {
                "path": str(replay.relative_to(root)),
                "sha256": digest(replay),
                "n": result["n"],
                "all_agree": result["all_agree"],
            }
        )
        return report, {
            "path": str(path.relative_to(root)),
            "sha256": digest(path),
            "report": report,
        }

    for pair in specification["pairs"]:
        before, old_evidence = read_run(pair["baseline"])
        after, new_evidence = read_run(pair["candidate"])
        comparison = compare_runs(before, after)
        pairs.append(
            {
                "track": pair["track"],
                "baseline": old_evidence,
                "candidate": new_evidence,
                "comparison": comparison,
            }
        )
        reports.append(after)
        baselines.append(before)
    supervisor = []
    for name in specification["supervisor_runs"]:
        path = base / name / "report.json"
        report = json.loads(path.read_text())
        if report["summary"]["n"] != 3 or len(report["results"]) != 3:
            raise ValueError("incomplete Supervisor sample")
        supervisor.append(
            {"path": str(path.relative_to(root)), "sha256": digest(path), "report": report}
        )
    path = base / specification["qualification"]
    qualification = {
        "path": str(path.relative_to(root)),
        "sha256": digest(path),
        "result": json.loads(path.read_text()),
    }
    gates = release_diagnostics(reports, baselines=baselines)
    packet = {
        "iteration": specification["iteration"],
        "manifest_sha256": digest(args.manifest),
        "pairs": pairs,
        "replays": replays,
        "supervisor": supervisor,
        "feedback_qualification": qualification,
        "gates": gates,
        "production_accepted": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".json").write_text(
        json.dumps(packet, ensure_ascii=False, indent=2) + "\n"
    )
    lines = [
        "# 第六期效果迭代：实测结果",
        "",
        "本报告由完整原始证据生成；公开开发集、本地评分协议，不是官方榜单或生产验收。",
        "",
        "| 轨道 | 原策略 | 改进版 | 改善/回退题 | 原/改进服务失败 | 双侧配对 p 值 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for pair in pairs:
        c = pair["comparison"]
        p = c["mcnemar_exact_two_sided_p"]
        cells = [
            pair["track"],
            f"{c['baseline_passed']}/{c['n']}",
            f"{c['candidate_passed']}/{c['n']}",
            f"{len(c['improvements'])}/{len(c['regressions'])}",
            f"{c['service_failures']['baseline']}/{c['service_failures']['candidate']}",
            f"{p:.4f}" if p is not None else "服务故障，不作归因",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        (
            "p 值为逐轨道的未校正描述性配对检验；小样本、公开数据上的定向开发和多重比较限制其解释。"
            "回退题仍单独阻断，不能用净增抵消。"
        ),
        "",
        "## 效率与证据",
        "",
        "| 轨道 | 原/改进上报 token | 原/改进 P95 秒 | 完整计费 |",
        "|---|---:|---:|---|",
    ]
    for pair in pairs:
        e = pair["comparison"]["efficiency"]
        a, b = e["baseline"], e["candidate"]
        cost = "有" if a["cost_usd"] is not None and b["cost_usd"] is not None else "未取得"
        cells = [
            pair["track"],
            f"{a['reported_tokens']}/{b['reported_tokens']}",
            f"{a['p95_seconds']:.2f}/{b['p95_seconds']:.2f}",
            cost,
        ]
        lines.append("| " + " | ".join(cells) + " |")
    q = qualification["result"]
    lines += [
        "",
        f"无模型回放：{sum(r['n'] for r in replays)} 条判定全部一致。",
        (
            f"历史工具反馈 {q['reproduced']} 条重算，字符数 "
            f"{q['before_characters']} → {q['after_characters']}；不是 token 或费用实测节省。"
        ),
        "",
        "## 实际产品 Supervisor",
        "",
        "| 模式 | 成功 | 数据库未变化 |",
        "|---|---:|---|",
    ]
    for evidence in supervisor:
        r = evidence["report"]
        name = "请求参数绑定工具" if r["bound_tool_arguments"] else "原工具"
        name += "/验证后完成" if r.get("early_validated_completion") else "/原结束条件"
        lines.append(
            f"| {name} | {r['summary']['passed']}/{r['summary']['n']} | {r['database_unchanged']} |"
        )
    lines += [
        "",
        (
            "Supervisor 为三个 TLC 认证指标的小规模联调，"
            "成功要求角色、请求、结果及证据哈希同时通过；"
            "不替代新公开数据源的产品适配或独立上线验收。"
        ),
        "",
        "## 发布门禁",
        "",
        "`production_accepted=false`。当前阻断：",
        "",
    ]
    lines += ["- " + reason for reason in gates["blocked_by"]]
    lines += [
        "",
        f"完整机器证据：[JSON]({args.output.with_suffix('.json').name})。原始报告和哈希保存在机器证据内；未修改默认模型、账户或外部系统。",
        "",
    ]
    args.output.with_suffix(".md").write_text("\n".join(lines))
    print(
        json.dumps(
            {
                "pairs": len(pairs),
                "replayed_tasks": sum(r["n"] for r in replays),
                "production_accepted": False,
            }
        )
    )


if __name__ == "__main__":
    main()
