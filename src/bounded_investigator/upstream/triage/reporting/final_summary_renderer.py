"""Static Rich renderer for the final Stage-2 result.

It consumes the structured triage payload directly.  It never parses the human summary
text, so presentation changes cannot affect result semantics.
"""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

from ..failure_format import format_failure_counters


def render_final_summary(payload: dict, out_dir: Path, *, stream: TextIO) -> bool:
    """Render the final result in Rich when available and interactive.

    Returns True when Rich output was used; callers may fall back to plain summary when
    False.  This function is intentionally independent from the live dashboard lifetime.
    """
    if not bool(getattr(stream, "isatty", lambda: False)()):
        return False
    try:
        from rich.console import Console, Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
    except (ImportError, ModuleNotFoundError):
        return False

    console = Console(file=stream, highlight=False, soft_wrap=True)
    stats = payload["stats"]
    usage = payload["usage"]
    config = payload["config"]
    assertions = payload["assertions"]
    failed = [item for item in assertions if not item["passed"]]

    overview = _kv_table(Table, Text, [
        ("样本", str(payload["sample"].get("name", "?"))),
        ("Round1 模型", "%s → %s" % (config.get("model_profile") or "?", config.get("model_id") or "?")),
        ("Round2 模型", "%s → %s" % (config.get("round2_model_profile") or "?", config.get("round2_model_id") or "disabled")),
        ("UNDECIDED 输入", str(stats["undecided_in"])),
        ("输出目录", str(out_dir)),
    ])

    outcome = _kv_table(Table, Text, [
        ("Round 1", "ANALYZE=%d  SKIP=%d  UNCERTAIN=%d" % (stats["r1_analyze"], stats["r1_skip"], stats["r1_uncertain"])),
        ("Round 2", "total=%d  ANALYZE=%d  SKIP=%d  forced=%d  retry=%d" % (
            stats["r2_total"], stats["r2_analyze"], stats["r2_skip"], stats["forced_analyze_on_failure"], stats["r2_retry_recovered"]
        )),
        ("最终 task", str(len(payload["tasks"]))),
        ("最终判定", "ANALYZE=%d  SKIP=%d" % (stats["final_analyze"], stats["final_skip"])),
    ])

    execution = _kv_table(Table, Text, [
        ("Round1 执行", "root=%d  first_ok=%d  retry=%d  split=%d/%d  failed=%d" % (
            stats["tasks_total"], stats["first_attempt_ok"], stats["retry_recovered"], stats["split_recovered"], stats["split_tasks"], stats["terminal_failed"]
        )),
        ("Round1 失败", format_failure_counters((
            ("schema", stats["schema_failed"]),
            ("id", stats["id_mismatch"]),
            ("provider", stats["provider_error"]),
            ("timeout", stats["provider_timeout"]),
            ("budget", stats["budget_exceeded"]),
        ))),
        ("Round2 失败", format_failure_counters((
            ("schema", stats["r2_schema_failed"]),
            ("id", stats["r2_id_mismatch"]),
            ("provider", stats["r2_provider_error"]),
            ("timeout", stats["r2_provider_timeout"]),
            ("budget", stats["r2_budget_exceeded"]),
        ))),
        ("SecondHop", "packages=%d  complete=%d  partial=%d  failed=%d  truncated=%d" % (
            stats["second_hop_packages"], stats["second_hop_complete"], stats["second_hop_partial"], stats["second_hop_failed"], stats["second_hop_truncated"]
        )),
    ])

    validation = _kv_table(Table, Text, [
        ("模型请求", str(usage["requests"])),
        ("Token", "input=%d  output=%d" % (usage["input_tokens"], usage["output_tokens"])),
        ("总耗时", "%d ms" % usage["wall_ms"]),
        ("断言", "%d/%d 通过" % (len(assertions) - len(failed), len(assertions))),
    ])
    if failed:
        for item in failed[:5]:
            validation.add_row(Text("失败", style="bold red"), Text("%s：%s" % (item["name"], item["detail"]), style="red"))

    cards = [
        Panel(overview, title="运行概览", title_align="left", border_style="bright_cyan"),
        Panel(outcome, title="分诊结果", title_align="left", border_style="bright_green"),
        Panel(execution, title="执行与补料", title_align="left", border_style="bright_magenta"),
        Panel(validation, title="资源与校验", title_align="left", border_style="bright_yellow" if not failed else "bright_red"),
    ]

    if console.size.width >= 120:
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_row(cards[0], cards[1])
        grid.add_row(cards[2], cards[3])
        content = grid
    else:
        content = Group(*cards)

    console.print()
    console.print(
        Panel(
            content,
            title="阶段 2 快速分诊 · 最终结果",
            subtitle="完成" if not failed else "存在断言失败",
            title_align="left",
            border_style="bold bright_green" if not failed else "bold bright_red",
            padding=(0, 1),
        )
    )
    return True


def _kv_table(Table, Text, rows):
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(style="bold bright_white", no_wrap=True, width=14)
    table.add_column(ratio=1)
    for key, value in rows:
        table.add_row(Text(str(key), style="bold bright_white"), Text(str(value), style="bright_white"))
    return table
