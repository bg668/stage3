"""Deterministically classify whether a function needs its own Stage-3 primary slot.

This is deliberately structural, not maliciousness logic. PROGRAM_SKIP functions
have no analysis role because they are already outside later analysis scope. A
SUPPORT_FORWARDER is kept in the artifact and may be used as supporting evidence,
but it does not need an independent investigation task because its body only
forwards one direct call. False negatives are preferred over false positives: any
ambiguity stays PRIMARY.
"""

from __future__ import annotations

import re
from typing import Iterable

from .model import ANALYSIS_PRIMARY as PRIMARY, ANALYSIS_SUPPORT_FORWARDER as SUPPORT_FORWARDER, Function
from .relations import Relationships


_FAILED_DECOMPILE = {"FAILED", "TIMEOUT", "UNAVAILABLE"}
_CONTROL_RE = re.compile(r"\b(if|else|for|while|switch|case|do|goto|try|catch|throw)\b")
_DIRECT_CALL_RE = re.compile(r"^([A-Za-z_]\w*)\s*\((.*)\)$", re.DOTALL)
_NESTED_CALL_RE = re.compile(r"\b[A-Za-z_]\w*\s*\(")
_CAST_PREFIX_RE = re.compile(
    r"^\(\s*(?:const\s+|volatile\s+|signed\s+|unsigned\s+)*"
    r"(?:struct\s+|enum\s+)?[A-Za-z_]\w*"
    r"(?:\s+[A-Za-z_]\w*)*(?:\s*\*+\s*)?\)\s*"
)
_SIDE_EFFECT_ARG_RE = re.compile(
    r"\+\+|--|<<=|>>=|\+=|-=|\*=|/=|%=|&=|\|=|\^=|(?<![=!<>])=(?!=)|\?"
)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def assign_analysis_roles(functions: Iterable[Function], relationships: Relationships) -> None:
    """Populate ``Function.analysis_role`` in place from deterministic source facts."""

    # DeepExtract inbound_xrefs currently overlap heavily with ordinary CALL edges.
    # Treat only reference pairs that are *not* already represented as direct calls
    # as potential data/callback references; otherwise every normal caller would
    # incorrectly force a thin forwarder back to PRIMARY.
    call_edges = set(relationships.call_edges)
    non_call_reference_edges = {
        edge for edge in relationships.reference_edges if edge not in call_edges
    }
    reference_targets = {target for _source, target in non_call_reference_edges}
    vtable_members = set(getattr(relationships, "vtable_members", set()))
    if not vtable_members:
        vtable_members = {
            ref
            for members in relationships.vtable_groups.values()
            for ref in members
        }
    for fn in functions:
        if fn.decision == "PROGRAM_SKIP":
            # analysis_role only describes functions that may still enter later
            # analysis. A deterministic Stage-1 skip is already out of scope.
            fn.analysis_role = None
            continue
        fn.analysis_role = classify_analysis_role(
            fn,
            referenced_as_data=fn.ref in reference_targets,
            in_vtable=fn.ref in vtable_members,
        )


def classify_analysis_role(
    fn: Function,
    *,
    referenced_as_data: bool = False,
    in_vtable: bool = False,
) -> str:
    """Return PRIMARY unless the function is provably a thin direct-call forwarder.

    The role answers only whether the function adds enough local behavior semantics
    to justify an independent Stage-3 primary.  It does not decide whether the
    callee/API is benign or malicious.
    """

    if fn.is_entry is True or fn.is_export is True or fn.export_name:
        return PRIMARY
    if referenced_as_data or in_vtable:
        # Callback/vtable targets may be independently invoked even when their body
        # is tiny; removing them from primary scope could hide the actual action.
        return PRIMARY
    if fn.is_library is True or fn.is_thunk is True:
        # Existing Stage-1 rules own library/thunk semantics.
        return PRIMARY
    if not fn.has_code:
        return PRIMARY
    if fn.decompile_status and str(fn.decompile_status).strip().upper() in _FAILED_DECOMPILE:
        return PRIMARY
    if fn.has_indirect_call is True:
        return PRIMARY
    if fn.loop_count not in (None, 0):
        return PRIMARY
    if fn.basic_block_count is not None and fn.basic_block_count != 1:
        return PRIMARY
    if len(fn.callees) > 1:
        return PRIMARY
    if fn.string_refs:
        # A bound literal/path/object is data semantics supplied by the wrapper.
        return PRIMARY

    statements = _body_statements(fn.code)
    if statements is None:
        return PRIMARY

    if len(statements) == 1 and _is_single_direct_call(statements[0]):
        return SUPPORT_FORWARDER
    if (
        len(statements) == 2
        and _is_direct_call_statement(statements[0])
        and statements[1].strip() == "return;"
    ):
        return SUPPORT_FORWARDER
    return PRIMARY


def _body_statements(code: str) -> tuple[str, ...] | None:
    """Extract top-level decompiler statements; ambiguity returns ``None``."""

    text = _BLOCK_COMMENT_RE.sub("", code.replace("\r\n", "\n").replace("\r", "\n"))
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    body = text[start + 1 : end]
    body = "\n".join(line.split("//", 1)[0] for line in body.split("\n"))
    if not body.strip() or _CONTROL_RE.search(body):
        return None
    if "{" in body or "}" in body:
        return None

    statements: list[str] = []
    current: list[str] = []
    paren_depth = 0
    for char in body:
        current.append(char)
        if char == "(":
            paren_depth += 1
        elif char == ")":
            paren_depth -= 1
            if paren_depth < 0:
                return None
        elif char == ";" and paren_depth == 0:
            statement = "".join(current).strip()
            if statement:
                statements.append(_collapse_ws(statement))
            current = []
    if paren_depth != 0 or "".join(current).strip():
        return None
    return tuple(statements)


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _is_single_direct_call(statement: str) -> bool:
    return _is_return_direct_call(statement) or _is_direct_call_statement(statement)


def _is_return_direct_call(statement: str) -> bool:
    text = statement.strip()
    if not text.startswith("return ") or not text.endswith(";"):
        return False
    expression = text[len("return ") : -1].strip()
    return _is_pure_single_call_expression(expression, allow_return_adapters=True)


def _is_direct_call_statement(statement: str) -> bool:
    text = statement.strip()
    if not text.endswith(";"):
        return False
    expression = text[:-1].strip()
    return _is_pure_single_call_expression(expression, allow_return_adapters=False)


def _is_pure_single_call_expression(expression: str, *, allow_return_adapters: bool) -> bool:
    """Whether *expression* is exactly one direct call plus pure adaptation.

    Thin wrappers frequently normalize an API result (``!Api(...)`` or a C cast)
    or adapt arguments with pure expressions (``flags & 1``).  Those operations do
    not add an independent behavior and should not create a Stage-3 primary.

    This stays deliberately conservative: nested calls, assignments, increments,
    ternaries, or any suffix computation around the call are rejected.
    """

    text = expression.strip()
    if not text:
        return False

    if allow_return_adapters:
        normalized = _strip_pure_return_adapters(text)
        if normalized is None:
            return False
        text = normalized

    match = _DIRECT_CALL_RE.fullmatch(text)
    if not match:
        return False
    arguments = match.group(2)
    if _NESTED_CALL_RE.search(arguments):
        return False
    if _SIDE_EFFECT_ARG_RE.search(arguments):
        return False
    return True


def _strip_pure_return_adapters(expression: str) -> str | None:
    """Remove only side-effect-free wrappers around a returned call expression.

    Supported adapters are outer parentheses, C-style casts and unary ``!``, ``~``,
    ``+`` or ``-``.  ``++``/``--`` are rejected because they mutate state.
    """

    text = expression.strip()
    while True:
        changed = False

        stripped = _strip_outer_parentheses(text)
        if stripped != text:
            text = stripped
            changed = True

        if text.startswith(("++", "--")):
            return None
        if text[:1] in {"!", "~", "+", "-"}:
            text = text[1:].lstrip()
            changed = True
            continue

        cast = _CAST_PREFIX_RE.match(text)
        if cast:
            text = text[cast.end() :].lstrip()
            changed = True
            continue

        if not changed:
            return text


def _strip_outer_parentheses(expression: str) -> str:
    text = expression.strip()
    while len(text) >= 2 and text[0] == "(" and text[-1] == ")":
        depth = 0
        wraps_all = True
        for index, char in enumerate(text):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    return text
                if depth == 0 and index != len(text) - 1:
                    wraps_all = False
                    break
        if depth != 0 or not wraps_all:
            break
        text = text[1:-1].strip()
    return text
