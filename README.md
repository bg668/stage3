# Bounded Investigator MVP v1.7.3

用于恶意代码静态分析的“有界调查 Worker”。完整流程为：材料包 → DAG 调度 → `Analyzer → Reviewer → 条件 Reviser` → 全局事实整合 → 程序生成三层证据报告。上游包完成后，只向下游传递已验证结果的紧凑摘要。

建立整体认知请先读 [src 架构与处理流程](docs/src-architecture.md)，其中包含模块职责、核心节点、正常与失败分支、产物索引和源码阅读顺序。

当前核心能力：

已支持版本化 harness、独立角色定义和程序按角色加载的技能。目录、运行参数与迁移方式见 [harness 使用说明](docs/harness-runtime.md)。

1. **DAG handoff**：按 `manifest.json` 的 `depends_on` 动态调度，只传递上游紧凑事实投影。
2. **Artifact detection specificity**：artifact 同时输出行为 `role` 与检测价值 `detection_specificity`。
3. **Reasoning token 统计**：总 usage 和每阶段 usage 单独记录 reasoning token。
4. **Clean-context Format Repair + Result Preservation**：structured-output 校验失败时，独立 Repair LLM 只修格式；required 字段缺失立即停止 Repair，并保存 raw/partial 结果，避免已完成分析丢失。
5. **Decision-only Global Reconciliation**：程序先 exact-reduce claims/artifacts，Global LLM 只输出 `keep / merge_into / superseded_by / unresolved` 关系决策，不再重写事实全文；程序随后从原 validated facts 组装 canonical result。
6. **三层证据报告与审计**：canonical facts 按显式 `evidence_level` 分为已确认行为、疑似行为和调查线索；gap/handoff 保留为调查记录。报告由程序生成，无最终评估模型调用。
7. **独立 HTML 报告预览**：从通过来源校验的全局事实编排摘要与机制章节，使用 `malware-report-html.skill` 原模板生成单文件 HTML。缺失的补充模块使用明确标注的固定测试数据，不写回分析事实。该命令单独调用报告编排模型，不改变现有离线报告路径。

## 1. 安装

```bash
uv sync --extra dev
cp .env.example .env
```

API Key 只从 `.env` / 环境变量读取。

## 2. 配置

```yaml
providers:
  deepseek:
    type: deepseek
    base_url: "https://api.deepseek.com"
    api_key_env: "DEEPSEEK_API_KEY"
    sdk_max_retries: 0

models:
  investigator:
    provider: deepseek
    model_name: "deepseek-chat"
    api_mode: chat
    output_mode: tool
    reasoning: omit
    stream_output: true

  reviewer_fast:
    provider: deepseek
    model_name: "deepseek-chat"
    output_mode: tool
    reasoning: omit

  repair_fast:
    provider: deepseek
    model_name: "deepseek-chat"
    # Format Repair 实际走纯文本输出，避免 repair 自身再次触发 structured validation。
    output_mode: prompted
    reasoning: omit
    temperature: 0

  global_reconciler:
    provider: deepseek
    model_name: "deepseek-chat"
    output_mode: tool
    reasoning: omit

stages:
  analyzer:
    model: investigator
    max_output_tokens: 40960
  reviewer:
    model: reviewer_fast
    max_output_tokens: 15360
  reviser:
    model: investigator
    max_output_tokens: 20480
  format_repair:
    model: repair_fast
    max_output_tokens: 40960
  global_reconciliation:
    model: global_reconciler
    max_output_tokens: 40960
  report_composition:
    model: global_reconciler
    max_output_tokens: 16384

workflow:
  max_model_requests: 3       # 仅 Analyzer/Reviewer/Reviser
  total_tokens_limit: 130000  # 核心分析独立预算
  wall_clock_seconds: 600
  output_retries: 0           # 不使用原 Agent 的 full-context output retry
  enable_revision: true

format_repair:
  enabled: true
  max_attempts: 3
  total_tokens_limit: 240000  # repair 独立预算，仅格式失败时消耗
  deterministic_cleanup: true

global_reconciliation:
  enabled: true
  max_model_requests: 1
  total_tokens_limit: 160000
  wall_clock_seconds: 300

html_report:
  max_model_requests: 2
  total_tokens_limit: 64000
  wall_clock_seconds: 300

```

示例的 Analyzer / Reviewer / Reviser 输出上限为 `40960 / 15360 / 20480`，单包总 token 上限为 `130000`。总上限不得低于任一核心阶段的输出上限；具体值按模型和材料规模调整。

配置顶层拒绝未知字段。已删除的 `gap_reconciliation`、`final_evidence_assessment` 及其 stage 配置不再接受，使用当前 `config.example.yaml` 配置运行阶段；其余供应商、模型、预算和环境变量约定不变。

`reasoning: omit` 表示完全不发送 reasoning/thinking 参数；`disabled` 才显式发送关闭值。

## 3. DAG handoff

测试清单位于：

```text
testdata/manifest.json
```

格式：

```json
{
  "jobs": [
    {"job_id": "J00", "depends_on": []},
    {"job_id": "J01", "depends_on": ["J00"]},
    {"job_id": "J12", "depends_on": ["J11", "J01"]}
  ]
}
```

Batch 默认自动查找：

```text
<input_dir>/manifest.json
<input_dir>/../manifest.json
```

也可以显式指定：

```bash
uv run bounded-investigator batch \
  testdata/packets \
  --manifest testdata/manifest.json \
  --config config.yaml \
  --output-dir results
```

默认在 DAG 全局整合得到有效 canonical result 后自动生成 HTML，无需再执行 `report-html`。页面位于 `results/html_report/_final_report.html`，命令结束时打印该路径；结构化报告、模板 JSON、manifest 和阶段诊断保存在同一子目录。

自动报告按 `html_report` 预算正常调用一次报告模型，内容校验失败时最多修订一次，两次共享预算；格式修复另行计量且额度不翻倍。`--no-html-report` 可关闭；`--context-file report-context.json` 可传入已有的样本身份和补充资料。启用自动报告时缺少 `stages.report_composition` 会在分析前报错。没有 manifest、全局整合关闭或未得到有效全局结果时，明确记录跳过原因。

HTML 失败不会删除分析 JSON 或 Markdown，`_dag_summary.json` 独立记录 HTML 状态及诊断位置，命令返回失败状态；可使用保留的全局结果单独重试 `report-html`，不必重新分析。新 batch 开始时只清理 `html_report/` 内旧的发布文件，保留历史 attempt 诊断及其他文件。

调度器只在依赖完成后启动子任务，但不同分支仍按 `batch.concurrency` 并发执行。父包失败时，子包不会被整体取消，而会收到：

```text
[dependency J11]
status: unavailable
reason: UnexpectedModelBehavior: ...
[/dependency]
```

这适合当前测试阶段，因为某个包的结构化输出失败不应该把其他分支一起报废。

### Handoff 内容

不传完整父包 JSON，只投影：

```text
[dependency J11]
status: complete_with_gaps
summary: ...
claims:
- Firefox profile locate ... | evidence=F-0483 ...
artifacts:
- [read_source/none] file: signons*.txt | ...
unresolved:
- ...
[/dependency]
```

Analyzer、Reviewer、Reviser 都看到相同 dependency context，因此 Reviewer 不会再把 J11 已经解释的内容错误判成 J12 的 outside_packet 缺口。

运行结束额外生成：

```text
results/_dag_summary.json
```

用于查看每个 job 的依赖、成功/失败状态和整批耗时。

如果没有发现 manifest，batch 会明确打印 warning，并退化到原来的 independent 模式。

## 4. Artifact role + detection specificity

每个 artifact 现在有两个正交字段：

```json
{
  "kind": "file",
  "value": "%TEMP%\\NOIP.abc",
  "role": "created_output",
  "detection_specificity": "high"
}
```

`role` 回答“它在行为中做什么”：

- `created_output`
- `created_mutex`
- `persistence_value`
- `config_marker`
- `read_source`
- `environment_probe`
- `system_path`
- `parser_marker`
- `other`

`detection_specificity` 回答“它作为感染/检测指标有多大价值”：

- `high`：强样本/家族/感染特异性；
- `medium`：需要组合上下文；
- `low`：较通用，只作辅助；
- `none`：正常系统对象、数据源、环境探针等，不应作为感染 IOC。

两者不绑定。例如：

- `config_marker` 可以是 `high`；
- 标准 Windows `Run` 路径虽然是 `persistence_value`，通常只能是 `low/none`；
- `signons*.txt` 是 `read_source`，通常 `none`。

Reviewer 也会检查 `artifact_specificity_error`，但只有会造成高价值 IOC/非 IOC 混淆的错误才允许判 major。

## 5. Usage / reasoning token

每个结果现在包含：

```text
usage
├─ input_tokens
├─ output_tokens
├─ reasoning_tokens
├─ visible_output_tokens
├─ total_tokens
└─ ...

stage_usage
├─ analyzer
├─ reviewer
└─ reviser（若触发）
```

PydanticAI/Provider 能提供时，`reasoning_tokens` 从 usage details 的：

```text
reasoning_tokens / thinking_tokens / thoughts_tokens
```

中提取。它是 `output_tokens` 的子集，不会再加到 `total_tokens` 上重复计费；`visible_output_tokens = output_tokens - reasoning_tokens`。

这能直接判断 DeepSeek V4 等模型到底有多少预算花在隐藏 reasoning，而不是只看到一个异常巨大的 output token 总数。

## 6. Prompt 与 Schema

LLM 输入继续使用紧凑 Markdown/XML；机器输出继续由 PydanticAI structured output 强约束。

三角色策略和技能位于 `src/bounded_investigator/harnesses/baseline-v1/`；共享证据规则、输入模板和其他阶段提示词位于：

```text
src/bounded_investigator/prompts/
```

调查与全局结果的 schema 唯一真源：

```text
src/bounded_investigator/schemas.py
```

不要另维护第二份 JSON/YAML schema。独立 HTML 报告的编排契约 `ReportComposition` 位于 `reporting/composition.py`，它只描述展示组织，不替代分析结果契约。

## 7. 运行

单包，不使用 DAG：

```bash
uv run bounded-investigator run \
  testdata/packets/J00.md \
  --config config.yaml \
  --output results/J00.json
```

完整 DAG：

```bash
uv run bounded-investigator batch \
  testdata/packets \
  --manifest testdata/manifest.json \
  --config config.yaml \
  --output-dir results
```

模型实时输出仍由 model profile 控制：

```yaml
stream_output: true   # false 关闭
```

批量调试如果不想多个 packet 流交错，可临时设：

```yaml
batch:
  concurrency: 1
```

## 8. Clean-context Format Repair 与失败兜底

MVP 把“分析是否完成”和“结构化结果是否通过”分开。structured output 失败不会再直接抹掉已经生成的分析：

```text
原 Stage 输出
  ↓
保存 raw_output.txt
  ↓
Pydantic validation
  ├─ PASS → success
  └─ FAIL
       ↓
       required 字段 missing ?
       ├─ YES → 不调用 Repair，直接 Partial Salvage
       └─ NO  → Clean Format Repair，最多 3 次
                   ↓ 每次重新校验
                   required missing → 立即停止
                   第 3 次仍失败 → Partial Salvage
```

Repairer 仍是干净上下文，只接收 `TARGET_SCHEMA + VALIDATION_ERROR + ORIGINAL_RAW_OUTPUT + 当前候选`，禁止重新分析源码或补事实。允许为空的字段通过 Pydantic 默认值处理；真正 required 的字段缺失视为语义缺失，Repair 不得凭空生成。

Partial Salvage 只处理 `InvestigationResult` 中的 `claims[]` 和 `artifacts[]`：逐项执行原 Pydantic schema 校验，通过的保留，失败项直接丢弃，不修、不猜。

最终 delivery status：

```text
success
success_after_repair
partial_validated
raw_only
execution_failed   # 保留枚举，真实模型/API执行异常仍由 CLI error 文件记录
```

阶段产物会写到：

```text
results/_artifacts/Jxx/
  analyzer/
    raw_output.txt
    validated_result.json      # 验证成功时立即 checkpoint
    partial_result.json        # salvage 成功时
    validation_errors.json     # 降级时
  reviewer/
    ...
  reviser/
    ...
  format_repair/
    analyzer_attempt_1.txt
    ...
```

Reviewer 格式失败时保留已经验证的 Analyzer 结果；Reviser 格式失败时保留 Analyzer draft，并设置 `revision_failed=true`。这样末端格式问题不会把上游已经完成的分析一起判死。

DAG handoff 只消费可信内容：

- `success / success_after_repair`：传完整已验证 result；
- `partial_validated`：只传逐项验证成功的 claims/artifacts，并显式标记 partial；
- `raw_only`：不传未经验证事实，只标记 dependency unavailable。

Repair 使用独立预算，不占 Analyzer/Reviewer/Reviser 的核心请求额度。`format_repair.attempts` 只统计真正调用 Repair LLM 的次数，因此 required missing 直接降级时为 `0`。

## 9. 测试

无需真实 LLM 即可执行：

```bash
uv run python -m pytest tests -q
```

当前覆盖：

- provider/model/stage 配置与 reasoning 归一化；
- compact prompt；
- artifact role + detection specificity；
- Reviewer major 约束；
- manifest DAG cycle/依赖校验；
- compact dependency handoff；
- 失败依赖降级传递；
- reasoning token 统计与 stage delta；
- format repair deterministic cleanup 与 required-missing 立即停止；
- repair prompt 外置与 clean-context 输入契约；
- partial salvage 只保留独立验证通过的 claims/artifacts；
- raw-only/partial DAG handoff 降级；
- repair/core usage 合并不重复计算 reasoning；
- Global Reconciliation deterministic reducer、decision-only resolver、canonical assembler、provenance/coverage gate；
- 三层知识报告、来源去向审计、材料核查与补料建议；
- 角色技能展开、不可变 harness 快照、指令与模型输出 schema 的回归检查。
- 独立报告角色与 HTML skill 加载、目的聚类和有序内容块、上下文与测试填充、原模板资源和失败产物处理。

## 10. Decision-only Global Reconciliation

DAG 所有 Worker 完成后，由程序归并事实，LLM 只处理关系决策：

```text
validated / partial packet facts
  ↓
Deterministic Reducer（程序）
  ├─ exact claim merge
  ├─ exact artifact merge
  └─ artifact metadata conflict detection
  ↓
Decision-only Global Resolver
  ├─ keep_claim_ids[]
  ├─ merge_into[]
  ├─ superseded_by[]
  ├─ unresolved[]
  └─ artifact_conflict_decisions[]
  ↓
Canonical Assembler（程序）
  ↓
provenance / coverage gate
  ↓
canonical facts
```

Global LLM **不读取源码、不调用工具、不重写 behavior/action/evidence/IOC value**。它只判断已经验证的 reduced claim 之间是什么关系。绝大多数不冲突的 claim 只需要把 ID 放进 `keep_claim_ids`，不需要重复输出事实和 evidence。

### 10.1 程序先做的事

每个原始 claim/artifact 仍有稳定来源 ID：

```text
J13:C001
J14:C004
J00:A003
```

程序先将完全相同的 claim 按 `behavior + condition + source + action + sink` 归并成 `R:Cxxx`；artifact 按 `kind + exact value` 归并成 `R:Axxx`。合并只聚合真实 source ids/evidence，不让 LLM生成新事实。

Artifact 默认完全由程序组装；只有同一 `kind/value` 下 `role` 或 `detection_specificity` 冲突时，才把该 conflict group 交给 Resolver 选择一个真实 source artifact 作为元数据来源。若 Resolver 未处理，程序采用保守值并生成 gap，不会提升成高置信 IOC。

### 10.2 Resolver 输出只有关系

典型输出形态：

```json
{
  "keep_claim_ids": ["R:C001", "R:C003"],
  "merge_into": [
    {"claim_id": "R:C004", "target_claim_id": "R:C003", "reason": "same behavior"}
  ],
  "superseded_by": [
    {
      "claim_id": "R:C002",
      "target_claim_id": "R:C003",
      "reason": "direct CreateRemoteThread evidence supersedes APC inference"
    }
  ],
  "unresolved": []
}
```

`merge_into` / `superseded_by` 的 target 必须位于 `keep_claim_ids`，不允许形成关系链。模型漏掉某个 reduced claim 时，程序 coverage gate 会自动把它保留成 unresolved，而不是静默删除。

### 10.3 Canonical fact 由程序组装

Resolver 决策完成后，程序直接从原始 validated claim/artifact 拷贝：

- behavior / condition / source / action / sink；
- evidence；
- artifact value / role / specificity；
- confidence / limitations；
- source ids。

因此 Global LLM 无法创造新的 IOC、函数证据或恶意行为。`superseded_by` 只改变事实状态，不改写源分析历史。

### 10.4 硬门禁与降级

程序继续执行：

1. **Provenance gate**：canonical evidence/value/source id 必须来自真实输入。
2. **Coverage gate**：每个原始 claim 必须最终进入 canonical / superseded / unresolved；每个 artifact 必须进入 canonical。
3. **Resolver failure fallback**：如果 Global Resolver 自身格式失败且 Repair 也失败，程序退化为“所有 reduced claims 保守 keep + artifacts 确定性合并”，仍生成 canonical facts，并增加 `Global semantic reconciliation did not complete` gap。Resolver 失败只意味着冲突未消解，不会再次让全局事实层消失。

输出：

```text
results/_global_reconciliation.json
results/_global_artifacts/
  input_projection.md          # decision-only resolver 输入
  reducer_summary.json         # source→reduced group 映射和压缩统计
  global_reconciliation/
    raw_output.txt
    validated_result.json      # schema-valid relationship decisions
    resolver_decisions.json    # decision-only 结果
    canonical_result.json      # 程序组装+provenance gate 后的权威结果
results/_dag_summary.json
```

`_dag_summary.json` 会额外记录：

```text
source_claim_count
reduced_claim_count
source_artifact_count
reduced_artifact_count
artifact_conflict_count
```

后续最终报告只读取 `canonical_result.json` / `_global_reconciliation.json` 中的 canonical result，不再直接扫描全部 packet JSON。

## 11. 三层报告与结果重放

全局来源校验通过后，直接用程序生成：

- `_report_ready.json`：三层行为知识、IOC、检测特征、上下文对象、材料建议和输入覆盖告警。
- `_final_report.md`：可阅读报告。
- `_knowledge_audit.json`：每条原始 claim/artifact 的最终去向与覆盖审计。

`confirmed` 表示证据直接支持描述的行为能力；不代表运行时一定发生。未声明 `evidence_level` 的结论进入调查线索，不由 `confidence` 自动升级。调查过程中的 gaps 作为记录保留，可能已被后续节点回答，不作为重新评估的当前未知项。

从已保存的 Worker 结果重新执行全局整合及报告，不重跑 Workers：

```bash
uv run bounded-investigator reconcile results --output-dir replayed_results --config config.yaml
```

从已有全局封套重生成报告，无模型请求：

```bash
uv run bounded-investigator report results/_global_reconciliation.json --output-dir reported_results --config config.yaml
```

`assess` 是 `report` 的别名。完整 DAG 报告要求启用 `global_reconciliation`；无 manifest 的独立批处理分支只生成单包结果。

当前保存结果的结构版本为 `11.0`。本次清理删除旧最终评估与 Gap Reconciliation 的执行实现、配置和提示词，不提供旧 Python 导入路径或历史 JSON 转换器。模型输出契约、角色指令及修订保护保持原有规则；软件包版本仍为 `1.7.3`，本次未发布新版本。

## 12. 报告 Agent 与 HTML 预览

```bash
uv run bounded-investigator report-html \
  results/_global_reconciliation.json \
  --output-dir html_preview \
  --context-file report-context.json \
  --packets-dir testdata/packets \
  --packet-results-dir results \
  --config config.yaml \
  --env-file .env
```

仅全局结果路径必填；其余参数可选。输出目录不得与输入全局结果所在目录、显式单包结果目录或材料目录重合。`report-html` 使用一个报告 Agent 加载 HTML 生成 skill，正常只调用一次；正文遗漏或引用错误等内容校验失败时，向同一角色提交具体问题，最多再修订一次。程序不自动补写正文。`html_report.max_model_requests` 默认为 2，设为 1 可关闭语义修订。示例中的 `stages.report_composition` 复用 `global_reconciler` 模型配置；两次请求共享默认 64000 tokens、300 秒，单次最大输出为 16384 tokens，实际按配置执行，不自动增加额度。缺少该阶段配置时此命令及默认启用 HTML 的 DAG batch 明确报错。格式修复共用独立额度，不占 A/R/R 请求额度。现有 `report/assess` 仍完全离线，reconcile 不自动生成 HTML；batch 默认在全局结果有效时自动生成，见前文。

报告角色定义在 `reporting/agents/report.md`，以 `{{skill:malware-report-html}}` 引用 `reporting/skills/malware-report-html/SKILL.md`。程序确定性展开技能及组件说明，不扩展 A/R/R harness 的角色集合，不自主发现或安装个人技能。报告 Agent 围绕样本目的聚类全部行为，包括未确认行为，解释机制协同并按重要性排序。HTML skill 根据有序内容块绑定原始对象和源码，选择文字、列表、表格及代码等已有组件，不再机械生成全字段表。正文不展示内部证据等级、confidence 和来源 ID；关键对象原值及必要源码位置保留。不增加取舍台账或额外内容审计。

生成结构化报告 `_report_document.json`、模板数据 `_html_report.json`、页面 `_final_report.html`、运行记录 `_html_report_manifest.json`，以及 `_html_report_artifacts/` 中的指令快照和阶段诊断，不额外生成 Markdown。开始新运行时清理旧文档和页面；报告 Agent 成功后即保存本次有效文档，后续 HTML 失败时保留它与诊断，撤去最终 HTML 和模板 JSON。manifest 记录报告角色与 skill 的版本和指纹。

HTML 使用新版 malware-report-html 1.1 的 CSS、首屏规则分档关键发现、审计徽章和 IOC 分类。`context-file` 可提供样本元信息、哈希、ATT&CK、归因和 YARA；缺失资料显示真实空状态，无哈希也可生成，不再自动填入测试资料。HTML skill 的程序接口也能独立校验和渲染已有模板数据，无需调用模型。

详细字段、空值约定与失败行为见 [HTML 报告说明](docs/html-report.md)。开发验收使用固定模型响应，不自动调用真实模型。HTML 文件可下载后在本地浏览器打开；文件生成验证不等于已经完成视觉核对。

当前新版演示：[全字段 HTML](examples/html-preview-v11/_final_report.html)、[空上下文 HTML](examples/html-preview-v11-empty/_final_report.html)、[结构化报告](examples/html-preview-v11/_report_document.json)、[模板 JSON](examples/html-preview-v11/_html_report.json)。整个样本均为合成数据，采用固定响应、零真实模型调用；全字段版的演示资料由上下文显式提供，空上下文版验证真实空状态。旧版演示和原始输入保留。

报告角色 1.3、内部 HTML skill 1.2、模板 1.1：新增必填 title/findings，规则表决定能力档位，程序绑定证据与确定程度。partial 发现关联缺口，调查线索不升级为关键发现。IOC、检测特征和上下文对象统一展示并去重，审计未运行或状态未知时不会显示通过。保留一次局部修订与独立预算；分析 schema 仍为 11.0，原始事实及历史结果不改写。

离线复现：`uv run --offline python examples/generate_html_preview.py --output-dir html_preview_v11`。空上下文版另加 `--context-file examples/html-preview-v11-input/empty-context.json`。脚本不连接模型供应商。

## 13. 代码职责与已知限制

代码按模型执行（`execution/`）、包内调查（`investigation/`）、材料与证据（`evidence/`）、报告（`reporting/`）、批次与重放调度（`orchestration/`）组织。`cli.py` 负责命令适配，`workflow.py` 编排单包与全局流程；具体执行和格式修复由组合的 `StageRunner` 承担。更多说明见 [src 架构与处理流程](docs/src-architecture.md)。

本次报告升级不接入 harness2、不增加发布门禁、不进行真实模型实验。底层知识审计未提供材料时仍可能记录 `not_run/passed=true`；HTML 会识别未执行状态并显示“证据未审计”，不会据此展示通过。报告完成状态不能单独作为证据质量或发布批准的依据。
