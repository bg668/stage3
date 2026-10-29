# 报告 Agent 与 HTML 生成 skill

## 目标与边界

将现有 `report-html` 拆为一个报告 Agent 和一个由程序加载的 HTML 生成 skill。使用一次报告模型请求，沿用 `StageRunner`、独立预算、格式修复及原模板；不增加调查模型请求、第二个报告 Agent、自主技能发现或个人 skill 安装。

现有 `report/assess`、batch、reconcile、调查与全局结果 schema `11.0`、A/R/R 指令、harness 指纹和审计门禁保持不变。开发验收使用固定模型响应，不自动调用真实模型。

## 职责与数据流

```text
全局事实与材料
  → ReportAgent 加载 report 角色与 HTML skill
  → ReportComposition
  → _report_document.json
  → HtmlReportSkill.compile：原始事实绑定
  → HtmlReportSkill.render：原模板校验与渲染
  → 模板 JSON、HTML、manifest
```

报告角色在 `reporting/agents/report.md`；技能在 `reporting/skills/malware-report-html/`，包含 `SKILL.md` 与组件说明 `references/blocks.md`。角色以独占一行的 `{{skill:malware-report-html}}` 引用技能。加载器在模型构建前拒绝缺失资源、角色不匹配和重复引用，保存独立的 `report_agent_snapshot.json`。HTML skill 只复用现有 `html_template/`，不复制 CSS 或渲染实现。

`ReportAgent.compose(...)` 返回 `ReportComposition`；`HtmlReportSkill.instructions()` 提供技能及组件说明，`compile(...)` 返回模板数据和测试填充清单，`render(data)` 独立校验并生成 HTML。报告使用独立角色入口，不向调查 harness 增加角色。

## 内容与展示规则

- 报告先给整体画像，再按样本目的组织机制。全部行为都进入正文，包括未确认行为；重要性决定顺序、篇幅和强调方式。
- 章节解释触发条件、数据流、控制关系及辅助机制。归并解释保留各行为独有条件、对象与影响；未知用途不强行解释成攻击目的。
- 未确认内容在所属机制中准确表述，不能升格。目的、归因与顺序均不得超出事实，材料包 DAG 不作执行链。
- 模型按阅读任务选择有序的段落、列表、键值、卡片、精简表格、步骤、提示块与代码。文字解释因果，表格比较同类项，代码支撑具体判断，不固定拼接行为表与全部源码。
- 程序按内部引用取回原始对象和代码；正文不显示证据等级、confidence、来源 ID 等管理字段，保留必要的检索标识、函数及行段。
- 不新增行为取舍台账、排除理由或内容审计阶段。已有分析记录继续保留。

## 产物与失败处理

保留一次 `report_composition` 请求、最大输出 16384 tokens、总预算 64000 tokens、超时 300 秒；格式修复独立计量。

每次运行清理本输出目录的旧 `_report_document.json`、HTML 及模板 JSON。报告 Agent 成功后保存本次有效文档，不另生成 Markdown。HTML 编译、校验、渲染或完成 manifest 写入失败时保留有效文档和诊断，撤去 HTML 与模板 JSON，记录失败；成功后原子发布。

manifest 记录报告角色及 skill 的版本与指纹。补充上下文和固定测试填充沿用现有约定，测试数据不补造行为、不覆盖事实或显式空值、不写回输入。原模板 CSS、HTML 结构、目录、布局与组件样式不变，仅修正超出证据的行为链说明文案。

## 验收

固定响应覆盖角色/skill 隔离、全部行为与未确认行为、目的聚类、多组件表达、字段与片段合法性、原值保真、独立渲染、格式修复、失败产物清理及现有命令回归。原模板示例按字比较，除明确批准的文案外不应变化，CSS 按字节相同。

wheel/sdist 应包含角色、skill、组件说明及模板并可隔离加载。交付完整合成预览、结构化报告、模板 JSON 和 manifest。实际通过数量及视觉验收状态只依据执行记录汇报；浏览器策略拒绝时不绕过。
