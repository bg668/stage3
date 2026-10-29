# 第三阶段源码架构与处理流程

第三阶段消费已导出的材料包和依赖清单。正常路径是：DAG 调度 → 每包 Analyzer / Reviewer / 条件 Reviser → 全局事实关系整合 → 程序报告。第一、第二阶段的材料生产不在本目录实现。

## 从哪里开始读

先看根级 `cli.py` 的命令，再看 `workflow.py` 的单包和全局编排；随后沿下表进入具体实现。路径均相对于 `src/bounded_investigator/`。

| 位置 | 职责 |
| --- | --- |
| `cli.py`、`__main__.py` | 命令与参数适配、加载配置、初始化日志、调用业务入口 |
| `config.py`、`schemas.py` | 当前配置，以及调查/全局结果的结构化数据契约 |
| `workflow.py` | 单包 A/R/R 和全局整合编排，组合 StageRunner 执行阶段 |
| `execution/` | 模型与网关适配、阶段运行、流式捕获、格式修复、部分结果保全、用量与阶段产物 |
| `investigation/` | 制品规范化、Reviewer 职责约束、修订计划与受控合并 |
| `evidence/` | 材料范围、清单与补料目标核对、证据引用检查、源码索引和片段提取 |
| `reconciliation.py` | 程序事实归并、模型关系决策投影、canonical 组装、来源与覆盖校验 |
| `reporting/` | 三层知识投影、补料建议、报告保存与离线重建；独立报告 Agent、HTML skill 与原模板渲染 |
| `orchestration/` | manifest/DAG、独立及依赖批处理、已保存 Worker 结果的全局重放 |
| `harness/`、`harnesses/`、`prompts.py`、`prompts/` | 角色技能加载、不可变策略快照、可信规则和输入模板组合 |
| `io.py`、`logging_utils.py` | 包读写、错误文件和日志 |

入口负责调用，不承担分析规则。阶段执行的复杂性集中在 `execution/stage_runner.py`，报告保存集中在 `reporting/service.py`，避免每种调用路径各自实现。包内模块通过直接函数调用组合，不使用插件框架、继承式工作流或旧路径转发模块。

## 调用方向与正常路径

1. `run` 加载配置与 harness 快照，调用工作流处理一个包。`batch` 交给调度器；有 manifest 时按依赖启动 Worker，无 manifest 时执行独立批处理。
2. 工作流为 Analyzer、Reviewer、Reviser 组装相同的依赖上下文。`StageRunner` 接收阶段 Agent、提示词、预算、用量状态、修复状态和产物目录，执行模型并保存阶段结果。Reviewer 仅审查当前包新事实；重大且可修的问题才触发受控 Reviser。
3. 依赖调度只传已验证事实的紧凑摘要，不传父包完整结果或未经验证的输出。独立分支受批次并发上限约束。
4. DAG 完成且启用全局整合时，程序先精确归并 claim/artifact。Global Resolver 仅返回 ID 关系决策，程序据此组装 canonical facts，再校验来源与覆盖。
5. 报告仅消费通过来源校验的 canonical facts。程序按显式证据等级分层，记录每条来源的去向、调查缺口、材料建议和引用审计；不调用最终评估模型。

`reconcile` 从已保存的 Worker 封套重新运行全局整合和报告，会调用全局模型，但不重跑 Worker。`report` 从已保存的全局封套重建报告，无模型调用；`assess` 是同一功能的命令别名。它们使用同一报告保存实现。

## 独立 HTML 预览路径

`report-html` 读取已保存且来源归并校验有效的全局封套，不运行 Worker 或重新整合事实。调用方向为：`cli.py` → `reporting/html_report.py` → `reporting/agent.py` 的 `ReportAgent` → `reporting/html_skill.py` 的 `HtmlReportSkill` → `reporting/html_template/`。入口管理输入、预算、诊断和发布；报告 Agent 借助 `StageRunner` 生成 `ReportComposition`；HTML skill 绑定原始事实并校验、渲染模板。

batch 的命令编排 `run_batch()` 默认在 `run_batch_dag()` 完成全局整合和原有报告保存后，复用同一个 `create_html_report()`，输出到批次目录下的 `html_report/`。报告读取本批次保存的 scope/inventory，实际包列表通过可选 `packet_paths` 传入投影和源码片段提取，保留自定义 pattern、manifest 范围与补充材料。`--no-html-report` 可关闭，`--context-file` 透传补充上下文；没有有效全局结果时明确跳过。HTML 错误在全局分析流程之外处理，保留分析结果，写入 `_dag_summary.json.html_report` 后向命令返回失败。清理旧 HTML 发布文件由报告模块统一实现，跳过时也不残留旧成功页面。

报告角色定义在 `reporting/agents/report.md`，以独占一行的 `{{skill:malware-report-html}}` 引用 `reporting/skills/malware-report-html/SKILL.md`，程序同时加载该技能的组件说明 `references/blocks.md`。加载在模型构建前完成，缺失资源、错误角色或重复引用提前拒绝。报告角色使用独立入口，不扩大 A/R/R harness 的角色集合，不安装个人 skill 或增加自主检索。skill 复用现有 `html_template/`，没有第二份模板。

报告 Agent 只接收已有事实投影，负责整体画像、目的聚类、风险研判、建议和正文组织。全部发现的行为都进入相应机制，包括尚未确认的行为；重要性决定顺序与篇幅。章节解释触发条件、数据流、控制关系和辅助行为作用；合并解释不丢失独有条件，未知用途不强行归类。不会新增取舍台账或独立内容审计阶段。

报告角色 1.2 先生成首稿，`composition.py` 一次收集现有契约的全部问题；失败时由同一角色返回 `revision.py` 定义的 `ReportRevisionPatch`，只替换获授权章节/字段或追加必要机制章节。程序保留其余内容，拒绝丢失原行为、越权和只附加遗漏 ID 的补丁，完整复检后才保存有效文档。正常一次、失败最多两次语义请求，复用同一阶段配置与总期限；格式修复共用独立额度。`display.py` 统一校验与编译的字段绑定口径，HTML skill 不承担语义补写。旧单项遗漏程序补全已移除。详细输入、产物与失败状态见 [HTML 报告说明](html-report.md)。

模型输出有序且类型受限的段落、列表、键值、卡片、比较表、步骤、提示块和代码等内容块，通过内部事实/片段引用选择已有数据。HTML skill 的程序接口按 ID 取回行为、IOC、证据及源码，模型不能替换原始字段。正文隐藏内部证据等级、confidence、来源 ID 等管理信息，保留必要的检索标识和代码位置。不再固定拼接全字段行为表、制品表与全部源码。行为链来自行为与证据关系，不使用包依赖 DAG 冒充执行顺序。引用合法与语义正确是不同检查，仍需分析人员复核。

未提供材料目录时，可加载保存的材料索引；旧 `_report_ready.json` 的源码片段只在 canonical claims/artifacts 与当前事实逐项匹配且片段引用合法时复用，否则记录告警并跳过。上下文在模型调用前使用原模板校验器预检，未知字段和非法空值不会消耗模型请求。

模型阶段 `report_composition` 在示例配置中复用全局整合的模型配置，默认使用独立的单次请求、64000 tokens 和 300 秒预算；最大输出 16384 tokens，实际预算按配置执行。独立报告命令和默认启用 HTML 的 DAG batch 要求配置该阶段。阶段执行和格式修复复用现有执行器，修复另行计量，不增加第二个模型 Agent。开始预览时清除旧报告文档、HTML 和模板 JSON。报告 Agent 成功后保存本次有效的 `_report_document.json`；后续 HTML 或完成 manifest 写入失败时保留该文档和本次诊断，撤去最终 HTML 与模板 JSON。HTML 文件采用原子替换。

补充上下文只提供样本身份和外部资料；未接入的补充模块允许固定测试数据填充，明确标记并逐字段记入 manifest。显式空值不回填，已确定为空的 IOC 不制造替代值。输入事实、现有报告与审计产物保持只读。HTML 输出目录不得与全局输入目录及显式单包结果/材料目录重合，模板 CSS、布局和模块保持原 skill 设计。完整约定及合成演示链接见 [HTML 报告说明](html-report.md)。

## 失败与降级

- 结构化校验失败时先保留原始输出。允许的格式问题进入干净上下文的 Format Repair；必需字段缺失不由 Repair 补造事实。修复失败时只逐项保留通过原 schema 校验的 claim/artifact，否则保留 raw-only 产物。
- Reviewer 格式失败保留已验证 Analyzer 结果；Reviser 失败或修订被保护规则拒绝时保留原稿，并记录失败或拒绝原因。模型/API 执行异常写入错误文件。
- 父包失败不取消整条分支；子包收到明确的依赖不可用状态。partial 只传已验证条目，raw-only 不传事实。
- Global Resolver 的结构化输出不可用时，程序保守保留 reduced facts 并记录未完成的语义整合；来源与覆盖校验继续执行。来源校验不通过不生成可交付知识报告。
- 引用审计的已知缺陷仍在：没有材料时可能得到 `not_run/passed=true`。它不等于证据已经核验；本次重组没有修复此问题或增加发布门禁。

## 主要产物

| 产物 | 用途 |
| --- | --- |
| `Jxx.json` / `Jxx.error.json` | Worker 结果封套 / 执行错误 |
| `_artifacts/Jxx/<stage>/` | raw、validated、partial 输出与校验错误；修复尝试另存于 format_repair 目录 |
| `harness_snapshot.json` | 批次根目录及实际工作流产物目录中的策略来源、原文、组合指令与指纹 |
| `_dag_summary.json` | 依赖、成功失败、耗时和全局处理摘要 |
| `_global_reconciliation.json`、`_global_artifacts/` | 全局封套、输入投影、reducer 摘要、Resolver 决策与 canonical facts |
| `_evidence_inventory.json`、`_evidence_scope.json`、`_material_requests.json` | 材料索引、可用范围和核对后的材料需求 |
| `_report_ready.json`、`_final_report.md` | 三层知识结构与可读报告 |
| `_knowledge_audit.json`、`_evidence_reference_audit.json` | 来源去向/覆盖审计与材料引用审计 |
| `_reconcile_summary.json` / `_report_summary.json` | 全局重放 / 离线报告摘要 |
| `_report_document.json` | 报告 Agent 的有效 `ReportComposition`，仅由 `report-html` 生成，不另生成 Markdown |
| `_html_report.json`、`_final_report.html` | HTML 模板数据与单文件预览，仅由 `report-html` 生成 |
| `_html_report_manifest.json`、`_html_report_artifacts/` | HTML 输入与模板来源、报告角色/skill 版本指纹、测试填充、校验、模型用量及阶段诊断；attempt 下保存 `report_agent_snapshot.json` |

## 修改与验证约定

软件包版本为 `1.7.3`，保存结果结构版本为 `11.0`。旧最终评估与 Gap Reconciliation 的执行、提示词、配置及专用兼容逻辑已删除，不提供历史 JSON 转换器。配置顶层拒绝未知字段；当前模型输出容错与证据保守处理继续保留。

改角色策略与技能先读 [harness 使用说明](harness-runtime.md)；改模型执行看 `execution/`；改材料判断看 `evidence/`；改报告看 `reporting/`。调查与全局结果的 schema 只定义在 `schemas.py`，不要另维护第二份；独立展示契约 `ReportComposition` 定义在 `reporting/composition.py`。角色指令固定夹具、当前 schema 与修订回放测试用于防止整理改变分析语义；完整离线检查命令见 README。

代码职责整理及报告 Agent / HTML skill 接入保持现有 A/R/R harness 资源和指纹不变。报告角色与 skill 有独立指令快照；报告 skill 的程序接口可在不构建 Agent 时独立渲染已有模板数据。没有自主技能检索、主动补料工具或 harness2 适配器；HTML 开发验收使用固定响应，没有进行真实模型质量实验。
