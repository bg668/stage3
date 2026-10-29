# batch 自动生成 HTML

2026-10-27 已实施。沿用现有报告 Agent、HTML skill、模板和预算，不调用真实项目模型做开发验收。

## 行为

- 原 batch 命令在 DAG 调查、全局整合和既有报告保存后，默认自动调用 create_html_report，输出 `<output-dir>/html_report/_final_report.html` 并打印页面路径。
- `--no-html-report` 关闭自动生成；`--context-file` 传递已有样本身份和补充资料。缺少报告阶段配置时，在调查请求前报错。
- 无 manifest、全局整合关闭或没有有效 canonical result 时跳过，日志记录原因；DAG summary 的 html_report 单独记录状态。
- 报告成功时 summary 保存页面、manifest 路径和用量；失败时保留原单包、全局和 Markdown 结果，记录错误和诊断位置，命令非零退出。独立 report-html 保留用于单独重试，report/assess/reconcile 的调用行为不变。
- 自动报告复用本批次保存的合并 scope 和 inventory，实际包列表取自本次 manifest 任务集合，保持自定义 pattern，源码审计和片段提取不再固定重扫 J*.md。
- 新批次只清理4个自有 HTML 发布文件，保留历史 attempt 和无关文件。删除前保护 context、scope、manifest、实际包，以及外部材料目录，避免损坏输入。

## 验证

- 修改前完整基线414通过、1项Windows符号链接权限跳过；修改后工作区433通过、1项同原因跳过。
- batch专用19项：真实生产链路接固定模型响应，覆盖自动生成、关闭、CLI上下文、无效全局结果、HTML失败保全、旧制品清理、输入冲突、自定义pattern、忽略非manifest包、专家scope及外部材料保留。
- 新功能先复现10项失败；目录别名与输入保护、两项兼容问题均补充失败复现后修复。初次测试网络保护误拦Windows asyncio内部唤醒，修正为允许该本机连接，并禁止所有真实Pydantic模型请求。
- requesting-code-review 独立复现自定义pattern、材料范围和额外材料目录输入保护问题；逐项修复后复核通过，未发现新增问题。
- wheel/sdist 在工作区外分别导入42模块，使用固定响应验证自动HTML、关闭后的清理和CLI新参数，真实模型请求为0。
- 隔离sdist的完整测试433通过、1项Windows权限跳过。
- 配置、分析schema、模板、修订保护、工作流和harness指纹不变；严格引用解析器不变，源码片段函数只增加显式包列表入口。
- 既有 results/、html_preview/ 和 examples/ 共286文件校验和均未改变。未自动重跑分析、覆盖已有页面或提交代码。

修改前备份与校验记录位于系统临时目录 `batch-html-20261027-8os69vcr`。真实模型服务的可用性、额度与新一轮报告内容仍以用户实际运行结果为准。
