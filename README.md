# Bounded Investigator MVP v1.7

用于恶意代码静态分析的“有界调查 Worker”。完整流程为：材料包 → DAG 调度 → `Analyzer → Reviewer → 条件 Reviser` → 全局事实整合 → 程序生成三层证据报告。上游包完成后，只向下游传递已验证结果的紧凑摘要。

建立整体认知请先读 [src 架构与处理流程](docs/src-architecture.md)，其中包含模块职责、核心节点、正常与失败分支、产物索引和源码阅读顺序。

当前核心能力：

1. **DAG handoff**：按 `manifest.json` 的 `depends_on` 动态调度，只传递上游紧凑事实投影。
2. **Artifact detection specificity**：artifact 同时输出行为 `role` 与检测价值 `detection_specificity`。
3. **Reasoning token 统计**：总 usage 和每阶段 usage 单独记录 reasoning token。
4. **Clean-context Format Repair + Result Preservation**：structured-output 校验失败时，独立 Repair LLM 只修格式；required 字段缺失立即停止 Repair，并保存 raw/partial 结果，避免已完成分析丢失。
5. **Decision-only Global Reconciliation**：程序先 exact-reduce claims/artifacts，Global LLM 只输出 `keep / merge_into / superseded_by / unresolved` 关系决策，不再重写事实全文；程序随后从原 validated facts 组装 canonical result。
6. **三层证据报告与审计**：canonical facts 按显式 `evidence_level` 分为已确认行为、疑似行为和调查线索；历史 gap/handoff 保留为调查记录。报告由程序生成，无最终评估模型调用。旧 Gap Reconciliation 配置仅保留兼容，不再执行。

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

```

你本地已经验证较小的 `4096 / 1536 / 2048` 容易触发输出截断/格式失败，所以本版默认采用 `40960 / 15360 / 20480`。相应地，单包总 token 上限提高到 `130000`，否则 Analyzer 单阶段上限会和总预算互相打架。所有值仍可自行调整。

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

Prompt 位于：

```text
src/bounded_investigator/prompts/
```

Schema 唯一真源：

```text
src/bounded_investigator/schemas.py
```

不要另维护第二份 JSON/YAML schema。

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
- 三层知识报告、来源去向审计、材料核查与补料建议；另保留旧阶段的兼容逻辑测试。

## 10. Decision-only Global Reconciliation

DAG 所有 Worker 完成后，不再把全部 claims/artifacts 交给一个 LLM 重新生成 canonical 全文。v0.7 改为方案 A：

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

v1.7 在全局来源校验通过后，直接用程序生成：

- `_report_ready.json`：三层行为知识、IOC、检测特征、上下文对象、材料建议和输入覆盖告警。
- `_final_report.md`：可阅读报告。
- `_knowledge_audit.json`：每条原始 claim/artifact 的最终去向与覆盖审计。

`confirmed` 表示证据直接支持描述的行为能力；不代表运行时一定发生。旧结果未声明 `evidence_level` 时，进入调查附录，不由 `confidence` 自动升级。历史 gaps 作为调查记录保留，可能已被后续节点回答，不作为重新评估的当前未知项。

从已保存的 Worker 结果重新执行全局整合及报告，不重跑 Workers：

```bash
uv run bounded-investigator reconcile results --output-dir replayed_results --config config.yaml
```

从已有全局封套重生成报告，无模型请求：

```bash
uv run bounded-investigator report results/_global_reconciliation.json --output-dir reported_results --config config.yaml
```

`assess` 是 `report` 的别名。完整 DAG 报告要求启用 `global_reconciliation`；无 manifest 的独立批处理分支只生成单包结果。

旧 `gap_reconciliation` 和 `final_evidence_assessment` 配置仍接受兼容读取，但当前自动报告主链路不执行这两个模型阶段。更多说明见 [src 架构与处理流程](docs/src-architecture.md)。
