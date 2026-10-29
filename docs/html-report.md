# 报告 Agent 与 HTML 1.1

`report-html` 将已有全局分析事实组织为单文件静态 HTML。当前采用 malware-report-html 模板 1.1、内部 HTML skill 1.2 和报告角色 1.3。一个报告 Agent 编排内容，程序绑定原始事实、校验并渲染；必要时由同一角色局部修订一次。

报告头、指标条和关键发现帮助读者快速判断能力、风险与证据状态；行为链与机制章节解释具体关系。HTML 成功生成不等于证据审计通过，审计状态同时展示在报告头并记录在 manifest。

## 运行与批处理

```bash
uv run bounded-investigator report-html results/_global_reconciliation.json \
  --output-dir html_preview_v11 \
  --context-file report-context.json \
  --packets-dir testdata/packets \
  --packet-results-dir results \
  --config config.yaml --env-file .env
```

仅全局封套路径必填。输出目录默认 `html_preview`，必须与全局结果、单包结果和材料目录不同；验证新版本时使用新目录。单包结果目录默认全局结果所在目录，独立命令的材料目录读取 `J*.md`。材料只用于恢复证据及源码片段，不触发重新调查。没有源码材料时可复用与 canonical facts 完全匹配的已保存片段，审计状态仍如实标记未执行。

DAG `batch` 保存有效全局结果后默认自动生成 `<output-dir>/html_report/_final_report.html`，`--no-html-report` 可关闭。`--context-file` 与独立命令含义相同。自动报告沿用本批次 scope、inventory、实际选中的包及专家补充材料，遵守 pattern 和 manifest 的任务集合。没有有效全局结果时记录 skipped；HTML 失败保留分析结果和 Markdown，记录失败并返回非零，可单独重试 `report-html`。

新 batch 会清理该批次报告目录的旧发布文件及 manifest，保留历史 attempt 和其他文件。清理前检查输入文件与发布文件名冲突、输出目录与输入目录重合、输出落入外部补充材料目录等情况，防止损坏输入。`report/assess` 仍离线输出知识 JSON 与 Markdown，`reconcile` 不自动调用 HTML。

## 配置与修订预算

```yaml
stages:
  report_composition:
    model: global_reconciler
    max_output_tokens: 16384
html_report:
  max_model_requests: 2
  total_tokens_limit: 64000
  wall_clock_seconds: 300
```

报告阶段使用独立预算，不占 A/R/R 预算。正常一次请求，契约问题最多增加一次局部修订；`max_model_requests: 1` 关闭修订，上限为 2。首稿与修订共享总 token 预算及期限，不自动提高已有配置额度。SDK token 限制依赖返回用量，不能视为精确计费硬上限；格式修复单独计量，两次报告请求共享原有格式修复额度。缺少 `stages.report_composition` 时在模型请求前报错。

修订输入包括结构化 issues、相关事实、受影响内容及章节索引。补丁只允许修改授权顶层字段、替换受影响章节，遗漏行为时才允许追加机制章节；不得删除或重排原章节，不得丢失原有合法行为。引用数量增加或只改标题不能冒充补充行为。补丁合并后完整复检，失败不会触发第三次报告请求。

## 内容与事实契约

模型输出 `ReportComposition`，程序编译为 `schema_version: "1.1"` 的模板 JSON。分析事实 schema 仍为 11.0。

- `title` 与 `findings` 为必填。标题描述样本画像；发现可为空数组。发现使用 `{rule, title, claim_ids, state_note?, section_index?}`，引用已有行为，章节索引从零开始。
- 普通规则从 `tier_rules.json` 选择，档位和类别由程序取回，不能覆盖。只有 `CUSTOM` 额外填写 `tier`、规则表内的 `category`、`tier_reason`，并产生人工复核提示。恶意程度与证据状态相互独立，风险仍根据实际能力、影响和条件研判。
- 发现引用全部为 confirmed 时编译为 `confirmed`，包含 suspected 时为 `partial`。partial 必须有 `state_note` 和通过 `claim_ids` 关联的缺口。lead 与 unclassified 只进入缺口，不能升级为关键发现。程序取回原始证据位置，不让模型重写。
- `gaps` 使用 `{gap_id?, claim_ids[], text, impact, blocking}`，至少关联已有历史问题或行为。历史问题只是上下文，不自动成为当前缺口。正文多处描述未闭合问题但缺口为空会进入一次修订；源码中的原始注释不参与报告未知点计数。
- 已确认和疑似行为须在正文实质表达，可以按目的合并，不要求穷尽原始字段。每章先用 callout 给出结论，每章叙述段落不超过三段，三个以上同构对象优先用表格。内部引用合法不能自动证明正文含义正确。
- `chain` 仅表达证据支持的先后顺序；`brief` 为可选节点短说明，建议不超过 12 字。`key` 只标真正造成危害的一到三步。不能从 DAG、编号或文件顺序推定执行链；无依据时保留空链。
- IOC、检测特征、上下文对象集中列入 IOC 模块，分别映射为 `high`、`medium`、`context`。同值按此优先级保留一次并合并角色、证据和简短说明，原值不改写；不会将读取来源等对象静默改成感染 IOC。不要另开检测线索章节重复列值。
- `ioc_notes` 可按 artifact_id 提供简短用途；不自动拼接原始 meaning 或整段证据观察。原始对象值和选中源码逐字绑定；正文不得展示内部 claim/artifact/source ID、confidence 或处理状态。

检测建议从已有发现出发，使用组合特征；只看到输入 Hook 而未见保存或外传时不能认定键盘记录。家族公开能力与样本事实分开，未知 IOC、路径、归因和技术映射不得编造。

## 上下文与真实空状态

`--context-file` 接受 JSON，顶层支持 `meta`、`hashes`、`attack`、`attribution`、`attribution_empty_reason`、`yara`。`meta` 允许 `id`、`title`、`date`、`product`、`edition`、`method`、`tlp`、`sample`。风险和判断来自报告，状态由流程生成，审计由程序读取，不能从上下文覆盖。

```json
{
  "meta": {
    "id": "sample-001",
    "method": "静态分析（基于提供的源码与结构化分析结果）",
    "sample": {"filename": "sample.exe", "filetype": "PE32+", "arch": "x64"}
  },
  "hashes": [],
  "attack": [],
  "attribution": null,
  "attribution_empty_reason": "尚无经过核实的家族归因资料。",
  "yara": null
}
```

省略标题时使用报告编排的样本画像；报告编号默认 `HTML-REPORT`，日期采用生成时的北京时间，分析方式默认说明基于已提供源码与结构化结果的静态分析。显式传入的空或非法必填元信息会报错。

不再自动填入全零哈希、测试归因、ATT&CK 或 YARA。缺失或 null 哈希归一为空数组，报告头和哈希模块显示“未提供样本哈希”；无归因时解释原因，ATT&CK 和 YARA 无数据时隐藏。显式提供的合法外部资料保留。manifest 的 `test_fills` 字段为兼容既有消费者保留，值始终为空数组。

## 审计、接口与资源

`meta.audit` 使用现有知识审计及源码引用审计：来源不完整或审计失败显示 `incomplete`；实际执行且通过才显示 `passed`；缺少执行证据、未运行或未知状态显示 `not_run`。真实引用计数取 `total_segments` 与 `invalid_segments`，未运行不伪造计数。全局封套的 `provenance_valid` 或单独的 `passed=true` 不能替代执行状态。

报告角色通过 `{{skill:malware-report-html}}` 确定性加载内部 skill、组件说明、模块说明和规则表；不扩大 A/R/R harness 的角色集合。角色与 skill 原文、实际指令、资源及指纹保存到报告快照，规则表和模板资源也纳入指纹。模板 CSS 对照新版源 skill 保持一致，仅适配包内导入、项目证据格式、空哈希、中性文案、分析方式类型与源码文本检查。

- `HtmlReportSkill.instructions()`：返回技能、组件、模块说明及规则表。
- `compile(composition, facts, context=None, inventory=None, *, projection, audit=None)`：校验并编译只读事实，返回模板数据和空的测试填充清单；独立调用不提供审计时显示未审计。
- `validate(data)`、`render(data)`：确定性校验和生成单文件 HTML，无需模型。新报告严格使用 1.1 校验，保留已有 1.0 模板数据读取能力；历史 ReportComposition 不自动补写新增语义字段。

## 产物与失败行为

| 文件 | 内容 |
| --- | --- |
| `_report_document.json` | 通过完整编排校验的报告文档，内部保留事实引用 |
| `_html_report.json` | 绑定原始事实后的模板数据，不暴露内部引用字段 |
| `_final_report.html` | CSS 与数据内嵌的离线静态页面，无外部脚本或资源请求 |
| `_html_report_manifest.json` | 状态、版本、指纹、审计、输入摘要、用量、产物、告警和错误 |
| `_html_report_artifacts/<attempt>/` | 指令快照、首稿、修订、输入投影与失败诊断 |

开始新运行时清理旧文档和发布文件。首稿有效才保存本次文档；编译或渲染失败时保留本次有效文档及诊断，撤去 HTML 和模板 JSON。只有全部成功才原子发布，完成 manifest 写入失败也会撤去已发布文件。输入事实、审计和历史目录不被改写。

## 离线演示与验收

```bash
uv run --offline python examples/generate_html_preview.py --output-dir examples/html-preview-v11
uv run --offline python examples/generate_html_preview.py --output-dir examples/html-preview-v11-empty --context-file examples/html-preview-v11-input/empty-context.json
uv run --offline pytest tests -q -rs
```

[全字段预览](../examples/html-preview-v11/_final_report.html) 与 [空上下文预览](../examples/html-preview-v11-empty/_final_report.html) 均使用新版独立夹具和固定模型响应，真实模型请求数为 0。整个样本、行为、对象及源码均为合成演示数据；全字段上下文显式提供演示哈希、归因、映射和规则，仅用于版式验收。空上下文预览验证系统不会自动补这些内容。旧版预览与原始夹具保留。

自动化验收覆盖新字段、规则与 CUSTOM、证据状态、缺口关联、三类指标去重、审计状态、上下文空值、局部修订、失败发布、批处理和输入不变；安装包另需隔离验证说明、规则、CSS 与模板资源加载。结构与渲染检查不能代替真实模型对恶意行为语义的判断，也不能冒充浏览器视觉验收。
