"""Human-readable rendering of the preprocessing contract."""

from __future__ import annotations

from pathlib import Path

from .model import PROGRAM_ANALYZE


def render_summary(payload: dict) -> str:
    stats = payload["stats"]
    lines = [
        "预处理摘要  %s  [code-view=%s]" % (
            payload["sample"]["name"], payload["config"]["code_view"]),
        "=" * 60,
        "sqlite       : %s" % payload["sample"]["sqlite_path"],
        "DeepExtract版本: %s" % (payload["sample"]["deepextract_version"] or "未知(旧库)"),
        "FLIRT        : %s" % payload["sample"]["flirt_status"],
        "",
        "三分划分",
        "-" * 60,
        "  scope             %6d" % stats["scope"],
        "  PROGRAM_ANALYZE   %6d  (%.1f%%)" % (
            stats["program_analyze"], 100 * stats["program_analyze"] / max(stats["scope"], 1)),
        "  PROGRAM_SKIP      %6d  (%.1f%%)" % (
            stats["program_skip"], 100 * stats["program_skip"] / max(stats["scope"], 1)),
        "  UNDECIDED         %6d  (%.1f%%)   <- 需要分诊 LLM" % (
            stats["undecided"], 100 * stats["undecided"] / max(stats["scope"], 1)),
        "  PROGRAM 决策占比  %6.1f%%" % (100 * stats["program_decision_ratio"]),
        "  capability hits   %6d  (仅导航,不改变上述决策)"
        % len(payload.get("capability_hits", [])),
        "",
        "分诊 unit          %6d 个,平均 %.1f 个函数 / %.0f 字符" % (
            payload["units_summary"]["count"],
            payload["units_summary"]["mean_functions"],
            payload["units_summary"]["mean_chars"]),
        "",
        "规则贡献",
        "-" * 60,
        "  %-24s %-7s %8s %10s" % ("规则", "方向", "命中", "边际"),
    ]
    for kind, label, note in (
        ("CORRECTNESS", "正确性规则", "边际为 0 = 本样本无此类代码,与规则存废无关"),
        ("EFFICIENCY", "效率规则", "边际持续为 0 时才需重新考虑"),
    ):
        group = [r for r in stats["rules"]
                 if r.get("justification") == kind and not r.get("diagnostic_only")]
        if not group:
            continue
        lines.append("  -- %s(%s)" % (label, note))
        for rule in group:
            direction = "ANALYZE" if rule["decision"] == PROGRAM_ANALYZE else "SKIP"
            lines.append("  %-24s %-7s %8d %10d" % (
                rule["id"], direction, rule["hits"], rule["marginal"]))

    reach = stats.get("skip_reachability") or {}
    if reach.get("skipped_total"):
        by_layer = reach["reachable_by_layer"]
        lines += ["", "SKIP 集合的可恢复性(细读能否捞回)", "-" * 60,
                  "  被 SKIP 总数                  %6d" % reach["skipped_total"],
                  "  L1 仅直接调用可达             %6d" % by_layer["L1_calls_only"],
                  "  L2 + 引用边(虚表/回调)      %6d" % by_layer["L2_with_refs"],
                  "  L3 + 虚表共属                 %6d" % by_layer["L3_with_vtable"],
                  "  不可达合计                    %6d" % reach["unreachable_total"],
                  "    已识别库函数                  %6d  <- 非程序分析目标"
                  % reach["unreachable_libraries"],
                  "    桩函数(nullsub/thunk/单 return) %6d  <- 无实质行为"
                  % reach["unreachable_stubs"],
                  "    **有实质内容**              %6d  <- 真正的盲区"
                  % reach["unreachable_substantive"],
                  "  可达者的调用深度              %s"
                  % ("均值 %.1f / 最大 %d" % (reach["mean_depth"], reach["max_depth"])
                     if by_layer["L3_with_vtable"] else "n/a")]
        for sample in reach.get("substantive_samples", []):
            lines.append("      - %s" % sample)
        for rule_id, info in sorted(reach.get("per_rule", {}).items(),
                                    key=lambda item: -item[1]["substantive"]):
            if info["substantive"]:
                lines.append("    %-24s %d 个有实质内容的不可达 / 共 %d"
                             % (rule_id, info["substantive"], info["total"]))
        if reach["unreachable_substantive"] == 0:
            lines.append("  说明:有实质内容的盲区为 0 —— 误 SKIP 在架构上可由细读恢复,"
                         "key_functions 的作用是回归检测而非防灾。")
        else:
            lines.append("  说明:上面列出的函数细读走不到,key_functions 应优先覆盖它们。")

        vtable_groups = payload.get("vtable_groups") or {}
        if vtable_groups:
            functions_in_vtables = len(
                {ref for members in vtable_groups.values() for ref in members}
            )
            lines.append("  虚表分组 %d 组,涉及 %d 个函数"
                         % (len(vtable_groups), functions_in_vtables))
        else:
            lines.append("  注意:未检出虚表分组。若样本是 C++/COM,"
                         "L3 无效,可达性可能被低估。")

    diagnostic_rules = stats.get("diagnostic_rules") or {}
    if diagnostic_rules:
        lines += ["", "诊断模式规则(仅统计,不改变决策)", "-" * 60,
                  "  %-24s %10s %10s" % ("规则", "本可命中", "实际影响")]
        for rule_id, info in diagnostic_rules.items():
            lines.append("  %-24s %10d %10d" % (
                rule_id, info["would_hit"], info["would_flip"]))
            for sample in info["samples"]:
                lines.append("      - %s" % sample)
        lines.append("  说明:'实际影响' = 启用后真正会改变决策的数量;"
                     "已被其他规则抓到的不计入")

    comparison = payload.get("code_view_comparison")
    if comparison:
        lines += ["", "code-view 装箱对照(零 LLM;只反映代价,不反映信号质量)",
                  "-" * 60,
                  "  %-10s %8s %12s %16s" % ("模式", "unit 数", "总字符", "字符/函数")]
        for row in comparison:
            lines.append("  %-10s %8d %12d %16.1f" % (
                row["mode"], row["units"], row["total_chars"], row["chars_per_function"]))

    if stats["functions_without_flags"]:
        lines += ["", "注意:%d 个函数缺少 flags,依赖 flags 的规则对它们保持沉默"
                  % stats["functions_without_flags"]]

    missing = payload["diagnostics"]["semantic_columns_missing"]
    if missing:
        lines += ["", "缺失的语义列(导出中没有这些列)",
                  "-" * 60, "  " + ", ".join(missing)]
    all_null = payload["diagnostics"].get("semantic_columns_all_null") or []
    if all_null:
        lines += ["", "⚠ 列存在但全表为 NULL(极可能是 v6->v7 迁移后未重新导出)",
                  "-" * 60, "  " + ", ".join(all_null),
                  "  依赖这些列的规则会全程沉默。需在 IDA 中重新执行导出。"]

    lines += ["", "断言", "-" * 60]
    for check in payload["assertions"]:
        flag = "PASS" if check["passed"] else "FAIL"
        note = "  (跳过)" if check.get("skipped") else ""
        lines.append("  [%s] %-24s %s%s" % (flag, check["name"], check["detail"], note))
        if not check["passed"]:
            for violation in check["violations"][:10]:
                lines.append("         - %s" % violation)
    return "\n".join(lines) + "\n"


def write_summary(path: Path, payload: dict) -> str:
    text = render_summary(payload)
    path.write_text(text, encoding="utf-8")
    return text
