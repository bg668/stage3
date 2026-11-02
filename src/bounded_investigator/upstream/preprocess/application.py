"""Application service for stage-1 preprocessing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import checks, pipeline, view
from .analysis_role import assign_analysis_roles
from .capability_detection import detect_capabilities
from .contract import build_payload
from .rules import RuleContext
from .source import SqliteSource
from .summary import write_summary


@dataclass(frozen=True)
class PreprocessOptions:
    sqlite: Path
    out: Path
    key_functions: Path | None = None
    sample_name: str | None = None
    max_functions_per_unit: int = view.DEFAULT_MAX_FUNCTIONS_PER_UNIT
    max_chars_per_unit: int = view.DEFAULT_MAX_CHARS_PER_UNIT
    trivial_max_size: int = 64
    code_view: str = view.DEFAULT_CODE_VIEW
    remove_variable_declarations: bool = True
    function_code_max_chars: int = view.DEFAULT_FUNCTION_CODE_MAX_CHARS
    compare_code_views: bool = False
    no_code_dump: bool = False
    capability_rules_enabled: bool = True


@dataclass
class PreprocessRun:
    payload: dict
    units: list[dict]
    summary: str

    @property
    def exit_code(self) -> int:
        return 0 if all(check["passed"] for check in self.payload["assertions"]) else 1


def run_preprocess(options: PreprocessOptions) -> PreprocessRun:
    snapshot = SqliteSource(options.sqlite).read()
    functions = snapshot.functions

    stats = pipeline.run(
        functions,
        snapshot.meta,
        RuleContext(trivial_max_size=options.trivial_max_size),
        vtable_groups=snapshot.relationships.vtable_groups,
    )
    assign_analysis_roles(functions, snapshot.relationships)
    capability_hits = detect_capabilities(
        functions,
        enabled=options.capability_rules_enabled,
    )
    units = view.pack_units(
        functions,
        max_functions=options.max_functions_per_unit,
        max_chars=options.max_chars_per_unit,
        code_view=options.code_view,
        remove_variable_declarations=options.remove_variable_declarations,
        function_code_max_chars=options.function_code_max_chars,
    )
    comparison = (
        view.compare_code_views(
            functions,
            options.max_functions_per_unit,
            options.max_chars_per_unit,
            remove_variable_declarations=options.remove_variable_declarations,
            function_code_max_chars=options.function_code_max_chars,
        )
        if options.compare_code_views
        else None
    )

    key_addresses = checks.load_key_functions(options.key_functions)
    assertions = checks.run_all(
        functions,
        key_addresses,
        total_functions=snapshot.meta.total_functions,
    )
    func_lib_count = sum(1 for fn in functions if fn.is_library)
    flirt_status = snapshot.meta.flirt_report(func_lib_count)

    payload = build_payload(
        sqlite_path=options.sqlite,
        sample_name=options.sample_name or snapshot.meta.sample_name or options.sqlite.stem,
        sample_sha256=snapshot.meta.sample_sha256,
        deepextract_version=snapshot.meta.deepextract_version,
        arch=snapshot.meta.arch,
        bitness=snapshot.meta.bitness,
        flirt_status=flirt_status,
        config={
            "max_functions_per_unit": options.max_functions_per_unit,
            "max_chars_per_unit": options.max_chars_per_unit,
            "trivial_max_size": options.trivial_max_size,
            "code_view": options.code_view,
            "remove_variable_declarations": options.remove_variable_declarations,
            "function_code_max_chars": options.function_code_max_chars,
            "capability_rules_enabled": options.capability_rules_enabled,
        },
        code_view_comparison=comparison,
        stats=stats,
        diagnostics=snapshot.diagnostics,
        assertions=assertions,
        functions=functions,
        capability_hits=capability_hits,
        relationships=snapshot.relationships,
        units=units,
    )

    _write_outputs(options, payload, units, functions)
    summary = write_summary(options.out / "summary.txt", payload)
    return PreprocessRun(payload=payload, units=units, summary=summary)


def _write_outputs(options: PreprocessOptions, payload: dict, units: list[dict], functions) -> None:
    options.out.mkdir(parents=True, exist_ok=True)
    (options.out / "preprocess.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    if not options.compare_code_views:
        units_dir = options.out / "units"
        units_dir.mkdir(exist_ok=True)
        for unit in units:
            (units_dir / ("%s.txt" % unit["unit_id"])).write_text(
                unit["text"], encoding="utf-8"
            )

    if not options.no_code_dump:
        code_dir = options.out / "code"
        code_dir.mkdir(exist_ok=True)
        for fn in functions:
            if fn.code:
                (code_dir / ("%s.txt" % fn.ref)).write_text(fn.code, encoding="utf-8")
