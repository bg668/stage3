# 版本化 harness 与角色技能

调查角色定义与可复用分析技能分开，由程序按角色加载。DAG、A/R/R、模型供应商、格式修复、模型输出契约与修订保护保持原有规则。本说明中的 harness 仍只管理 Analyzer、Reviewer、Reviser。报告 Agent 有独立的角色和 HTML skill 加载入口，见下文。执行代码按职责组织，见 [src 架构与处理流程](src-architecture.md)。

## 文件组织

- `src/bounded_investigator/harness/`：可信的加载、校验、组合与快照模块。
- `src/bounded_investigator/harnesses/baseline-v1/harness.yaml`：格式版本与策略版本。
- 同目录 `systemprompt.md`：三角色共享策略，基线为空，以保持原指令。
- 同目录 `agents/analyzer.md`、`reviewer.md`、`reviser.md`：角色定义；从原角色提示词迁移，原位置不再保留重复副本。
- 同目录 `skills/packet-fact-boundary/SKILL.md`：首个实际内置的分析方法技能，只分配给 Reviewer，区分本包新事实与已确认的上游事实。
- 同目录 `skills/control-flow-conditions/SKILL.md`：分配给 Analyzer、Reviewer，从调用点核对外层分支、循环、API 成功与结果使用条件。
- 自定义 harness 下的 `skills/<name>/SKILL.md`：可选分析方法或领域技能；不含工具、脚本或 plugin。
- `prompts/`：保留可信公共质量规则、输入模板及本次不开放修改的其他阶段提示词。

基线随 wheel/sdist 发布。不指定目录时自动使用随包基线；需要实验时，复制整个 `baseline-v1` 目录到独立位置再编辑。不会搜索父目录、用户目录或自动混入其他配置。

## 运行

原命令无需修改。自定义策略使用：

```powershell
bounded-investigator run testdata/packets/J00.md --config config.yaml --harness-dir D:/experiments/harness-v2 --output D:/experiments/run-01/J00.json
bounded-investigator batch testdata/packets --config config.yaml --harness-dir D:/experiments/harness-v2 --output-dir D:/experiments/run-02
```

也可在 YAML 配置中设置 `harness_dir: ./my-harness`，相对路径按配置文件目录解析；命令行覆盖值按当前目录解析。新候选使用新输出目录；原命令里的模型与预算配置继续生效，不由角色文件覆盖。

程序调用可使用 `load_harness(path)` 获取不可变快照，再传给 `BoundedInvestigationWorkflow(config, harness=snapshot)`。整个批次在启动时只读取一次，所有包与全局工作流共享同一对象；运行中修改原目录不会改变已启动批次。

## 增加技能

最初提取的 `skills/packet-fact-boundary/SKILL.md`：

```markdown
---
name: packet-fact-boundary
description: 复核时区分当前材料包的新事实与依赖交接中的已确认上游事实
roles: [reviewer]
---

最重要的职责边界：你审查的是当前 <material> 的新行为，不是从头重述整个样本。
<dependency_handoffs> 中已经确认的上游事实只作上下文；如果 Analyzer 没有在本包重复报告上游的凭据写盘、互斥体、Hook 等事实，不是漏报，更不得为此触发 Reviser。
```

这两行原文从 Reviewer 角色说明中原样提取，属于分析方法，不是新增 PE 映射、凭据读取等领域知识。`agents/reviewer.md` 在原位置用独占一行的引用加载：

```text
{{skill:packet-fact-boundary}}
```

加载器把该行替换为技能正文，不增加标题、说明或额外空行。角色身份、其他证据要求、问题严重程度、修订触发条件和输出契约保留原位。技能只进入 roles 指定角色；当前允许 analyzer/reviewer/reviser，name 必须与目录一致。这里的 roles 和引用语法是本项目契约，不声称可直接作为 OpenCode 通用技能使用。

每个角色中显式引用的技能只展开一次；未显式引用、但分配给该角色的技能仍按目录名字典序追加，兼容原有自定义 harness。引用必须独占一行并使用上述格式；缺失技能、跨角色引用、同一角色重复引用、技能内嵌套引用均在调用模型前报错。共享策略不支持技能引用。没有自动触发行、自主检索或额外模型调用。

角色文件的 frontmatter 只允许 `name: analyzer` 等对应角色名。`harness.yaml` 只允许 `format_version: 1` 与小写连字符形式的 `version`。共享策略 → 展开引用后的角色定义 → 未引用的匹配技能 → 可信公共规则 → Reviewer/Reviser 固定修订策略，按此顺序组合。

## 校验与追溯

加载器拒绝未支持文件、链接/junction、缺角色、空技能正文、非法元数据、超长文件与过多文件。当前上限为 64 个文件、单文件 64 KiB、总计 256 KiB；不会静默截断。固定证据规则和修订策略由包内可信代码提供，候选目录不能替换。自然语言策略仍可能与规则冲突；目录校验不等于语义质量保证。

批次根目录及实际工作流的 artifact 目录写入 `harness_snapshot.json`，包含版本、来源、候选文件全文、可信规则、实际组合指令和 SHA-256。指纹包括候选内容与可信规则，不含绝对路径；模型/预算仍由现有配置及输出记录承担，不把该指纹冒称完整实验版本。

已有快照与本次内容/来源不同或被修改时拒绝继续，避免混用结果。快照不保存 API key；其中包含分析策略文本，请按分析产物管理。

首批技能提取保留 `baseline-v1` 版本名。当时三角色展开后的最终指令与提取前逐字相同，但技能文件和引用改变了源文件组织，因此内容指纹和快照会变化。“指令相同”不等于“快照相同”；后续有意更新的指令见下面按日期记录，新运行使用新输出目录，不覆盖或复用旧运行目录。

## 2026-10-27：引用契约与控制流条件

公共质量规则与 `EvidenceRef.location` 字段说明统一为 `F-XXXX Lx[-Ly]`，例如 `F-0483 L122-L153`。函数名写在观察说明中，不能插在编号和行段之间。依赖引用复用交接中的原始源码位置，不写 `dependency J00 summary` 等摘要标签。claims 原有位置交接保留，完整/部分结果的 artifacts 现在也携带原始证据位置。未提供原位置的依赖语义只能作上下文，不能编造证据。

`control-flow-conditions` 由程序在 Analyzer 和 Reviewer 角色中的显式位置展开；不进入 Reviser。它要求从实际调用点确认包围条件和循环，区分调用、成功返回、结果使用，避免把后续路径检查反套到前面的调用或把循环外调用写成逐条执行。Reviewer 的条件修正需同时引用调用及外层控制流。现有 `packet-fact-boundary` 技能原文未改。

三角色都因公共规则有意变更，Analyzer/Reviewer 另增加控制流技能。历史角色预期与 2026-10-26 Reviser 夹具原文保留；历史复现使用冻结的角色和可信规则，当前预期独立保存在 `tests/fixtures/role_instructions_20261027.json`。`baseline-v1` 名称保留，本轮内容指纹为 `c2ff23920a5645b454810cf3ceea364d39a49f42b538a22d3b3c206f6458c28d`。

修订权限、严格引用解析、合并保护、失败回退、预算和分析结果结构 11.0 均未改变。`EvidenceRef` 只更新了字段说明，字段结构没有变化。新的付费分析必须使用新输出目录，不覆盖 `results/`、旧快照或旧 HTML。离线检查能证明资源加载和执行契约，不能证明实际模型会正确遵循新的分析方法；真实效果待后续重跑确认。

## 2026-10-26：Reviser 完整输出约束

本次在原 `baseline-v1` 目录和版本名下有意更新 Reviser 指令：重大包内问题仍作为修订触发条件，触发后执行程序计划中全部已授权操作，包括明确附带 fixes 的 minor 问题。Reviser 必须返回完整 `InvestigationResult`；可以精简文字，不能省略、合并、重排或用新条目顶替未经删除授权的原条目。Analyzer、Reviewer、分析方法 skill、可信公共规则和修订权限不变。

修订输入除原有 replace/delete/append 路径外，按五个列表给出原有、删除、新增及最终数量，并逐项标明原始槽位到输出位置的对应关系。原始路径始终使用 Analyzer 的零基编号；删除只压缩最终输出位置，新增授权仍按原长度槽位解释，再追加到保留条目的末尾。例如 J07 的 claims 为原有 3、删除 0、新增 2，最终必须为 5，原来的 SID 事实必须保留；J11 为原有 2、新增 1，最终必须为 3，不能用修正项和新增项顶替 NSS 加载事实。

这些说明直接来自现有 `RevisionPlan`，不额外维护权限表，不增加模型请求、重试或预算。长度、范围和证据保护仍由原合并逻辑执行；不合规候选仍被拒绝，失败回退不变。旧三角色指令夹具保留用于历史重建，当前 Reviser 使用独立的新预期夹具；此前“指令逐字相同”的说明仅指技能提取时的变化。

本次指令变更会产生新的 harness 指纹。新运行必须使用新的输出目录，例如 `results_reviser_check`；不要覆盖原 `results`、复制旧快照到新运行或删除旧快照来强行继续。已有 J07/J11、全局结果及 HTML 不会自动改变，需要后续明确发起新分析并重新整合、生成报告。离线回归只证明契约与保护正确，不证明模型已在真实重跑中遵循新指令。

## 验证与范围

### 独立报告角色与 HTML skill

`report-html` 的报告角色位于 `reporting/agents/report.md`，通过独占一行的 `{{skill:malware-report-html}}` 引用 `reporting/skills/malware-report-html/SKILL.md`，技能元数据为 `roles: [report]`。程序在构建 `ReportAgent` 前确定性读取技能及其 `references/blocks.md`；缺失资源、错误角色或重复引用会提前报错。这不是向调查 harness 增加第四个角色，`harness_dir` 也不参与报告资源的选择。

报告角色负责整体理解、目的聚类、全部行为呈现和重点排序；HTML skill 提供表达选择准则及事实绑定、模板校验和渲染能力。一次报告模型请求同时使用展开后的角色与 skill 指令；没有第二个报告 Agent、自主技能检索或个人 skill 安装。HTML skill 复用已随包提供的唯一 `html_template/` 实现，也可以通过程序接口独立渲染已有模板数据。

报告角色和 skill 均用 `metadata.version` 声明版本。运行的 attempt 目录保存 `report_agent_snapshot.json`，记录报告角色、skill 与加载说明原文、`expanded_instructions` 以及包含 native/prompted 输出适配的实际 `instructions` 和指纹。manifest 的 `report_agent` 记录名称、版本、指纹，`html_skill` 同时记录模板版本与模板资源指纹。调查的 `harness_snapshot.json`、三角色指令与指纹不因该拆分变化。报告快照用于记录实际指令，不额外建立行为取舍记录或内容审计阶段。报告数据及失败处理见 [HTML 报告说明](html-report.md)。

### 调查 harness 回归

固定的旧指令夹具用于逐字验证三角色基线，提取技能时不修改夹具。另验证原位展开、技能隔离/排序、显式引用与追加加载并存、非法引用、运行中修改源文件、工作流 schema、配置相对路径、非法内容及快照篡改。Windows 下链接创建可能因权限跳过，需在具备权限的环境补验。逐字一致只证明本次未改变指令，不代表分析质量提升；本次不调用真实模型。

代码清理后，`workflow.py` 组合 `execution/stage_runner.py` 执行模型阶段；批次调度、材料索引和报告分别在 `orchestration/`、`evidence/`、`reporting/`。harness 加载器、资源路径、角色技能、可信规则与内容指纹不因这些 Python 模块搬迁而改变。旧 Python 导入路径不提供转发模块。

软件包版本仍为 `1.7.3`，保存结果结构版本为 `11.0`；旧最终评估与 Gap Reconciliation 的模型流程、配置和历史 JSON 兼容代码已删除。配置顶层拒绝未知字段，旧配置应改用当前示例。模型输出 schema 与角色指令不随保存封套清理而收紧。

harness2 适配器、完整事件轨迹、两层发布门槛、审计 `not_run` 缺陷修复、Linux 培养环境和模型质量实验仍按接入方案另行实施。没有材料时仍可能得到 `not_run/passed=true`，本次整理未修复这一问题，也不构成上线批准。
