# 原模板 HTML 报告预览接入记录

## 设计与边界

新增独立的 `report-html` 命令，消费已通过来源校验的全局封套。只读报告编排模型组织摘要、章节、风险、行为链和建议，程序依据原有 claim/artifact ID 取回事实，使用 `malware-report-html.skill` 原校验器、渲染器和 CSS 生成单文件 HTML。

保留现有 report/assess 的离线行为，以及 batch、reconcile、A/R/R、harness 和保存结果结构 `11.0`。不修改事实、原审计或发布门禁。模板布局、固定模块、目录、指标卡、行为链和空状态设计不变，内容采用简洁表述。

补充上下文仅接收样本元信息、哈希、ATT&CK、归因和 YARA。尚未接入的数据使用明显标注的固定测试值并逐字段记录，显式空值不回填；已有事实和确定为空的 IOC 不注入测试内容。测试填充只存在于 HTML 预览数据，不进入 canonical result。

## 实现分工

- `reporting/composition.py`：编排模型契约、输入投影与事实引用检查。
- `reporting/html_report.py`：上下文、模板数据组装、独立预算与产物发布。
- `reporting/html_template/`：随包发布的模板资源、校验器和渲染器，保持原 CSS。
- CLI 与配置：独立命令及 `report_composition` 阶段，默认复用全局模型配置。

## 实现补充

- 缺少材料目录时可加载保存的材料索引；只有旧 `_report_ready.json` 的 canonical claims/artifacts 逐项匹配当前事实且片段引用合法时，才复用其源码片段。
- 上下文在模型调用前使用原模板校验器预检，拒绝未知字段与显式非法空值；无效上下文不消耗编排请求。
- 输出目录不与全局输入父目录、显式单包结果目录或材料目录重合。完成 manifest 写入与 HTML/JSON 发布处于同一失败清理范围，完成记录写入失败也撤去预览。

## 验收项目

使用固定模型响应测试字段映射、上下文与测试填充、显式空值、非法引用、修复和失败清理；检查模板 CSS、结构及源码/IOC/证据保真。运行既有回归和包资源隔离加载检查。开发验收不调用真实模型。

交付 `_html_report.json`、`_final_report.html`、`_html_report_manifest.json` 和 `_html_report_artifacts/`。若浏览器安全策略阻止本地打开，明确记录未视觉验收，不绕过限制。

演示交付路径为 [HTML](../../examples/html-preview/_final_report.html)、[报告 JSON](../../examples/html-preview/_html_report.json) 和 [manifest](../../examples/html-preview/_html_report_manifest.json)。整个演示样本、证据及源码均为合成数据，使用固定编排响应，零真实模型调用；它用于检验接入与模板，不能当作真实样本结论。

## 验收结果

2026-10-23 完整离线测试 **305 passed / 1 skipped**。跳过项是 Windows 未授权创建符号链接；新增 HTML 相关 58 项均通过。涵盖短/完整原模板比较、CSS 字节一致、内嵌 JSON 与转义、原字段/源码保真、显式空值、上下文预检、格式修复、超时/取消及发布失败清理。通用提醒过滤另有混合句回归，确保具体限制保留。

三角色最终指令、现有三个模型输出 schema、harness 指纹、旧夹具以及六个原命令参数与开发前快照完全一致。结果结构继续为 `11.0`，软件包版本继续为 `1.7.3`。本地和示例配置仅增加 HTML 阶段与独立预算。

源码包与 wheel 均离线构建成功，并在工作区之外分别隔离导入 40 个模块；模板 CSS/渲染器/校验器、测试数据、编排提示词及原有技能资源均可用。七个命令入口通过，安装包内实际 `report-html` 使用固定响应生成预览成功。未请求真实模型。

交付预览为 37,953 字节，含 3 个合成行为、2 个机制章节及 12 项外部模块字段测试填充；manifest 记录 0 次真实模型请求。浏览器安全策略曾拒绝 `file://`，本次未进行浏览器视觉验收，也未以其他通道绕过。

本轮基线、离线验证摘要与构建包保存于系统临时目录 `html-report-baseline-9ghtvxpu`。此前未提交的重组与 harness 工作保留，未创建提交或发布。
