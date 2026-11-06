# 有界动态函数探索

`explore` 已实现“入口初析 → 根据结果选择下一问题 → 按需取材 → 分析/审阅/必要修订”的串行循环。静态 `batch` 和 `baseline` 的依赖调度保持原行为。

## 配置与运行

在现有配置的 `stages` 下增加显式规划模型配置；旧命令不要求此阶段：

```yaml
stages:
  # 保留原来的 analyzer / reviewer / reviser 等配置
  planner:
    model: investigator
    max_output_tokens: 1024
exploration:
  max_rounds: 6
```

`investigator` 必须是该配置中已有的模型名称。首个入口来自上游 `is_entry` 元数据，无需规划调用；不存在入口时由规划器选择首项。

```powershell
.venv/Scripts/python.exe -m bounded_investigator explore `
  --preprocess reference/examples/out/debug_03/preprocess/preprocess.json `
  --triage reference/examples/out/debug_03/triage/triage.json `
  --config config.yaml `
  --env-file .env `
  --output-dir results/sample3-explore-NEW `
  --total-tokens 200000
```

输出必须是新目录，不能与来源目录重叠。模型密钥仍从环境变量读取。所有模型请求串行，禁用供应商自动重试；配置、来源、源码、harness、规划提示词和每项任务材料均冻结留档。默认不生成 HTML。

服务配置 `providers.<名称>.min_request_interval_seconds` 默认 `1`，设置为 `0` 可关闭。计时从上一次调用结束开始，流式调用须关闭响应后再等待；调用失败或取消也会进入冷却。同一进程、同一事件循环、同一服务地址的模型共用串行队列，覆盖分析、规划、修复、全局整合及 HTML 编排；不同服务地址互不阻塞。等待计入任务墙钟时间，不产生模型 token 消耗。独立进程之间不共享计时器。

第二阶段独立的模型注册 JSON 也支持 `providers.<名称>.min_request_interval_seconds`，同样默认 1 秒。这里约束 PydanticAI 模型请求，供应商 SDK 内部重试不另设计时器；当前阶段三配置的 `sdk_max_retries: 0` 保证没有隐藏重试绕过间隔。间隔用于降低连续调用密度，不能保证消除代理或上游断连。

## 取材和停止规则

- 保持上游保留集合不变；任务主体是一个函数或不可拆分的递归分量，不再等待原静态包的全部前置依赖。
- 初析只确认当前源码能证明的事实。后续候选来自材料需求、交接、明确函数编号的问题以及尚未访问的保留函数；规划器只能选择程序提供的候选 ID。
- 非保留函数只能作为支持材料，单包最多 8 个、12,000 字符。保留函数原文不截断；来源、原始行号和指纹放在材料映射中。无法提供的支持材料显式记录。
- 同一函数最多作为主体执行两次；重复问题只有材料或相关证据变化才允许重访。规划器不能读取任意路径、扩展预算或自行生成任务。
- 每项仍执行 Analyzer → Reviewer → 必要时 Reviser。只有完成审阅、未修订失败且引用检查通过的任务进入全局事实整合；草稿留档。引用位置有效不等于语义正确。
- 200,000 tokens 中分析阶段上限 160,000，余量供全局整合使用。规划、修复和失败调用均计入同一账本；按请求边界停止，最后一次请求可能跨线。未返回用量的失败标记未知，已知消耗只是下界。
- 规划器停止、无可执行候选、达到轮数、分析预算耗尽或规划失败均结束循环。模型停止不代表业务验收；支持材料被提供也不代表函数已分析完成。

首版不自动读取外部资源，不接入知识库或 harness2。待查问题保留历史出处；后续取材不会自动把原问题改成“已解决”。

## 产物与判断口径

| 路径 | 含义 |
| --- | --- |
| `experiment_manifest.json`、`effective_config.json` | 来源、模型、实现和策略指纹；本次循环状态 |
| `source/`、`source_catalog.json` | 冻结的上游输出、源码和固定候选集合 |
| `input/J*.md`、`input/material_mapping.json` | 实际供给源码及原始行映射 |
| `results/J*.json`、`results/_artifacts/` | 单任务结果、审阅和修订记录 |
| `results/_planning/` | 每轮候选、上下文、模型决策或失败 |
| `results/budget.json`、`results/exploration_events.jsonl` | 逐请求成本、选择和停止原因 |
| `results/exploration_scope.json` | 已供给、已审阅、草稿、支持、未访问、重访和待查问题 |
| `results/_global_reconciliation.json`、`results/_final_report.md` | 有可整合事实时产生；报告保留实际探索范围 |

`experiment_manifest.status=finished` 只说明循环退出，需同时查看 `stop_reason`、任务状态和全局整合状态。保存结果的报告重建与回放会保留探索范围；识别为动态运行但范围文件缺失时明确标为未知。

不要以结论数量或任务数量代替准确率。业务验收仍需检查恶意判定、检测、关联溯源三目标中的遗漏、错误确认、条件/数据流和指标价值。

## 2026-11-02 验证状态

- 完整回归：576 项通过、1 项跳过。覆盖入口优先、结果驱动候选、未知目标拒绝、重访限制、非保留支持材料、预算跨线、草稿隔离、报告与回放范围保留。
- 从源码分发包构建 wheel，并安装至独立目录；130 个模块可加载，上游提示词及规划提示词可从安装包读取。
- 样本 3 真实运行记录：`results/sample3-explore-20261102-01`。固定保留集合 100 个，首项直接选择入口 F-0500，完整供给 6,340 字符源码，没有等待原 J12 的 7 个依赖包。
- 本机模型服务两次返回 HTTP 502，分别发生于入口分析和后续规划；两次用量未知。没有审阅结果，未运行全局整合，99 个函数未作为主体访问。真实的“分析结果 → AI 后续选择”闭环尚未验收；固定响应测试已覆盖该闭环。
- 历史静态基线与此次动态失败运行不具备效果比较条件。不能据此声称更省 tokens 或更准确；模型服务恢复后用新目录重跑本命令，再进行三目标人工复核。

### 模型服务开启后的复验

新目录 `results/sample3-explore-20261102-02`：入口 F-0500 已完成分析、审阅及修订，最终 24 条引用位置检查通过，保留 5 条行为结论和 11 条制品记录。入口结果驱动程序生成 107 项候选，其中 9 项来自实际结果；但规划调用返回服务器过载，全局整合连接超时，尚无真实后续选择或全局报告。3 次成功请求已知消耗 44,278 tokens，另有 2 次失败请求用量未知，99 个函数未作为主体访问。详见该目录 `comparison.md`、`implementation_verification.json`；不能用提前失败造成的低消耗证明策略收益。

### 清理 results 后的新一轮测试

当前记录为 `results/sample3-explore-20261102-rerun-01`，此前 results 已由用户清理。本轮复用 reference 中前两阶段结果，完成 F-0500 → AI 选择 F-0483 → F-0483 分析的真实闭环，两项任务均完成审阅及修订。第二次规划选择重访 F-0500，但该请求完成后分析用量达到 170,458，跨过 160,000 上限 10,458 tokens，程序没有启动第三项任务；98 个函数预算暂缓。全局整合请求断连，1 次用量未知，未生成最终报告。两次规划共消耗 78,185 tokens。来源、718 行完整映射、引用及单任务与预算账本一致性核查通过，业务评价仍待人工复核。详见新目录 README.md 与 implementation_verification.json。
