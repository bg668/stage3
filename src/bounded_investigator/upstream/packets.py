"""Offline, lossless Stage-1/2 to Stage-3 packet assembly.

Selection reasons are navigation hints. Only original source text is evidence.
No LLM, old Stage-3 plan or answer key participates in packet construction.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..orchestration.dag import DagManifest
from .catalog import SourceCatalog, load_catalog, _components, _digest, _key

SUPPORT_MAX_FUNCTIONS = 8
SUPPORT_MAX_CHARS = 12_000


def prepare_packets(preprocess_path: Path, triage_path: Path, output_dir: Path) -> dict:
    """Validate saved artifacts and write fresh numbered packets, manifest and map."""
    preprocess_path, triage_path, output_dir = map(Path, (preprocess_path, triage_path, output_dir))
    if output_dir.exists():
        raise FileExistsError(output_dir)
    catalog = load_catalog(preprocess_path, triage_path)
    pre = {'sample': catalog.sample}
    functions, verdicts = catalog.functions, catalog.verdicts
    selected, edges, config = catalog.selected_refs, catalog.edges, catalog.config
    codes, source_meta = {}, {}

    def load_code(ref: str) -> str:
        if ref in codes:
            return codes[ref]
        code = catalog.code(ref)
        codes[ref] = code
        fn = catalog.functions[ref]
        source_meta[ref] = {'source_file': fn['source_file'], 'code_sha256': fn['code_sha256'],
                            'address': fn['address'], 'symbol': fn['name'],
                            'original_lines': fn['original_lines'], 'ends_with_newline': fn['ends_with_newline'],
                            'code_chars': fn['code_chars'], 'occurrences': []}
        return code

    for ref in sorted(selected, key=_key):
        load_code(ref)
    max_functions, max_chars = config['max_functions_per_unit'], config['max_chars_per_unit']
    groups, current = [], []
    for component in _components(selected, edges):
        size = sum(len(codes[r]) for r in component)
        oversized = len(component) > max_functions or size > max_chars
        if current and (oversized or len(current) + len(component) > max_functions or sum(len(codes[r]) for r in current) + size > max_chars):
            groups.append((current, False))
            current = []
        if oversized:
            groups.append((component, True))
        else:
            current.extend(component)
    if current:
        groups.append((current, False))
    owners = {ref: f'J{i:02d}' for i, (refs, _) in enumerate(groups) for ref in refs}
    packets, rendered = [], {}
    for index, (refs, oversized) in enumerate(groups):
        job = f'J{index:02d}'
        support, omitted, support_chars = [], [], 0
        candidates = sorted({callee for ref in refs for callee in edges[ref]} - selected, key=_key)
        for ref in candidates:
            try:
                code = load_code(ref)
            except (OSError, UnicodeError, ValueError) as exc:
                omitted.append({'ref': ref, 'reason': f'source unavailable: {exc}'})
                continue
            if len(support) >= SUPPORT_MAX_FUNCTIONS or support_chars + len(code) > SUPPORT_MAX_CHARS:
                omitted.append({'ref': ref, 'reason': 'support count or character limit; whole source omitted'})
                continue
            support.append(ref)
            support_chars += len(code)
        deps = sorted({owners[callee] for ref in refs for callee in edges[ref] & selected if owners[callee] != job})
        packet = {'job_id': job, 'primary_refs': refs, 'support_refs': support, 'depends_on': deps, 'primary_code_chars': sum(len(codes[r]) for r in refs), 'support_code_chars': support_chars, 'oversized_component': oversized, 'material_boundaries': omitted}
        packets.append(packet)
        lines = [f'# {job} 自动生成的函数材料包', '', '只分析主体函数；支持函数提供直接调用上下文，不产生新主体任务。', '上游筛选理由仅作导航，不是行为证据。依赖表示分析上下文，不表示代码执行顺序。', '', f"主体函数：{', '.join(refs)}", f"支持函数：{', '.join(support) or '无'}", f"分析依赖：{', '.join(deps) or '无'}", f'不可拆分超限分量：{oversized}', '']
        for role, member_refs in (('primary', refs), ('support', support)):
            for ref in member_refs:
                fn, code = functions[ref], codes[ref]
                code_lines = code.splitlines()
                symbol = fn['name'] if re.match(r'^[A-Za-z_]', fn['name']) else 'sub_' + fn['address'].removeprefix('0x')
                lines += [f"## {ref} {symbol}，原文件 L0001–L{len(code_lines):04d} / 共 {len(code_lines)} 行", '', f"角色：{role}；地址：{fn['address']}；原始符号：{fn['name']}", f"来源文件：{source_meta[ref]['source_file']}", f"源码 SHA256：{source_meta[ref]['code_sha256']}", f"筛选导航：{json.dumps(verdicts.get(ref, {'decision': fn['decision'], 'reason': fn.get('reason', '')}), ensure_ascii=False)}", '']
                # Numbered source lines cannot close a fence; keep the evidence
                # parser's three-backtick contract even when code contains ```.
                fence = '```'
                lines.append(fence + 'c')
                first_line = len(lines) + 1
                lines.extend(f'L{i:04d}: {line}' for i, line in enumerate(code_lines, 1))
                source_meta[ref]['occurrences'].append({'packet_id': job, 'role': role, 'packet_file': f'packets/{job}.md', 'packet_start_line': first_line, 'packet_end_line': len(lines), 'original_start_line': 1, 'original_end_line': len(code_lines)})
                lines += [fence, '']
        lines += ['## 材料边界', '', json.dumps(omitted, ensure_ascii=False, indent=2), '']
        rendered[job] = '\n'.join(lines)
    manifest = {'purpose': '自动筛选结果到阶段三；依赖为分析依赖，非代码执行顺序', 'packet_count': len(packets), 'jobs': [{'job_id': p['job_id'], 'title': f"自动材料 {p['job_id']}", 'depends_on': p['depends_on'], 'source_chars': p['primary_code_chars'] + p['support_code_chars'], 'source_count': len(p['primary_refs']) + len(p['support_refs'])} for p in packets]}
    DagManifest.model_validate(manifest)
    primary_occurrences = [r for packet in packets for r in packet['primary_refs']]
    if len(primary_occurrences) != len(selected) or set(primary_occurrences) != selected:
        raise ValueError("retained function coverage is not exactly once")
    support_occurrences = [r for packet in packets for r in packet['support_refs']]
    mapping = {'schema_version': '1', 'sample': pre['sample'], 'preprocess_sha256': catalog.preprocess_sha256, 'triage_sha256': catalog.triage_sha256, 'packer': {'algorithm': 'scc-callee-first-v1', 'max_functions_per_unit': max_functions, 'max_chars_per_unit': max_chars, 'support_max_functions': SUPPORT_MAX_FUNCTIONS, 'support_max_chars': SUPPORT_MAX_CHARS}, 'selected_count': len(selected), 'selected_refs': sorted(selected, key=_key), 'support_unique_count': len(set(support_occurrences)), 'support_occurrence_count': len(support_occurrences), 'support_total_chars': sum(len(codes[r]) for r in support_occurrences), 'support_unique_chars': sum(len(codes[r]) for r in set(support_occurrences)), 'support_repeated_chars': sum(len(codes[r]) for r in support_occurrences) - sum(len(codes[r]) for r in set(support_occurrences)), 'packets': packets, 'functions': {r: metadata for r, metadata in source_meta.items() if metadata['occurrences']}}
    # All validation and construction finishes before creating the output directory.
    (output_dir / 'packets').mkdir(parents=True, exist_ok=False)
    for job, text in rendered.items():
        (output_dir / 'packets' / f'{job}.md').write_text(text, encoding='utf-8', newline='\n')
    for name, payload in (('manifest.json', manifest), ('material_mapping.json', mapping)):
        (output_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return mapping


def prepare_task_packet(
    catalog: SourceCatalog,
    *,
    task_id: str,
    primary_refs: list[str],
    support_refs: list[str],
    question: str,
    depth: str,
    output_dir: Path,
) -> dict:
    """Build one bounded question packet, without static package prerequisites.

    Primary material is one retained function/SCC. Support is an explicit ordered
    request, never an automatic expansion of the call graph. The orchestration
    layer owns question/evidence-state deduplication and revisit authorization.
    """
    if not re.fullmatch(r'J\d{4,}', task_id):
        raise ValueError('task_id must use the J0000 numbering contract')
    if not question.strip() or not depth.strip():
        raise ValueError('task question and depth must be explicit')
    output_dir = Path(output_dir)
    path, mapping_path = output_dir / f'{task_id}.md', output_dir / f'{task_id}.mapping.json'
    aggregate_path = output_dir / 'material_mapping.json'
    if path.exists() or mapping_path.exists():
        raise FileExistsError(path)
    requested_primary = [catalog.resolve(ref) for ref in primary_refs]
    if not requested_primary:
        raise ValueError('task requires a retained primary function')
    if any(ref not in catalog.selected_refs for ref in requested_primary):
        raise ValueError('primary functions must belong to the retained candidate set')
    primary = catalog.components(requested_primary[0])
    if any(ref not in primary for ref in requested_primary):
        raise ValueError('one task may contain only one primary recursive component')
    primary_chars = sum(len(catalog.code(ref)) for ref in primary)
    oversized = primary_chars > catalog.config['max_chars_per_unit'] or len(primary) > catalog.config['max_functions_per_unit']
    requested_support = list(dict.fromkeys(catalog.resolve(ref) for ref in support_refs))
    support, boundaries, support_chars = [], [], 0
    for ref in requested_support:
        if ref in primary:
            continue
        fn = catalog.functions[ref]
        if not fn['available']:
            boundaries.append({'ref': ref, 'reason': 'source unavailable', 'detail': fn['unavailable_reason']})
            continue
        code = catalog.code(ref)
        if len(support) >= SUPPORT_MAX_FUNCTIONS or support_chars + len(code) > SUPPORT_MAX_CHARS:
            boundaries.append({'ref': ref, 'reason': 'support count or character limit; whole source omitted'})
            continue
        support.append(ref)
        support_chars += len(code)
    # Model-derived task text is navigation, never a source section. JSON strings
    # keep embedded headings/fences on one physical line; originals stay in mapping.
    lines = [f'# {task_id} 有界函数探索任务', '', f'问题：{json.dumps(question, ensure_ascii=False)}', f'深度：{json.dumps(depth, ensure_ascii=False)}', '',
             '分析、审阅和修订均限于本任务问题及所供给源码。跨函数未知保持未知，不要求先分析整个调用链。',
             '主体函数完整供给；支持函数只作为问题上下文。候选身份、材料存在或规划器选择不构成恶意性证据。',
             f"主体函数：{', '.join(primary)}", f"支持函数：{', '.join(support) or '无'}", '']
    lines += ['### 主体函数直接关联（仅导航，未附正文不代表已调查）', '']
    for ref in primary:
        fn = catalog.functions[ref]
        lines += [f"- {ref} 调用：{', '.join(fn['callees']) or '无'}；被调用于：{', '.join(fn['callers']) or '无'}"]
    lines.append('')
    functions = {}
    for role, refs in (('primary', primary), ('support', support)):
        for ref in refs:
            fn, code = catalog.functions[ref], catalog.code(ref)
            code_lines = code.splitlines()
            symbol = fn['name'] if re.match(r'^[A-Za-z_]', fn['name']) else 'sub_' + fn['address'].removeprefix('0x')
            lines += [f"## {ref} {symbol}，原文件 L0001–L{len(code_lines):04d} / 共 {len(code_lines)} 行", '',
                      f"角色：{role}；地址：{fn['address']}；原始符号：{fn['name']}",
                      f"来源：{ref}.txt；SHA256：{fn['code_sha256']}", '```c']
            first_line = len(lines) + 1
            lines.extend(f'L{i:04d}: {line}' for i, line in enumerate(code_lines, 1))
            occurrence = {'packet_id': task_id, 'role': role, 'packet_file': f'{task_id}.md',
                          'packet_start_line': first_line, 'packet_end_line': len(lines),
                          'original_start_line': 1, 'original_end_line': len(code_lines)}
            functions[ref] = dict(fn, symbol=fn['name'], occurrences=[occurrence])
            lines += ['```', '']
    # Long source paths and complete upstream decisions stay in the sidecar.
    lines += ['### 本任务材料边界', '']
    lines.extend(f"- {row['ref']}: {row['reason']}" for row in boundaries)
    if not boundaries:
        lines.append('- 本次明确请求的支持材料均已提供。该状态不等同于业务问题已解决。')
    text = '\n'.join(lines) + '\n'
    mapping = {
        'job_id': task_id, 'question': question, 'depth': depth,
        'primary_refs': primary, 'support_refs': support,
        'requested_primary_refs': requested_primary, 'requested_support_refs': requested_support,
        'selected_support_refs': [r for r in support if r in catalog.selected_refs],
        'nonselected_support_refs': [r for r in support if r not in catalog.selected_refs],
        'primary_code_chars': primary_chars, 'support_code_chars': support_chars,
        'oversized_component': oversized, 'material_boundaries': boundaries,
        'depends_on': [], 'input_sha256': _digest(text.encode('utf-8')), 'functions': functions,
        'preprocess_sha256': catalog.preprocess_sha256, 'triage_sha256': catalog.triage_sha256,
    }
    if aggregate_path.exists():
        aggregate = json.loads(aggregate_path.read_text(encoding='utf-8'))
        if aggregate.get('preprocess_sha256') != catalog.preprocess_sha256 or aggregate.get('triage_sha256') != catalog.triage_sha256:
            raise ValueError('task mapping cannot mix source catalogs')
        if any(packet['job_id'] == task_id for packet in aggregate['packets']):
            raise FileExistsError(f'task already mapped: {task_id}')
    else:
        aggregate = {'schema_version': '1', 'mode': 'exploration', 'selected_count': len(catalog.selected_refs),
                     'selected_refs': sorted(catalog.selected_refs, key=_key),
                     'preprocess_sha256': catalog.preprocess_sha256, 'triage_sha256': catalog.triage_sha256,
                     'packets': [], 'functions': {}}
    aggregate['packets'].append({key: value for key, value in mapping.items() if key != 'functions'})
    for ref, fn in functions.items():
        if ref not in aggregate['functions']:
            aggregate['functions'][ref] = fn
        else:
            if aggregate['functions'][ref]['code_sha256'] != fn['code_sha256']:
                raise ValueError(f'conflicting source fingerprint for {ref}')
            aggregate['functions'][ref]['occurrences'].extend(fn['occurrences'])
    output_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8', newline='\n')
    mapping_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    aggregate_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'text': text, 'path': path, 'mapping': mapping, 'primary_refs': primary,
            'support_refs': support, 'boundaries': boundaries}
