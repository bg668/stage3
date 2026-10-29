# 报告单条遗漏修复与本次HTML恢复

2026-10-27 已完成。实际失败发生于 batch 报告阶段：58项分析行为中，G:C037未进入正文。它描述Credential Manager解密输出的结构化字段提取，与IE的User/Password处理不是同一条路径；原完整性校验判断正确，源码审计通过。

## 实现

- 完整性错误使用 MissingReportBehaviors，仍为 ValueError，原错误文字及严格校验保留。
- 报告Agent只处理单条遗漏，要求与已引用事实来自同一个来源包、源码行段有重叠，且唯一归属现有章节；不是按相似词、函数列表或材料DAG推断关系。
- 追加原始行为与条件，不只附加ID。若引用锚点只有综合callout，需有唯一相关事实，并保留该事实的行为、条件及确定程度，避免把补入内容误归到相邻机制。无法安全确定位置、多项遗漏及其他校验错误仍失败。
- 不增加模型调用、格式重试或独立Agent。原模型响应不改，生效结果另存coverage_recovered_result.json，再完整校验和渲染；manifest记录补全ID及产物。
- 不修改调查schema、harness/角色/skill、模板、预算、修订保护或审计门禁。

## 本次运行恢复

- [实际HTML](../../results/html_report/_final_report.html)
- [生效报告文档](../../results/html_report/_report_document.json)
- [模板数据](../../results/html_report/_html_report.json)
- [运行与恢复记录](../../results/html_report/_html_report_manifest.json)

使用原已保存响应和原事实离线补全G:C037，58项行为通过正文覆盖；模板无错误，锚点无失效，HTML内嵌JSON与模板数据一致。新增模型请求为0，不重跑调查或全局整合。原失败manifest和DAG summary另存原attempt诊断；DAG仅更新HTML状态，其余分析字段保持原样。原模型响应、源码审计、分析事实和其他既有产物未改。

## 验证

- 修改前433 passed / 1 skipped；13项新增回归先复现遗漏与上下文保真问题，再逐项修复。
- 工作区完整446 passed / 1 skipped；跳过项为Windows符号链接权限。
- wheel和sdist在工作区外分别导入42模块，用冻结的58行为失败案例完成单条恢复、严格校验和HTML渲染，模型请求0。
- 隔离sdist完整446 passed / 1 skipped，原因相同。
- requesting-code-review发现callout锚点必须保留原条件和确定程度，已补红绿回归并修复；复核确认无其他重要问题。
- 修改前292个制品中，290个原文件校验和未变；仅现有HTML manifest和DAG的HTML状态有意更新，恢复前版本保存在诊断中。模板、预算、分析schema、harness与合并保护保持原样。

这项程序保护不能替代全文语义复核；不是任意遗漏都自动放行。本次未重新调用真实分析/报告模型，也未进行浏览器视觉验收。
