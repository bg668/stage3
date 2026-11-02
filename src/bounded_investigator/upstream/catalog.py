"""Frozen, sample-scoped source catalog shared by static and dynamic analysis."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .triage.domain import FinalVerdict

def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _key(ref: str) -> int:
    if not re.fullmatch(r"F-\d+", ref):
        raise ValueError(f"invalid function ref: {ref!r}")
    return int(ref[2:])


def _unique(rows: list[dict], label: str) -> dict[str, dict]:
    result = {}
    for row in rows:
        ref = row['ref']
        _key(ref)
        if ref in result:
            raise ValueError(f"duplicate {label}: {ref}")
        result[ref] = row
    return result


def _components(nodes: set[str], edges: dict[str, set[str]]) -> list[list[str]]:
    """Iterative Kosaraju, followed by stable callee-first topological order."""
    seen, finish = set(), []
    for node in sorted(nodes, key=_key):
        if node in seen:
            continue
        stack = [(node, False)]
        while stack:
            current, done = stack.pop()
            if done:
                finish.append(current)
                continue
            if current in seen:
                continue
            seen.add(current)
            stack.append((current, True))
            stack.extend((n, False) for n in sorted(edges[current] & nodes, key=_key, reverse=True) if n not in seen)
    reverse = {n: set() for n in nodes}
    for caller in nodes:
        for callee in edges[caller] & nodes:
            reverse[callee].add(caller)
    seen, components = set(), []
    for node in reversed(finish):
        if node in seen:
            continue
        component, stack = [], [node]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            component.append(current)
            stack.extend(reverse[current] - seen)
        components.append(sorted(component, key=_key))
    owners = {ref: i for i, group in enumerate(components) for ref in group}
    deps = {i: {owners[callee] for caller in group for callee in edges[caller] & nodes if owners[callee] != i} for i, group in enumerate(components)}
    pending, ordered = set(deps), []
    while pending:
        ready = sorted((i for i in pending if not (deps[i] & pending)), key=lambda i: _key(components[i][0]))
        if not ready:
            raise ValueError("component condensation unexpectedly contains a cycle")
        # Choose one at a time so newly ready nodes compete by numeric ref.
        current = ready[0]
        ordered.append(components[current])
        pending.remove(current)
    return ordered


@dataclass
class SourceCatalog:
    preprocess_path: Path
    triage_path: Path
    sample: dict
    config: dict
    functions: dict[str, dict]
    selected_refs: set[str]
    verdicts: dict[str, dict]
    edges: dict[str, set[str]]
    preprocess_sha256: str
    triage_sha256: str
    _codes: dict[str, str]
    _components_by_ref: dict[str, list[str]]

    @property
    def entry_refs(self) -> list[str]:
        return sorted((ref for ref in self.selected_refs if self.functions[ref].get('is_entry')), key=_key)

    @property
    def ordered_components(self) -> list[list[str]]:
        return _components(self.selected_refs, self.edges)

    def resolve(self, identifier: str) -> str:
        if identifier in self.functions:
            return identifier
        matches = [ref for ref, fn in self.functions.items() if identifier in (fn['name'], fn['address'])]
        if not matches:
            raise ValueError(f"unknown source identifier: {identifier!r}")
        if len(matches) != 1:
            raise ValueError(f"ambiguous source identifier: {identifier!r}: {matches}")
        return matches[0]

    def code(self, identifier: str) -> str:
        ref = self.resolve(identifier)
        if ref not in self._codes:
            raise ValueError(f"source unavailable: {ref}: {self.functions[ref]['unavailable_reason']}")
        return self._codes[ref]

    def components(self, identifier: str) -> list[str]:
        ref = self.resolve(identifier)
        return list(self._components_by_ref.get(ref, [ref]))

    def snapshot(self) -> dict:
        return json.loads(json.dumps({
            'schema_version': '1', 'sample': self.sample, 'config': self.config,
            'preprocess_sha256': self.preprocess_sha256, 'triage_sha256': self.triage_sha256,
            'preprocess_file': str(self.preprocess_path.resolve()), 'triage_file': str(self.triage_path.resolve()),
            'selected_count': len(self.selected_refs), 'selected_refs': sorted(self.selected_refs, key=_key),
            'entry_refs': self.entry_refs, 'functions': self.functions,
            'call_edges': [[caller, callee] for caller in sorted(self.edges, key=_key) for callee in sorted(self.edges[caller], key=_key)],
            'components': self.ordered_components,
        }, ensure_ascii=False))


def load_catalog(preprocess_path: Path, triage_path: Path) -> SourceCatalog:
    """Validate upstream provenance and capture only this sample's known code files.

    Every existing valid source is read now. Later code() calls return captured text,
    so a new or changed file cannot silently enter an in-progress exploration.
    Missing/invalid retained source fails; unavailable support remains explicit.
    """
    preprocess_path, triage_path = Path(preprocess_path), Path(triage_path)
    pre_bytes, tri_bytes = preprocess_path.read_bytes(), triage_path.read_bytes()
    pre, tri = json.loads(pre_bytes), json.loads(tri_bytes)
    if tri.get('preprocess_ref', {}).get('sha256') != _digest(pre_bytes):
        raise ValueError("preprocess fingerprint does not match triage provenance")
    for key in ('name', 'sqlite_sha256', 'sample_sha256'):
        if key not in pre.get('sample', {}) or pre['sample'][key] != tri.get('sample', {}).get(key):
            raise ValueError(f"sample mismatch or missing identity: {key}")
    if not pre['sample']['sqlite_sha256'] and not pre['sample']['sample_sha256']:
        raise ValueError("sample identity has no source fingerprint")
    functions = _unique(pre['functions'], 'function')
    verdicts = _unique(tri['final_verdicts'], 'verdict')
    for verdict in verdicts.values():
        FinalVerdict.model_validate(verdict)
    undecided = {ref for ref, fn in functions.items() if fn['decision'] == 'UNDECIDED'}
    if set(verdicts) != undecided:
        raise ValueError(f"triage decisions must be complete and unique; missing={sorted(undecided - set(verdicts))}, unexpected={sorted(set(verdicts) - undecided)}")
    invalid = [ref for ref, fn in functions.items() if fn['decision'] not in {'PROGRAM_ANALYZE', 'PROGRAM_SKIP', 'UNDECIDED'}]
    if invalid:
        raise ValueError(f"unknown preprocess decisions: {invalid}")
    selected = {ref for ref, fn in functions.items() if fn['decision'] == 'PROGRAM_ANALYZE'} | {ref for ref, row in verdicts.items() if row['final_decision'] == 'ANALYZE'}
    if not selected:
        raise ValueError("no retained functions to prepare")
    edges = {ref: set() for ref in functions}
    for edge in pre['call_edges']:
        if not isinstance(edge, list) or len(edge) != 2 or any(ref not in functions for ref in edge):
            raise ValueError(f"invalid call edge or missing source function: {edge}")
        edges[edge[0]].add(edge[1])
    config = pre['config']
    max_functions, max_chars = config['max_functions_per_unit'], config['max_chars_per_unit']
    if type(max_functions) is not int or type(max_chars) is not int or min(max_functions, max_chars) <= 0:
        raise ValueError("packet limits must be positive integers")
    codes, metadata = {}, {}
    for ref, fn in functions.items():
        path = preprocess_path.parent / 'code' / f'{ref}.txt'
        entry = dict(fn, source_file=str(path.resolve()), selected=ref in selected,
                     is_entry=bool(fn.get('is_entry', False)), is_export=bool(fn.get('is_export', False)),
                     available=False, unavailable_reason=None, code_sha256=None,
                     callers=sorted((caller for caller in edges if ref in edges[caller]), key=_key),
                     callees=sorted(edges[ref], key=_key),
                     selection_verdict=verdicts.get(ref))
        try:
            data = path.read_bytes()
            entry['code_sha256'] = _digest(data)
            # Upstream's character length is measured before Windows LF->CRLF writes.
            code = data.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
            if not code.strip() or len(code) != fn['code_chars']:
                raise ValueError(f"code missing, incomplete or source length conflict: {ref}")
        except (OSError, UnicodeError, ValueError) as exc:
            if ref in selected:
                raise
            entry['unavailable_reason'] = f'{type(exc).__name__}: {exc}'
        else:
            codes[ref] = code
            entry.update(available=True, original_lines=len(code.splitlines()),
                         ends_with_newline=code.endswith(('\n', '\r')), code_chars=len(code))
        metadata[ref] = entry
    components = _components(selected, edges)
    return SourceCatalog(
        preprocess_path=preprocess_path, triage_path=triage_path,
        sample=pre['sample'], config=config, functions=metadata, selected_refs=selected,
        verdicts=verdicts, edges=edges, preprocess_sha256=_digest(pre_bytes), triage_sha256=_digest(tri_bytes),
        _codes=codes, _components_by_ref={ref: group for group in components for ref in group},
    )
