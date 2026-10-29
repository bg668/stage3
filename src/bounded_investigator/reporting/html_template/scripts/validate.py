#!/usr/bin/env python3
"""校验恶意样本分析报告数据（report.json）。

用法:
    python validate.py report.json [--legacy]

--legacy：1.1 新增的一致性错误（IOC 重复、gaps 与正文不一致）降级为警告，只用于重渲染存档的旧报告。

只依赖 Python 标准库。发现错误时退出码为 1；警告不影响退出码。
"""
import json
import re
import sys
from pathlib import Path

RISK_LEVELS = ("critical", "high", "medium", "low", "info")
CALLOUT_TONES = ("info", "risk", "boundary", "warning")
TOP_LEVEL_KEYS = {
    "schema_version", "meta", "hashes", "summary", "risk_judgement", "verdict", "scope",
    "chain", "findings", "sections", "ioc", "attack", "detection", "response", "attribution",
    "attribution_empty_reason", "gaps", "functions", "yara", "final",
}
HASH_LEN = {"MD5": 32, "SHA1": 40, "SHA256": 64}

# ---- 1.1 新增 ----
RULES_PATH = Path(__file__).resolve().parent.parent / "assets" / "tier_rules.json"
TIERS = ("malicious", "suspicious", "info")
FINDING_STATES = ("confirmed", "partial")
AUDIT_STATES = ("passed", "incomplete", "not_run")
IOC_SPECIFICITY = ("high", "medium", "context")
# 证据引用的约定写法：上线前改成第三阶段实际输出的格式，只保留一种为宜
EVIDENCE_PATTERNS = (
    re.compile(r"F-\d{4,5} L\d{1,6}(?:-L\d{1,6})?"),          # 函数编号 + 4 位行号区间
    re.compile(r"sub_[0-9A-Fa-f]{6,}( L\d{4}-L\d{4})?"),  # 函数名（可带行号）
)
# 正文出现这些词，说明还有没闭合的问题，应在 gaps 里列出
HEDGE_WORDS = ("未闭合", "未解析", "尚未", "不确定", "不能确认", "未能确认", "未证明", "没有证明", "未恢复", "未知")
FINDING_TITLE_MAX = 32  # 显示宽度（中文字 = 1）
CHAIN_BRIEF_MAX = 12
STRUCTURED_BLOCKS = {"table", "kv", "cards", "steps", "list"}
_ENGLISH_RUN = re.compile(r"(?:[A-Za-z][A-Za-z'’-]*[,;]?\s+){5,}[A-Za-z]")
_CODE_SPAN = re.compile(r"`[^`]*`")

# 每种积木：必填字段 -> 期望类型
BLOCK_SPECS = {
    "paragraph": {"text": str},
    "subheading": {"text": str},
    "callout": {"text": str},
    "table": {"columns": list, "rows": list},
    "kv": {"items": list},
    "cards": {"items": list},
    "list": {"items": list},
    "steps": {"items": list},
    "code": {"text": str},
    "tags": {"items": list},
    "empty": {"label": str, "text": str},
}


def width(s):
    """按显示宽度计长度：中日韩字符记 1，其余记 0.55，避免中英混排的标题被误判超长。"""
    return round(sum(1 if ord(ch) >= 0x2E80 else 0.55 for ch in s))


def load_rules(path=RULES_PATH):
    """读取档位规则表，返回 (rules_by_id, categories)。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {r["id"]: r for r in data["rules"]}, data["categories"]


def resolve_finding(f, rules):
    """返回 (tier, category)：普通规则取规则表的值，CUSTOM 取数据里的值。"""
    rule = rules.get(f.get("rule"))
    if rule:
        return rule["tier"], rule["category"]
    return f.get("tier"), f.get("category")


def iter_text(d):
    """遍历报告里所有给人读的文本，产出 (路径, 文本)。"""
    for i, s in enumerate(d.get("summary") or []):
        yield f"summary[{i}]", s
    yield "risk_judgement", d.get("risk_judgement")
    yield "meta.verdict", (d.get("meta") or {}).get("verdict")
    yield "final", d.get("final")
    for i, s in enumerate(d.get("chain") or []):
        if isinstance(s, dict):
            yield f"chain[{i}].action", s.get("action")
    for i, f in enumerate(d.get("findings") or []):
        if isinstance(f, dict):
            yield f"findings[{i}].title", f.get("title")
            yield f"findings[{i}].state_note", f.get("state_note")
    for i, sec in enumerate(d.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        for j, b in enumerate(sec.get("blocks") or []):
            if not isinstance(b, dict):
                continue
            p = f"sections[{i}].blocks[{j}]"
            if b.get("type") != "code":
                yield p, b.get("text")
            yield p, b.get("note")
            for r in b.get("rows") or []:
                for c in r if isinstance(r, list) else []:
                    yield p, c
            for it in b.get("items") or []:
                if isinstance(it, str):
                    yield p, it
                elif isinstance(it, dict):
                    for v in it.values():
                        yield p, v
    for kind in ("host", "network"):
        for i, it in enumerate((d.get("ioc") or {}).get(kind) or []):
            if isinstance(it, dict):
                yield f"ioc.{kind}[{i}].note", it.get("note")


class Checker:
    def __init__(self, rules=None, categories=None, legacy=False):
        self.errors = []
        self.warnings = []
        self.legacy = legacy  # True 时 1.1 新增的一致性错误降级为警告，用于重渲染旧报告
        if rules is None:
            rules, categories = load_rules()
        self.rules, self.categories = rules, categories

    def err(self, path, msg):
        self.errors.append(f"{path}: {msg}")

    def warn(self, path, msg):
        self.warnings.append(f"{path}: {msg}")

    def err11(self, path, msg):
        """1.1 新增的一致性错误；legacy 模式下只作警告。"""
        if self.legacy:
            self.warn(path, msg + "（legacy 模式降级为警告）")
        else:
            self.err(path, msg)

    # ---- 基础检查 ----
    def text(self, obj, key, path, required=True):
        v = obj.get(key) if isinstance(obj, dict) else None
        if v is None:
            if required:
                self.err(f"{path}.{key}", "缺少必填文本")
            return None
        if not isinstance(v, str) or not v.strip():
            self.err(f"{path}.{key}", "应为非空字符串")
            return None
        return v

    def array(self, obj, key, path, required=True, min_len=0):
        v = obj.get(key) if isinstance(obj, dict) else None
        if v is None:
            if required:
                self.err(f"{path}.{key}", "缺少必填数组")
            return []
        if not isinstance(v, list):
            self.err(f"{path}.{key}", "应为数组")
            return []
        if len(v) < min_len:
            self.err(f"{path}.{key}", f"至少需要 {min_len} 项")
        return v

    def objects(self, items, path, fields, optional=()):
        """items 中每项必须是对象，fields 为必填文本字段，optional 为可选文本字段。"""
        for i, it in enumerate(items):
            p = f"{path}[{i}]"
            if not isinstance(it, dict):
                self.err(p, "应为对象")
                continue
            for f in fields:
                self.text(it, f, p)
            for f in optional:
                if f in it and it[f] is not None and not isinstance(it[f], str):
                    self.err(f"{p}.{f}", "应为字符串")

    def evidence(self, refs, path):
        """检查证据引用：类型、格式、重复。"""
        if not isinstance(refs, list) or not all(isinstance(e, str) for e in refs):
            self.err(path, "应为字符串数组")
            return
        bad = [e for e in refs if not any(p.fullmatch(e) for p in EVIDENCE_PATTERNS)]
        if bad:
            shown = "、".join(bad[:4]) + (f" 等 {len(bad)} 条" if len(bad) > 4 else "")
            self.warn(path, f"不符合约定的证据引用格式（EVIDENCE_PATTERNS）：{shown}")
        dup = sorted({e for e in refs if refs.count(e) > 1})
        if dup:
            self.warn(path, f"重复的证据引用：{'、'.join(dup[:4])}")

    # ---- 积木 ----
    def blocks(self, blocks, path):
        for i, b in enumerate(blocks):
            p = f"{path}[{i}]"
            if not isinstance(b, dict):
                self.err(p, "积木应为对象")
                continue
            t = b.get("type")
            if t not in BLOCK_SPECS:
                self.err(f"{p}.type", f"未知积木类型 {t!r}，可选: {', '.join(BLOCK_SPECS)}")
                continue
            for field, typ in BLOCK_SPECS[t].items():
                v = b.get(field)
                if not isinstance(v, typ) or (typ is str and not v.strip()):
                    self.err(f"{p}.{field}", f"{t} 积木缺少或类型错误（应为 {typ.__name__}）")
            if t == "callout" and b.get("tone", "info") not in CALLOUT_TONES:
                self.err(f"{p}.tone", f"可选值: {', '.join(CALLOUT_TONES)}")
            if t == "table" and isinstance(b.get("columns"), list) and isinstance(b.get("rows"), list):
                n = len(b["columns"])
                if n == 0:
                    self.err(f"{p}.columns", "至少需要 1 列")
                if not b["rows"]:
                    self.warn(f"{p}.rows", "空表格；考虑改用 empty 积木说明原因")
                for r, row in enumerate(b["rows"]):
                    if not isinstance(row, list):
                        self.err(f"{p}.rows[{r}]", "每行应为数组")
                    elif len(row) != n:
                        self.err(f"{p}.rows[{r}]", f"有 {len(row)} 格，与 {n} 列不符")
                for c in b.get("mono_columns", []) or []:
                    if not isinstance(c, int) or not 0 <= c < n:
                        self.err(f"{p}.mono_columns", f"列号 {c!r} 越界（0..{n - 1}）")
            if t == "kv":
                self.objects(b["items"], f"{p}.items", ("k", "v"))
            if t == "cards":
                self.objects(b["items"], f"{p}.items", ("title", "text"), ("meta",))
            if t == "steps":
                self.objects(b["items"], f"{p}.items", ("title", "text"))
            if t in ("list", "tags"):
                for j, s in enumerate(b["items"]):
                    if not isinstance(s, str) or not s.strip():
                        self.err(f"{p}.items[{j}]", "应为非空字符串")
            if t in ("kv", "cards", "list", "steps", "tags") and isinstance(b.get("items"), list) and not b["items"]:
                self.err(f"{p}.items", "不能为空；没有内容时用 empty 积木")

    # ---- 1.1：关键发现 ----
    def findings(self, d, section_ids):
        try:
            version = tuple(int(x) for x in str(d.get("schema_version", "1.0")).split("."))
        except ValueError:
            version = (1, 0)
        if "findings" not in d:
            if version >= (1, 1):
                self.err("findings", "schema 1.1 必须提供 findings（可为空数组）")
            else:
                self.warn("findings", "旧版数据没有 findings，首屏不会显示关键发现")
            return []
        items = self.array(d, "findings", "$")
        if not items:
            self.warn("findings", "没有任何关键发现；确认样本确实没有可列出的能力")
        titles = set()
        for i, f in enumerate(items):
            p = f"findings[{i}]"
            if not isinstance(f, dict):
                self.err(p, "应为对象")
                continue
            rule = f.get("rule")
            if rule == "CUSTOM":
                if f.get("tier") not in TIERS:
                    self.err(f"{p}.tier", f"CUSTOM 规则必须给出 tier：{', '.join(TIERS)}")
                if f.get("category") not in self.categories:
                    self.err(f"{p}.category", f"CUSTOM 规则必须给出 category：{', '.join(self.categories)}")
                if self.text(f, "tier_reason", p):
                    self.warn(p, "使用了自定义规则，需人工复核档位，并考虑补进 tier_rules.json")
            elif rule not in self.rules:
                self.err(f"{p}.rule", f"未知规则 {rule!r}；从 assets/tier_rules.json 选择，或用 CUSTOM")
            else:
                for k in ("tier", "category"):
                    if k in f:
                        self.err(f"{p}.{k}", "档位和类别由规则表决定，不能在数据中改写；确需例外请用 CUSTOM")
            title = self.text(f, "title", p)
            if title:
                if width(title) > FINDING_TITLE_MAX:
                    self.warn(f"{p}.title", f"标题约 {width(title)} 字宽，超过 {FINDING_TITLE_MAX}；首屏只放一句话，细节放章节")
                if title in titles:
                    self.warn(f"{p}.title", "与另一条发现重复")
                titles.add(title)
            state = f.get("state")
            if state not in FINDING_STATES:
                self.err(f"{p}.state", f"可选值: {', '.join(FINDING_STATES)}")
            if state == "partial":
                self.text(f, "state_note", p)
            ev = f.get("evidence", [])
            self.evidence(ev, f"{p}.evidence")
            if isinstance(ev, list) and not ev:
                self.warn(f"{p}.evidence", "没有证据引用；每条发现都应能追溯到代码位置")
            sec = f.get("section")
            if sec is not None and sec not in section_ids:
                self.err(f"{p}.section", f"指向不存在的章节 id {sec!r}；可选: {', '.join(sorted(section_ids)) or '（无自定义章节 id）'}")
        return items

    # ---- 主体 ----
    def run(self, d):
        if not isinstance(d, dict):
            self.err("$", "顶层应为对象")
            return
        for k in d:
            if k not in TOP_LEVEL_KEYS:
                self.warn(k, "未知顶层字段，渲染时会被忽略；自定义内容请放进 sections")

        meta = d.get("meta")
        if not isinstance(meta, dict):
            self.err("meta", "缺少 meta 对象")
            meta = {}
        for f in ("id", "title", "date", "verdict"):
            self.text(meta, f, "meta")
        if meta.get("risk") not in RISK_LEVELS:
            self.err("meta.risk", f"可选值: {', '.join(RISK_LEVELS)}")
        if not meta.get("method"):
            if str(d.get("schema_version", "1.0")) == "1.0":
                self.warn("meta.method", "未说明分析方式；读者需要知道结论来自静态还是动态分析")
            else:
                self.err("meta.method", "必须说明分析方式（如基于提供的源码与结构化结果的静态分析）")
        elif not isinstance(meta["method"], str) or not meta["method"].strip():
            self.err("meta.method", "应为非空字符串")
        sample = meta.get("sample")
        if sample is not None:
            if not isinstance(sample, dict):
                self.err("meta.sample", "应为对象")
            else:
                for k, v in sample.items():
                    if not isinstance(v, str):
                        self.err(f"meta.sample.{k}", "应为字符串")
        audit = meta.get("audit")
        if audit is None:
            self.warn("meta.audit", "未提供证据审计状态，报告头将显示“证据未审计”")
        elif not isinstance(audit, dict):
            self.err("meta.audit", "应为对象")
        else:
            if audit.get("status") not in AUDIT_STATES:
                self.err("meta.audit.status", f"可选值: {', '.join(AUDIT_STATES)}")
            for k in ("refs_total", "refs_invalid"):
                if k in audit and not (isinstance(audit[k], int) and audit[k] >= 0):
                    self.err(f"meta.audit.{k}", "应为非负整数")
            if audit.get("status") == "incomplete":
                self.warn("meta.audit", "证据审计未通过，报告头会显示警示；正式引用前需处理失效引用")

        hashes = self.array(d, "hashes", "$")
        self.objects(hashes, "hashes", ("type", "value"), ("source",))
        for i, h in enumerate(hashes):
            if isinstance(h, dict) and isinstance(h.get("value"), str):
                exp = HASH_LEN.get(str(h.get("type", "")).upper())
                if exp and not re.fullmatch(rf"[0-9a-fA-F]{{{exp}}}", h["value"]):
                    self.warn(f"hashes[{i}].value", f"不像合法的 {h['type']}（应为 {exp} 位十六进制）")

        for i, s in enumerate(self.array(d, "summary", "$", min_len=1)):
            if not isinstance(s, str) or not s.strip():
                self.err(f"summary[{i}]", "应为非空字符串")
        self.text(d, "risk_judgement", "$")

        if "verdict" in d:
            self.objects(self.array(d, "verdict", "$"), "verdict", ("dimension", "conclusion"), ("confidence",))

        scope = d.get("scope")
        if scope is not None:
            if not isinstance(scope, dict):
                self.err("scope", "应为对象")
            else:
                self.objects(self.array(scope, "coverage", "scope", required=False), "scope.coverage", ("label", "value"), ("note",))
                self.text(scope, "inputs", "scope", required=False)
                self.text(scope, "not_covered", "scope", required=False)

        chain = self.array(d, "chain", "$")
        for i, s in enumerate(chain):
            p = f"chain[{i}]"
            if not isinstance(s, dict):
                self.err(p, "应为对象")
                continue
            self.text(s, "name", p)
            self.text(s, "action", p)
            brief = self.text(s, "brief", p, required=False)
            if brief and width(brief) > CHAIN_BRIEF_MAX:
                self.warn(f"{p}.brief", f"约 {width(brief)} 字宽，超过 {CHAIN_BRIEF_MAX}，会在行为链节点下换行")
            ev = s.get("evidence", [])
            self.evidence(ev, f"{p}.evidence")
            if isinstance(ev, list) and not ev:
                self.warn(f"{p}.evidence", "没有证据引用；每一步都应能追溯到代码位置")
            if "key" in s and not isinstance(s["key"], bool):
                self.err(f"{p}.key", "应为 true/false")
            if "phase" in s and s["phase"] is not None and not isinstance(s["phase"], str):
                self.err(f"{p}.phase", "应为字符串")
        if not chain:
            self.warn("chain", "行为链为空，将显示“未建立行为链”空状态")
        elif meta.get("risk") in ("critical", "high") and not any(isinstance(s, dict) and s.get("key") for s in chain):
            self.warn("chain", "风险为 high/critical，但没有任何一步标为 key；行为链没有重点")

        seen = set()
        for i, sec in enumerate(self.array(d, "sections", "$", required=False)):
            p = f"sections[{i}]"
            if not isinstance(sec, dict):
                self.err(p, "应为对象")
                continue
            self.text(sec, "title", p)
            sid = sec.get("id")
            if sid is not None:
                if not isinstance(sid, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", sid):
                    self.err(f"{p}.id", "应为小写字母开头的 slug，如 credential-theft")
                elif sid in seen:
                    self.err(f"{p}.id", f"重复的 id {sid!r}")
                else:
                    seen.add(sid)
            blocks = self.array(sec, "blocks", p, min_len=1)
            self.blocks(blocks, f"{p}.blocks")
            kinds = [b.get("type") for b in blocks if isinstance(b, dict)]
            if kinds.count("paragraph") >= 4 and not STRUCTURED_BLOCKS & set(kinds):
                self.warn(p, f"{kinds.count('paragraph')} 段文字、没有结构化积木；3 个以上同构对象请用 table，先给 callout 结论")

        findings = self.findings(d, seen)

        ioc = d.get("ioc")
        if not isinstance(ioc, dict):
            self.err("ioc", "缺少 ioc 对象（没有指标也要给出 host/network 空数组和原因）")
        else:
            values, no_note, no_spec, total = {}, 0, 0, 0
            for kind in ("host", "network"):
                items = self.array(ioc, kind, "ioc")
                self.objects(items, f"ioc.{kind}", ("type", "value"), ("note", "role"))
                if not items and not (isinstance(ioc.get(f"{kind}_empty_reason"), str) and ioc[f"{kind}_empty_reason"].strip()):
                    self.err(f"ioc.{kind}_empty_reason", f"{kind} 为空时必须说明原因")
                for i, it in enumerate(items):
                    if not isinstance(it, dict):
                        continue
                    p = f"ioc.{kind}[{i}]"
                    total += 1
                    key = str(it.get("value", "")).strip().lower()
                    if key in values:
                        self.err11(p, f"与 {values[key]} 的值重复（{it.get('value')}）；同一指标只列一次")
                    else:
                        values[key] = p
                    if not str(it.get("note") or "").strip():
                        no_note += 1
                    spec = it.get("specificity")
                    if spec is None:
                        no_spec += 1
                    elif spec not in IOC_SPECIFICITY:
                        self.err(f"{p}.specificity", f"可选值: {', '.join(IOC_SPECIFICITY)}")
                    if "evidence" in it:
                        self.evidence(it["evidence"], f"{p}.evidence")
            if total and no_spec:
                self.warn("ioc", f"{no_spec}/{total} 条没有 specificity（high/medium/context），无法区分 IOC、检测特征和上下文对象")
            if total and no_note * 2 > total:
                self.warn("ioc", f"{no_note}/{total} 条没有说明；读者无法判断每个指标的用途")

        for i, a in enumerate(self.array(d, "attack", "$", required=False)):
            p = f"attack[{i}]"
            if not isinstance(a, dict):
                self.err(p, "应为对象")
                continue
            for f in ("id", "name", "evidence"):
                self.text(a, f, p)
            if isinstance(a.get("id"), str) and not re.fullmatch(r"T\d{4}(\.\d{3})?", a["id"]):
                self.warn(f"{p}.id", "不像 ATT&CK 技术编号（Txxxx 或 Txxxx.xxx）")

        self.blocks(self.array(d, "detection", "$", min_len=1), "detection")
        self.objects(self.array(d, "response", "$", min_len=1), "response", ("title", "text"))

        attrib = d.get("attribution")
        if attrib is None:
            r = d.get("attribution_empty_reason")
            if not (isinstance(r, str) and r.strip()):
                self.err("attribution_empty_reason", "未归因时必须说明原因")
        elif not isinstance(attrib, dict):
            self.err("attribution", "应为对象或 null")
        else:
            self.text(attrib, "family", "attribution")
            self.text(attrib, "confidence", "attribution")
            for f in ("support", "not_confirmed"):
                for j, s in enumerate(self.array(attrib, f, "attribution", required=(f == "support"))):
                    if not isinstance(s, str) or not s.strip():
                        self.err(f"attribution.{f}[{j}]", "应为非空字符串")
            refs = self.array(attrib, "refs", "attribution", required=False)
            self.objects(refs, "attribution.refs", ("title", "url"))
            for j, r in enumerate(refs):
                if isinstance(r, dict) and isinstance(r.get("url"), str) and not r["url"].startswith(("http://", "https://")):
                    self.err(f"attribution.refs[{j}].url", "应以 http:// 或 https:// 开头")

        gaps = self.array(d, "gaps", "$")
        self.objects(gaps, "gaps", ("gap", "impact"))
        for i, g in enumerate(gaps):
            if isinstance(g, dict) and not isinstance(g.get("blocking"), bool):
                self.err(f"gaps[{i}].blocking", "应为 true/false")

        self.objects(self.array(d, "functions", "$", required=False), "functions", ("function", "role"), ("address", "lines"))

        if "yara" in d and d["yara"] is not None and not isinstance(d["yara"], str):
            self.err("yara", "应为字符串")
        self.text(d, "final", "$")

        self.consistency(d, findings, gaps)

    # ---- 1.1：跨字段一致性 ----
    def consistency(self, d, findings, gaps):
        risk = (d.get("meta") or {}).get("risk")
        tiers = [resolve_finding(f, self.rules)[0] for f in findings if isinstance(f, dict)]
        if risk in ("critical", "high") and findings and "malicious" not in tiers:
            self.warn("findings", f"风险为 {risk}，但没有任何“恶意”档发现；核对风险等级或规则选择")
        if risk in ("low", "info") and "malicious" in tiers:
            self.warn("meta.risk", f"存在“恶意”档发现，但风险为 {risk}")

        # 正文的“未闭合”类表述和 gaps 是否一致
        hits, where = 0, set()
        english = []
        for path, s in iter_text(d):
            if not isinstance(s, str):
                continue
            n = sum(s.count(w) for w in HEDGE_WORDS)
            if n:
                hits += n
                where.add(path.split(".")[0].split("[")[0])
            plain = _CODE_SPAN.sub("", s)
            if _ENGLISH_RUN.search(plain):
                english.append(path)
        partial = sum(1 for f in findings if isinstance(f, dict) and f.get("state") == "partial")
        if not gaps:
            if partial:
                self.err11("gaps", f"有 {partial} 条“部分证实”的发现，但 gaps 为空；把未闭合的部分写进 gaps")
            elif hits >= 3:
                self.err11("gaps", f"正文有 {hits} 处“未闭合 / 未解析 / 尚未”等表述（{'、'.join(sorted(where))}），但 gaps 为空")
            elif hits:
                self.warn("gaps", f"正文有 {hits} 处未闭合类表述，但 gaps 为空；确认是否遗漏缺口")
        if english:
            uniq = list(dict.fromkeys(english))
            self.warn("language", f"{len(uniq)} 个位置出现整句英文（如 {uniq[0]}）；报告应统一为中文，标识符用反引号")


def validate(data, rules_path=None, legacy=False):
    """返回 (errors, warnings) 两个字符串列表。legacy=True 时 1.1 新增的一致性错误降级为警告。"""
    rules, categories = load_rules(rules_path) if rules_path else load_rules()
    c = Checker(rules, categories, legacy)
    c.run(data)
    return c.errors, c.warnings


def main():
    args = [a for a in sys.argv[1:] if a != "--legacy"]
    if len(args) != 1:
        print(__doc__)
        sys.exit(2)
    try:
        with open(args[0], encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR JSON 解析失败: {e}")
        sys.exit(1)
    errors, warnings = validate(data, legacy="--legacy" in sys.argv[1:])
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    print(f"\n{len(errors)} 个错误, {len(warnings)} 个警告")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
