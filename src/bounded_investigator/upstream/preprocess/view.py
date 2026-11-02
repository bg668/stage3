"""Render UNDECIDED functions into bounded triage units."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import Function, UNDECIDED

DEFAULT_MAX_FUNCTIONS_PER_UNIT = 80
DEFAULT_MAX_CHARS_PER_UNIT = 12000
DEFAULT_FUNCTION_CODE_MAX_CHARS = 12000
CODE_VIEW_MODES = ("minimal", "landmark", "full")
DEFAULT_CODE_VIEW = "full"

HEAD_LINES = 2
TAIL_LINES = 1
SMALL_CODE_FULL_CHARS = 300
LANDMARK_BUDGET_DIVISOR = 10
LANDMARK_BUDGET_MIN = 120
LANDMARK_BUDGET_MAX = 500
LANDMARK_MAX_LINES = 20
LANDMARK_LINE_CHARS = 100
MAX_API_ITEMS = 6
MAX_STRING_ITEMS = 4
MAX_STRING_CHARS = 48
MAX_CAPABILITY_ITEMS = 4

_CONTROL_RE = re.compile(r"\b(if|else|for|while|switch|case|do|goto)\b")
_RETURN_RE = re.compile(r"\breturn\b")
_CALL_RE = re.compile(r"\b\w+\s*\(")
_FUNCTION_POINTER_DECL_RE = re.compile(
    r"\(\s*(?:(?:__\w+|const|volatile)\s+)*\*\s*[A-Za-z_]\w*\s*\)"
)
_DECLARATOR_TAIL_RE = re.compile(
    r"(?:\s+|\*+)\s*[A-Za-z_]\w*\s*(?:\[[^\]]*\]\s*)*$"
)
_NON_DECLARATION_PREFIXES = (
    "return ", "goto ", "if ", "for ", "while ", "switch ", "case ",
    "break", "continue", "do ", "else", "throw ", "__asm", "asm ",
)


@dataclass(frozen=True)
class PreparedCode:
    code: str
    removed_lines: int = 0
    removed_chars: int = 0


@dataclass(frozen=True)
class FunctionRender:
    text: str
    truncated_by_function_limit: bool = False
    removed_variable_declaration_lines: int = 0
    removed_variable_declaration_chars: int = 0


def _truncate(text: str, limit: int) -> str:
    text = text.replace("\n", "\\n").replace("\r", "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _join(items: list[str], max_items: int, item_limit: int = 0) -> str:
    if not items:
        return "-"
    shown = items[:max_items]
    if item_limit:
        shown = [_truncate(item, item_limit) for item in shown]
    text = ", ".join(shown)
    if len(items) > max_items:
        text += " (+%d)" % (len(items) - max_items)
    return text


def _normalize_code(code: str) -> str:
    return code.replace("\r\n", "\n").replace("\r", "\n")


def _is_variable_declaration(line: str) -> bool:
    """Conservatively recognize one-line, uninitialized local declarations.

    This intentionally accepts false negatives: only the declaration prefix at the
    beginning of a Hex-Rays function body is eligible for removal, and any line that
    can plausibly carry behavior stops the removal pass.
    """
    statement = line.split("//", 1)[0].strip()
    if not statement.endswith(";"):
        return False
    statement = statement[:-1].strip()
    if not statement or "=" in statement or "{" in statement or "}" in statement:
        return False
    lowered = statement.lower()
    if lowered.startswith(_NON_DECLARATION_PREFIXES):
        return False
    if any(token in statement for token in ("->", ".", "+", "-", "/", "%", "?", "|", "!")):
        return False
    if _FUNCTION_POINTER_DECL_RE.search(statement):
        return True
    return bool(_DECLARATOR_TAIL_RE.search(statement))


def _prepare_code(code: str, remove_variable_declarations: bool) -> PreparedCode:
    normalized = _normalize_code(code)
    if not remove_variable_declarations or not normalized:
        return PreparedCode(normalized)

    lines = normalized.split("\n")
    body_start = next((i for i, line in enumerate(lines) if "{" in line), None)
    if body_start is None:
        return PreparedCode(normalized)

    kept = lines[: body_start + 1]
    removed_lines = 0
    removed_chars = 0
    scanning = True

    for line in lines[body_start + 1 :]:
        stripped = line.strip()
        if scanning and (not stripped or stripped.startswith("//")):
            kept.append(line)
            continue
        if scanning and _is_variable_declaration(line):
            removed_lines += 1
            removed_chars += len(line)
            continue
        scanning = False
        kept.append(line)

    return PreparedCode(
        "\n".join(kept),
        removed_lines=removed_lines,
        removed_chars=removed_chars,
    )


def _significant_lines(code: str) -> list[tuple[int, str]]:
    return [(index, line) for index, line in enumerate(code.split("\n")) if line.strip()]


def _minimal_snippet(code: str) -> list[str]:
    lines = [line for _, line in _significant_lines(code)]
    if not lines:
        return []
    out = [
        "  | %s" % _truncate(line.strip(), LANDMARK_LINE_CHARS)
        for line in lines[:HEAD_LINES]
    ]
    if len(lines) > HEAD_LINES + TAIL_LINES:
        out.append("  | ... (%d 行省略)" % (len(lines) - HEAD_LINES - TAIL_LINES))
    if len(lines) > HEAD_LINES:
        out += [
            "  | %s" % _truncate(line.strip(), LANDMARK_LINE_CHARS)
            for line in lines[-TAIL_LINES:]
        ]
    return out


def _landmark_scores(fn: Function, code: str) -> dict[int, int]:
    significant = _significant_lines(code)
    if not significant:
        return {}

    scores: dict[int, int] = {significant[0][0]: 100}
    dangerous = [item for item in fn.dangerous_apis[:10] if item]
    apis = [item for item in fn.api_refs[:10] if item]
    strings = [item for item in fn.string_refs[:10] if item and len(item) >= 3]

    for index, line in significant:
        score = scores.get(index, 0)
        if any(item in line for item in dangerous):
            score = max(score, 90)
        elif any(item in line for item in apis):
            score = max(score, 70)
        if any(item in line for item in strings):
            score = max(score, 60)
        stripped = line.strip()
        if _CONTROL_RE.search(stripped):
            score = max(score, 50)
        if _RETURN_RE.search(stripped):
            score = max(score, 30)
        elif _CALL_RE.search(stripped):
            score = max(score, 10)
        if score:
            scores[index] = score
    return scores


def _landmark_snippet(fn: Function, code: str) -> list[str]:
    significant = _significant_lines(code)
    if not significant:
        return []
    if len(code) <= SMALL_CODE_FULL_CHARS:
        return [
            "  | %s" % _truncate(line.strip(), LANDMARK_LINE_CHARS)
            for _, line in significant
        ]

    scores = _landmark_scores(fn, code)
    if not scores:
        return _minimal_snippet(code)

    budget = min(
        LANDMARK_BUDGET_MAX,
        max(LANDMARK_BUDGET_MIN, len(code) // LANDMARK_BUDGET_DIVISOR),
    )
    by_index = dict(significant)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    keep: list[int] = []
    used = 0
    for index, _score in ranked:
        text = _truncate(by_index.get(index, "").strip(), LANDMARK_LINE_CHARS)
        if keep and (used + len(text) > budget or len(keep) >= LANDMARK_MAX_LINES):
            break
        keep.append(index)
        used += len(text)
    keep.sort()

    out: list[str] = []
    previous = None
    for index in keep:
        if previous is not None:
            gap = sum(1 for line_index, _ in significant if previous < line_index < index)
            if gap:
                out.append("  | ... (省略 %d 行)" % gap)
        raw = by_index.get(index, "")
        indent = len(raw) - len(raw.lstrip())
        out.append(
            "  | %s%s"
            % (" " * min(indent // 2, 8), _truncate(raw.strip(), LANDMARK_LINE_CHARS))
        )
        previous = index

    trailing = sum(1 for line_index, _ in significant if line_index > keep[-1])
    if trailing:
        out.append("  | ... (省略 %d 行)" % trailing)
    return out


def _full_snippet(code: str, max_chars: int) -> tuple[list[str], bool]:
    if max_chars <= 0:
        raise ValueError("function_code_max_chars 必须大于 0")
    truncated = len(code) > max_chars
    shown = code[:max_chars] if truncated else code
    out = ["  | %s" % line for line in shown.split("\n")]
    if truncated:
        out.append("  | ... (超出单函数 %d 字符上限,已截断)" % max_chars)
    return out, truncated


def render_function_result(
    fn: Function,
    code_view: str = DEFAULT_CODE_VIEW,
    *,
    remove_variable_declarations: bool = True,
    function_code_max_chars: int = DEFAULT_FUNCTION_CODE_MAX_CHARS,
) -> FunctionRender:
    if code_view not in CODE_VIEW_MODES:
        raise ValueError("未知 code_view: %s" % code_view)

    prepared = _prepare_code(fn.code, remove_variable_declarations)
    lines = [
        "[%s] %s %s sz=%d in=%d out=%d%s%s%s%s"
        % (
            fn.ref,
            fn.hex_address,
            fn.name,
            fn.size,
            fn.in_degree,
            fn.out_degree,
            (" bb=%d" % fn.basic_block_count) if fn.basic_block_count else "",
            (" loops=%d" % fn.loop_count) if fn.loop_count else "",
            " indirect" if fn.has_indirect_call else "",
            " named" if fn.has_user_name else "",
        )
    ]
    if prepared.removed_lines:
        lines.append(
            "  # 已去除变量声明:%d 行,%d 字"
            % (prepared.removed_lines, prepared.removed_chars)
        )
    if fn.dangerous_apis:
        lines.append("  danger: %s" % _join(fn.dangerous_apis, MAX_API_ITEMS))
    if fn.api_refs:
        lines.append("  api:    %s" % _join(fn.api_refs, MAX_API_ITEMS))
    if fn.string_refs:
        lines.append("  str:    %s" % _join(fn.string_refs, MAX_STRING_ITEMS, MAX_STRING_CHARS))
    if fn.capability_hits:
        labels = list(dict.fromkeys(
            "%s[%s,%s]" % (
                hit.capability,
                hit.confidence,
                "owner" if hit.owner_eligible else "context",
            )
            for hit in fn.capability_hits
        ))
        lines.append("  cap:    %s" % _join(labels, MAX_CAPABILITY_ITEMS))

    truncated = False
    if code_view == "minimal":
        lines.extend(_minimal_snippet(prepared.code))
    elif code_view == "landmark":
        lines.extend(_landmark_snippet(fn, prepared.code))
    else:
        snippet, truncated = _full_snippet(prepared.code, function_code_max_chars)
        lines.extend(snippet)

    return FunctionRender(
        text="\n".join(lines),
        truncated_by_function_limit=truncated,
        removed_variable_declaration_lines=prepared.removed_lines,
        removed_variable_declaration_chars=prepared.removed_chars,
    )


def render_function(
    fn: Function,
    code_view: str = DEFAULT_CODE_VIEW,
    *,
    remove_variable_declarations: bool = True,
    function_code_max_chars: int = DEFAULT_FUNCTION_CODE_MAX_CHARS,
) -> str:
    return render_function_result(
        fn,
        code_view,
        remove_variable_declarations=remove_variable_declarations,
        function_code_max_chars=function_code_max_chars,
    ).text


HEADER = """\
# 分诊输入 —— 每个函数一个特征块
#
# 首行字段:
#   [ref]  地址  名字  sz=字节数  in=被调用次数  out=调用次数
#   bb=基本块数  loops=循环数  indirect=存在间接调用
#   named=名字来自符号或分析工具推断,而非自动生成的 sub_*
# 后续行(为空则省略):
#   #        渲染时发生的确定性裁剪说明
#   danger:  危险 API 引用
#   api:     其他 API / 全局引用
#   str:     字符串引用
#   cap:     程序规则发现的可疑能力线索,不是恶意性结论
#   |        伪代码视图;full 模式仍受单函数字符上限约束
#
# 原始完整伪代码独立保存在 code/F-xxxx.txt;unit 中的裁剪不修改事实源。
"""


def render_unit(
    functions: list[Function],
    unit_id: str,
    code_view: str = DEFAULT_CODE_VIEW,
    *,
    remove_variable_declarations: bool = True,
    function_code_max_chars: int = DEFAULT_FUNCTION_CODE_MAX_CHARS,
) -> str:
    body = "\n\n".join(
        render_function(
            fn,
            code_view,
            remove_variable_declarations=remove_variable_declarations,
            function_code_max_chars=function_code_max_chars,
        )
        for fn in functions
    )
    return "%s\n# unit: %s  (%d 个函数, code-view=%s)\n\n%s\n" % (
        HEADER,
        unit_id,
        len(functions),
        code_view,
        body,
    )


def pack_units(
    functions: list[Function],
    max_functions: int = DEFAULT_MAX_FUNCTIONS_PER_UNIT,
    max_chars: int = DEFAULT_MAX_CHARS_PER_UNIT,
    code_view: str = DEFAULT_CODE_VIEW,
    *,
    remove_variable_declarations: bool = True,
    function_code_max_chars: int = DEFAULT_FUNCTION_CODE_MAX_CHARS,
) -> list[dict]:
    if max_functions <= 0:
        raise ValueError("max_functions 必须大于 0")
    if max_chars <= 0:
        raise ValueError("max_chars 必须大于 0")

    pending = [fn for fn in functions if fn.decision == UNDECIDED]
    units: list[dict] = []
    current: list[tuple[Function, FunctionRender]] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if not current:
            return
        unit_id = "U-%04d" % (len(units) + 1)
        body = "\n\n".join(render.text for _, render in current)
        text = "%s\n# unit: %s  (%d 个函数, code-view=%s)\n\n%s\n" % (
            HEADER,
            unit_id,
            len(current),
            code_view,
            body,
        )
        units.append(
            {
                "unit_id": unit_id,
                "subject_refs": [fn.ref for fn, _ in current],
                "function_count": len(current),
                "function_metrics": [
                    {
                        "ref": fn.ref,
                        "truncated_by_function_limit": render.truncated_by_function_limit,
                    }
                    for fn, render in current
                ],
                "chars": current_chars,
                "text": text,
            }
        )
        current, current_chars = [], 0

    for fn in pending:
        rendered = render_function_result(
            fn,
            code_view,
            remove_variable_declarations=remove_variable_declarations,
            function_code_max_chars=function_code_max_chars,
        )
        block_chars = len(rendered.text)
        if len(current) >= max_functions or (
            current and current_chars + block_chars > max_chars
        ):
            flush()
        current.append((fn, rendered))
        current_chars += block_chars
    flush()
    return units


def compare_code_views(
    functions: list[Function],
    max_functions: int = DEFAULT_MAX_FUNCTIONS_PER_UNIT,
    max_chars: int = DEFAULT_MAX_CHARS_PER_UNIT,
    *,
    remove_variable_declarations: bool = True,
    function_code_max_chars: int = DEFAULT_FUNCTION_CODE_MAX_CHARS,
) -> list[dict]:
    pending = [fn for fn in functions if fn.decision == UNDECIDED]
    rows = []
    for mode in CODE_VIEW_MODES:
        units = pack_units(
            functions,
            max_functions,
            max_chars,
            mode,
            remove_variable_declarations=remove_variable_declarations,
            function_code_max_chars=function_code_max_chars,
        )
        total = sum(unit["chars"] for unit in units)
        rows.append(
            {
                "mode": mode,
                "units": len(units),
                "total_chars": total,
                "chars_per_function": round(total / len(pending), 1) if pending else 0,
            }
        )
    return rows
