"""Read and normalize DeepExtract SQLite data; no decision logic lives here."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .model import Function, SampleMeta
from .relations import RelationInput, Relationships, build_relationships

_FUNCTION_TABLES = ("functions", "function", "ida_functions", "deep_functions")

_ID_COLS = ("function_id", "id", "func_id", "ea_id")
_NAME_COLS = ("function_name", "name", "func_name")
_MANGLED_COLS = ("mangled_name", "raw_name")
_ADDR_COLS = ("start_ea", "function_address", "address", "ea", "addr")
_END_COLS = ("function_end_address", "end_ea", "end_address", "end_addr", "max_ea", "end")
_SIZE_COLS = ("function_size", "size", "func_size", "length", "func_size_bytes")
_FLAG_COLS = ("function_flags", "flags", "func_flags")
_CODE_COLS = ("decompiled_code", "code", "pseudocode", "decompiled", "body")
_ASM_COLS = ("assembly_code", "assembly", "disasm")

_ENTRY_COLS = ("is_entry", "entry", "is_entrypoint")
_EXPORT_COLS = ("is_export", "exported")
_EXPORT_NAME_COLS = ("export_name",)
_SKIP_REASON_COLS = ("skip_reason",)
_DECOMPILE_STATUS_COLS = ("decompile_status",)
_INDIRECT_COLS = ("has_indirect_call",)
_BB_COLS = ("basic_block_count", "bb_count")
_USER_NAME_COLS = ("has_user_name",)
_CANDIDATE_COLS = ("analysis_candidate",)

_OUT_XREF_COLS = ("simple_outbound_xrefs", "outbound_xrefs")
_IN_XREF_COLS = ("simple_inbound_xrefs", "inbound_xrefs")
_GLOBAL_COLS = ("global_var_accesses",)
_DANGEROUS_COLS = ("dangerous_api_calls",)
_STRING_COLS = ("string_literals",)
_LOOP_COLS = ("loop_analysis",)
_VTABLE_COLS = ("vtable_contexts",)

_HEX_RE = re.compile(r"^0[xX][0-9a-fA-F]+$")


class SourceError(RuntimeError):
    pass


@dataclass
class FunctionBatch:
    functions: list[Function]
    relation_inputs: dict[str, RelationInput]
    diagnostics: dict[str, Any]


@dataclass
class SourceSnapshot:
    meta: SampleMeta
    functions: list[Function]
    relationships: Relationships
    diagnostics: dict[str, Any]


def _first(row: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    for key in candidates:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _has_any_col(row: dict[str, Any], candidates: tuple[str, ...]) -> bool:
    return any(key in row for key in candidates)


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    try:
        return int(text, 16) if _HEX_RE.match(text) else int(text)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y"):
        return True
    if text in ("0", "false", "no", "n"):
        return False
    return None


def _as_json(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list[Any]:
    parsed = _as_json(value)
    return parsed if isinstance(parsed, list) else []


def _names_from(entries: Any, keys: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(entries, list):
        return out
    for item in entries:
        value = None
        if isinstance(item, dict):
            value = next((item[key] for key in keys if item.get(key)), None)
        else:
            value = item
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


class SqliteSource:
    """DeepExtract SQLite reader and schema-normalization boundary."""

    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.exists():
            raise SourceError("找不到 sqlite 文件: %s" % self.path)

    def read(self) -> SourceSnapshot:
        meta = self.read_meta()
        batch = self.read_function_batch()
        relationships = build_relationships(batch.functions, batch.relation_inputs)
        return SourceSnapshot(meta, batch.functions, relationships, dict(batch.diagnostics))

    def read_meta(self) -> SampleMeta:
        with closing(self._connect()) as connection:
            if "analysis_metadata" not in self._tables(connection):
                return SampleMeta()
            row = connection.execute("SELECT * FROM analysis_metadata LIMIT 1").fetchone()
            if row is None:
                return SampleMeta()
            data = dict(row)

        signatures = _as_json(data.get("flirt_signatures_loaded"))
        return SampleMeta(
            sample_name=data.get("sample_name"),
            sample_sha256=data.get("sample_sha256"),
            file_type=data.get("file_type"),
            arch=data.get("arch"),
            bitness=_as_int(data.get("bitness")),
            ida_version=data.get("ida_version"),
            deepextract_version=(
                str(data["schema_version"])
                if data.get("schema_version") not in (None, "")
                else None
            ),
            flirt_signatures_loaded=signatures if isinstance(signatures, list) else None,
            decompiler_available=_as_bool(data.get("decompiler_available")),
            total_functions=_as_int(data.get("total_functions")),
        )

    def read_function_batch(self) -> FunctionBatch:
        table, rows = self._read_function_rows()
        probe = rows[0]
        diagnostics = self._probe_columns(probe, table)

        functions: list[Function] = []
        relation_inputs: dict[str, RelationInput] = {}
        for index, row in enumerate(rows):
            fn = self._build_function(row, index)
            functions.append(fn)
            relation_inputs[fn.ref] = self._relation_input(row)

        diagnostics["semantic_columns_all_null"] = self._all_null_columns(
            functions, diagnostics["semantic_columns_missing"]
        )
        return FunctionBatch(functions, relation_inputs, diagnostics)

    def read_functions(self) -> tuple[list[Function], dict[str, Any]]:
        """Compatibility API used by existing callers such as ``locate.py``."""
        batch = self.read_function_batch()
        build_relationships(batch.functions, batch.relation_inputs)
        return batch.functions, dict(batch.diagnostics)

    def _connect(self) -> sqlite3.Connection:
        try:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
        except (ValueError, sqlite3.OperationalError, sqlite3.DatabaseError):
            connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _tables(connection: sqlite3.Connection) -> list[str]:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        return [row[0] for row in rows]

    def _pick_function_table(self, connection: sqlite3.Connection) -> str:
        tables = self._tables(connection)
        for candidate in _FUNCTION_TABLES:
            if candidate in tables:
                return candidate
        raise SourceError(
            "未找到函数表。已有表: %s;期望其中之一: %s"
            % (", ".join(tables), ", ".join(_FUNCTION_TABLES))
        )

    def _read_function_rows(self) -> tuple[str, list[dict[str, Any]]]:
        with closing(self._connect()) as connection:
            table = self._pick_function_table(connection)
            columns = {
                row[1] for row in connection.execute('PRAGMA table_info("%s")' % table)
            }
            address_columns = [column for column in _ADDR_COLS if column in columns]
            order_by = (
                "COALESCE(%s, rowid)" % ", ".join(address_columns)
                if address_columns
                else "rowid"
            )
            rows = [
                dict(row)
                for row in connection.execute(
                    'SELECT * FROM "%s" ORDER BY %s' % (table, order_by)
                )
            ]
        if not rows:
            raise SourceError("函数表 %s 为空" % table)
        return table, rows

    @staticmethod
    def _relation_input(row: dict[str, Any]) -> RelationInput:
        return RelationInput(
            outbound_xrefs=_as_list(_first(row, _OUT_XREF_COLS)),
            inbound_xrefs=_as_list(_first(row, _IN_XREF_COLS)),
            vtable_contexts=_as_list(_first(row, _VTABLE_COLS)),
            global_var_accesses=_as_list(_first(row, _GLOBAL_COLS)),
        )

    @staticmethod
    def _all_null_columns(functions: list[Function], missing: list[str]) -> list[str]:
        getters = {
            "is_entry": lambda fn: fn.is_entry,
            "is_export": lambda fn: fn.is_export,
            "has_indirect_call": lambda fn: fn.has_indirect_call,
            "basic_block_count": lambda fn: fn.basic_block_count,
            "decompile_status": lambda fn: fn.decompile_status,
            "has_user_name": lambda fn: fn.has_user_name,
            "skip_reason": lambda fn: fn.export_skip_reason,
        }
        missing_set = set(missing)
        return sorted(
            label
            for label, getter in getters.items()
            if label not in missing_set and all(getter(fn) is None for fn in functions)
        )

    @staticmethod
    def _probe_columns(probe: dict[str, Any], table: str) -> dict[str, Any]:
        checks = {
            "address": _ADDR_COLS,
            "name": _NAME_COLS,
            "size": _SIZE_COLS,
            "flags": _FLAG_COLS,
            "code": _CODE_COLS,
            "is_entry": _ENTRY_COLS,
            "is_export": _EXPORT_COLS,
            "skip_reason": _SKIP_REASON_COLS,
            "decompile_status": _DECOMPILE_STATUS_COLS,
            "has_indirect_call": _INDIRECT_COLS,
            "basic_block_count": _BB_COLS,
            "analysis_candidate": _CANDIDATE_COLS,
            "has_user_name": _USER_NAME_COLS,
            "outbound_xrefs": _OUT_XREF_COLS,
            "dangerous_api_calls": _DANGEROUS_COLS,
            "string_literals": _STRING_COLS,
            "global_var_accesses": _GLOBAL_COLS,
        }
        available = {name: _has_any_col(probe, aliases) for name, aliases in checks.items()}
        return {
            "table": table,
            "columns_present": sorted(probe.keys()),
            "semantic_columns_available": available,
            "semantic_columns_missing": sorted(
                name for name, present in available.items() if not present
            ),
            "semantic_columns_all_null": [],
        }

    @staticmethod
    def _build_function(row: dict[str, Any], index: int) -> Function:
        address = _as_int(_first(row, _ADDR_COLS))
        if address is None:
            raise SourceError(
                "第 %d 行没有可识别的地址列。已有列: %s"
                % (index, ", ".join(sorted(row.keys())))
            )

        source_id = _first(row, _ID_COLS)
        source_id = str(source_id) if source_id is not None else str(address)
        name = _first(row, _NAME_COLS) or _first(row, _MANGLED_COLS) or ("sub_%X" % address)
        code = str(_first(row, _CODE_COLS) or "")

        size = _as_int(_first(row, _SIZE_COLS))
        if size is None:
            end = _as_int(_first(row, _END_COLS))
            size = end - address if end is not None and end > address else 0

        global_accesses = _as_list(_first(row, _GLOBAL_COLS))
        loop_data = _as_json(_first(row, _LOOP_COLS)) or {}
        return Function(
            ref="F-%04d" % (index + 1),
            source_id=source_id,
            address=address,
            name=str(name),
            size=size or 0,
            flags=_as_int(_first(row, _FLAG_COLS)),
            code=code,
            assembly=str(_first(row, _ASM_COLS) or ""),
            is_entry=(
                _as_bool(_first(row, _ENTRY_COLS)) if _has_any_col(row, _ENTRY_COLS) else None
            ),
            is_export=(
                _as_bool(_first(row, _EXPORT_COLS)) if _has_any_col(row, _EXPORT_COLS) else None
            ),
            export_name=_first(row, _EXPORT_NAME_COLS),
            decompile_status=_first(row, _DECOMPILE_STATUS_COLS),
            has_indirect_call=(
                _as_bool(_first(row, _INDIRECT_COLS))
                if _has_any_col(row, _INDIRECT_COLS)
                else None
            ),
            basic_block_count=_as_int(_first(row, _BB_COLS)),
            has_user_name=(
                _as_bool(_first(row, _USER_NAME_COLS))
                if _has_any_col(row, _USER_NAME_COLS)
                else None
            ),
            analysis_candidate=(
                _as_bool(_first(row, _CANDIDATE_COLS))
                if _has_any_col(row, _CANDIDATE_COLS)
                else None
            ),
            export_skip_reason=_first(row, _SKIP_REASON_COLS),
            api_refs=_names_from(global_accesses, ("name", "api_name")),
            dangerous_apis=_names_from(
                _as_json(_first(row, _DANGEROUS_COLS)), ("name", "api_name", "api")
            ),
            string_refs=_names_from(
                _as_json(_first(row, _STRING_COLS)), ("value", "string", "text")
            ),
            loop_count=(
                _as_int(loop_data.get("loop_count")) if isinstance(loop_data, dict) else None
            ),
        )
