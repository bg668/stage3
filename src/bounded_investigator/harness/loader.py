from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import yaml

from ..investigation.revision import revision_policy_context

ROLES = ('analyzer', 'reviewer', 'reviser')
MAX_FILE_BYTES = 65_536
MAX_TOTAL_BYTES = 262_144
MAX_FILES = 64
_NAME = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*')
_SKILL_REFERENCE = re.compile(r'\{\{skill:([a-z0-9]+(?:-[a-z0-9]+)*)\}\}')


def builtin_harness_dir() -> Path:
    # Wheels are installed unpacked; resources stay inside the installed package.
    return Path(str(files('bounded_investigator').joinpath('harnesses', 'baseline-v1')))


@dataclass(frozen=True)
class ResolvedHarness:
    version: str
    fingerprint: str
    source: str
    _instructions: tuple[tuple[str, str], ...]
    _sources: tuple[tuple[str, str], ...]
    _trusted: tuple[tuple[str, str], ...]

    def instructions(self, role: str) -> str:
        if role not in ROLES:
            raise ValueError(f'Unsupported harness role: {role}')
        return dict(self._instructions)[role]

    def save(self, directory: Path) -> None:
        """Persist actual consumed policies, refusing mixed-harness reuse of a run."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / 'harness_snapshot.json'
        payload = dict(version=self.version, fingerprint=self.fingerprint, source=self.source,
                       instructions=dict(self._instructions), files=dict(self._sources),
                       trusted_rules=dict(self._trusted), skill_loading='deterministic_by_role')
        try:
            with target.open('x', encoding='utf-8') as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
        except FileExistsError:
            saved = json.loads(target.read_text(encoding='utf-8'))
            if saved != payload:
                raise ValueError(f'Output directory belongs to a different harness or was modified: {directory}')


def _markdown(text: str, path: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0] != '---':
        raise ValueError(f'{path}: YAML frontmatter is required')
    try:
        end = lines.index('---', 1)
        meta = yaml.safe_load('\n'.join(lines[1:end]))
    except (ValueError, yaml.YAMLError) as exc:
        raise ValueError(f'{path}: invalid frontmatter') from exc
    body = '\n'.join(lines[end + 1:]).strip()
    if not isinstance(meta, dict) or not body:
        raise ValueError(f'{path}: metadata and nonempty policy are required')
    return meta, body


def load_harness(directory: str | Path | None = None) -> ResolvedHarness:
    """Read and validate once; return immutable, role-specific instruction snapshots."""
    root = Path(directory) if directory is not None else builtin_harness_dir()
    if root.is_symlink() or (hasattr(root, 'is_junction') and root.is_junction()):
        raise ValueError('Harness root must not be a symlink or junction')
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f'Harness directory does not exist: {root}')
    sources: dict[str, str] = {}
    total = 0
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError(f'Harness links are not allowed: {path}')
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        allowed = rel in {'harness.yaml', 'systemprompt.md', *(f'agents/{r}.md' for r in ROLES)}
        parts = rel.split('/')
        skill = len(parts) == 3 and parts[0] == 'skills' and parts[2] == 'SKILL.md' and _NAME.fullmatch(parts[1])
        if not (allowed or skill):
            raise ValueError(f'Unsupported harness file: {rel}')
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f'Harness file exceeds size limit: {rel}')
        try:
            raw = path.read_bytes()
            text = raw.decode('utf-8')
        except (OSError, UnicodeError) as exc:
            raise ValueError(f'Cannot read UTF-8 harness file: {rel}') from exc
        total += len(raw)
        if len(raw) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(sources) >= MAX_FILES:
            raise ValueError('Harness exceeds file count or size limit')
        sources[rel] = text
    required = {'harness.yaml', 'systemprompt.md', *(f'agents/{r}.md' for r in ROLES)}
    if missing := required - sources.keys():
        raise ValueError(f'Missing harness files: {sorted(missing)}')
    try:
        config = yaml.safe_load(sources['harness.yaml'])
    except yaml.YAMLError as exc:
        raise ValueError('Invalid harness.yaml') from exc
    if (not isinstance(config, dict) or set(config) != {'format_version', 'version'}
            or type(config['format_version']) is not int or config['format_version'] != 1
            or not isinstance(config['version'], str) or not _NAME.fullmatch(config['version'])):
        raise ValueError('harness.yaml requires format_version: 1 and a lowercase version name')
    agents = {}
    skills: dict[str, dict[str, str]] = {r: {} for r in ROLES}
    skill_names: set[str] = set()
    if '{{skill:' in sources['systemprompt.md']:
        raise ValueError('Skill references are only supported in role files')
    for role in ROLES:
        meta, body = _markdown(sources[f'agents/{role}.md'], f'agents/{role}.md')
        if meta != {'name': role}:
            raise ValueError(f'agents/{role}.md: only name: {role} is supported')
        agents[role] = body
    for rel, text in sorted(sources.items()):
        if not rel.startswith('skills/'):
            continue
        meta, body = _markdown(text, rel)
        roles = meta.get('roles')
        if (set(meta) != {'name', 'description', 'roles'} or meta.get('name') != rel.split('/')[1]
                or not isinstance(meta.get('description'), str) or not meta['description'].strip()
                or not isinstance(roles, list) or not roles or any(r not in ROLES for r in roles)
                or len(roles) != len(set(roles))):
            raise ValueError(f'{rel}: requires matching name, description and unique supported roles')
        if '{{skill:' in body:
            raise ValueError(f'{rel}: Nested skill references are not supported')
        skill_names.add(meta['name'])
        for role in roles:
            skills[role][meta['name']] = body
    rules = files('bounded_investigator').joinpath('prompts', 'investigation_result.rules.md').read_text(encoding='utf-8').strip()
    revision = revision_policy_context()
    instructions = []
    for role in ROLES:
        used: set[str] = set()
        expanded = []
        for line in agents[role].split('\n'):
            if '{{skill:' not in line:
                expanded.append(line)
                continue
            match = _SKILL_REFERENCE.fullmatch(line)
            if match is None:
                raise ValueError(f'agents/{role}.md: skill reference must occupy a standalone line: {{{{skill:name}}}}')
            name = match.group(1)
            if name not in skill_names:
                raise ValueError(f'agents/{role}.md: Unknown skill: {name}')
            if name not in skills[role]:
                raise ValueError(f'agents/{role}.md: skill {name} is not assigned to {role}')
            if name in used:
                raise ValueError(f'agents/{role}.md: Duplicate skill reference: {name}')
            used.add(name)
            expanded.append(skills[role][name])
        # Preserve instruction order exactly; policies cannot replace trusted rules.
        remaining = [body for name, body in skills[role].items() if name not in used]
        parts = [sources['systemprompt.md'].strip(), '\n'.join(expanded), *remaining, rules]
        if role != 'analyzer':
            parts.append(revision)
        instructions.append((role, '\n\n'.join(p for p in parts if p)))
    trusted = (('investigation_result.rules.md', rules), ('revision_policy', revision))
    content = json.dumps([sorted(sources.items()), trusted], ensure_ascii=False, separators=(',', ':'))
    fingerprint = hashlib.sha256(content.encode('utf-8')).hexdigest()
    return ResolvedHarness(config['version'], fingerprint, str(root), tuple(instructions),
                           tuple(sorted(sources.items())), trusted)
