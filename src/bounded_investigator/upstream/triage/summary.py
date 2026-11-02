"""Human-readable projection of ``triage.json``."""

from __future__ import annotations

from pathlib import Path

from .failure_format import format_failure_counters


def render_summary(payload: dict) -> str:
    stats = payload["stats"]
    usage = payload["usage"]
    config = payload["config"]
    assertions = payload["assertions"]
    failed = [item for item in assertions if not item["passed"]]
    r2_model = config.get("round2_model_id") or "disabled"
    lines = [
        "阶段 2 快速分诊 (V17 Bottom-Anchored Parallel Analysis + Latest Result + Provider Health + V5 Decision Logic)",
        "=" * 60,
        "样本: %s" % payload["sample"].get("name", "?"),
        "Round1 模型: %s -> %s"
        % (config.get("model_profile") or "?", config.get("model_id") or "?"),
        "Round2 模型: %s -> %s"
        % (config.get("round2_model_profile") or "?", r2_model),
        "UNDECIDED 输入: %d" % stats["undecided_in"],
        "Round 1: ANALYZE=%d SKIP=%d UNCERTAIN=%d"
        % (stats["r1_analyze"], stats["r1_skip"], stats["r1_uncertain"]),
        "Round 2: total=%d ANALYZE=%d SKIP=%d forced=%d retry_recovered=%d"
        % (
            stats["r2_total"],
            stats["r2_analyze"],
            stats["r2_skip"],
            stats["forced_analyze_on_failure"],
            stats["r2_retry_recovered"],
        ),
        "Round1 执行: root=%d first_ok=%d retry_recovered=%d split=%d/%d failed=%d"
        % (
            stats["tasks_total"],
            stats["first_attempt_ok"],
            stats["retry_recovered"],
            stats["split_recovered"],
            stats["split_tasks"],
            stats["terminal_failed"],
        ),
        "Round1 失败 attempt: %s" % format_failure_counters((
            ("schema", stats["schema_failed"]),
            ("id", stats["id_mismatch"]),
            ("provider", stats["provider_error"]),
            ("timeout", stats["provider_timeout"]),
            ("budget", stats["budget_exceeded"]),
        )),
        "Round2 失败 attempt: %s" % format_failure_counters((
            ("schema", stats["r2_schema_failed"]),
            ("id", stats["r2_id_mismatch"]),
            ("provider", stats["r2_provider_error"]),
            ("timeout", stats["r2_provider_timeout"]),
            ("budget", stats["r2_budget_exceeded"]),
        )),
        "SecondHop: packages=%d complete=%d partial=%d failed=%d truncated=%d"
        % (
            stats["second_hop_packages"],
            stats["second_hop_complete"],
            stats["second_hop_partial"],
            stats["second_hop_failed"],
            stats["second_hop_truncated"],
        ),
        "最终 task: %d  |  最终判定: ANALYZE=%d SKIP=%d"
        % (len(payload["tasks"]), stats["final_analyze"], stats["final_skip"]),
        "用量: requests=%d input=%d output=%d wall=%dms"
        % (
            usage["requests"],
            usage["input_tokens"],
            usage["output_tokens"],
            usage["wall_ms"],
        ),
        "断言: %d/%d 通过" % (len(assertions) - len(failed), len(assertions)),
    ]
    for item in failed:
        lines.append("  [FAIL] %s: %s" % (item["name"], item["detail"]))
    return "\n".join(lines) + "\n"


def write_summary(path: Path, payload: dict) -> str:
    text = render_summary(payload)
    path.write_text(text, encoding="utf-8")
    return text
