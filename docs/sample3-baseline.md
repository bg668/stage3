# 样本 3 对照基线

本轮接通保存的第一、二阶段结果与当前阶段三，比较自动材料和原有人工材料。基线重新调用阶段三，不读取历史模型响应；标准答案不进入模型上下文。

## 可重复运行

在项目根目录运行，`--output-dir` 必须不存在，且不得位于输入目录内：

```powershell
.venv/Scripts/python.exe -m bounded_investigator baseline `
  --preprocess reference/examples/out/debug_03/preprocess/preprocess.json `
  --triage reference/examples/out/debug_03/triage/triage.json `
  --manual-dir reference/examples/input/example3_16_test_packets `
  --config config.yaml `
  --output-dir results/sample3-baseline-NEW `
  --total-tokens 200000
```

只生成自动材料，不调用模型：

```powershell
.venv/Scripts/python.exe -m bounded_investigator prepare-packets `
  --preprocess reference/examples/out/debug_03/preprocess/preprocess.json `
  --triage reference/examples/out/debug_03/triage/triage.json `
  --output-dir results/sample3-packets-NEW
```

后续从 SQLite 开始：

```powershell
.venv/Scripts/python.exe -m bounded_investigator preprocess --sqlite reference/examples/input/test_sample3.db --output-dir results/preprocess-NEW
.venv/Scripts/python.exe -m bounded_investigator triage --preprocess results/preprocess-NEW/preprocess.json --llm-config YOUR-REGISTRY.json --output-dir results/triage-NEW
```

第二阶段保留上游 registry JSON 合同，使用 `providers`、`models`、`tasks`；不是当前阶段三的 YAML 配置。上游实现位于 `src/bounded_investigator/upstream/`；参考目录保持只读。

## 材料与执行合同

- 保留第一阶段 PROGRAM_ANALYZE 与第二阶段最终 ANALYZE 的并集。来源指纹、样本身份、判定完整性、唯一性及主体源码缺失均先校验。
- 强连通分量保持完整，按被调用者先行顺序合包；使用保存的 80 函数、12,000 字符限制。超大分量独立保留。直接调用的未保留函数仅作支持材料，最多 8 个、12,000 字符，整函数添加，遗漏记录为材料边界。
- 样本 3 自动组 13 包、100 个主体函数、144,816 主体代码字符；支持材料 29 个唯一函数、104 次出现。人工组原有 16 包，内容和依赖保持不变。
- 每组独立 200,000 输入＋输出 tokens；分析部分最多使用前 160,000，整合使用总余额。格式修复共用所在阶段的预算。基线串行、关闭 HTML 模型编排，保留结构化结果与可生成的确定性 Markdown。
- 预算默认不启用；基线显式启用。逐供应商请求检查，允许最后一次请求跨线；实际超额如实记录。未知用量仅能提供已知消耗下界，不能视为零成本。推理、缓存不在输入输出之外重复加总。
- 完整执行任务比例要求有有效结果、完成审阅、修订未失败且无预算暂停。另行保留有效初稿比例。函数范围表示提供给任务的材料范围，不代表逐函数全部分析完整。

## 产物

`experiment_manifest.json` 冻结输入哈希、有效配置、模型与 harness/代码指纹；两组的 `input/` 和 `results/` 相互独立。`material_mapping.json` 定位主体、支持材料、原文件、地址、符号、指纹和原行号。

每组 `results/budget.json` 记录逐请求、输入、输出、缓存、推理、耗时、超额和未知用量。`_dag_summary.json` 记录任务、依赖不可用原因、预算暂缓、审阅状态及整合状态。`comparison.json` / `comparison.md` 汇总两组；`human_review.md` 保留恶意判定、检测、关联溯源三个目标的待评价表。

两组同时存在函数选择和任务组织差异，不能单独归因于筛选召回率；行为与制品数量不代表准确率。

## 验证

最终离线回归：538 passed、1 skipped。固定模型响应覆盖双组执行、独立失败、单请求跨线后保留初稿、拒绝后不额外调用、格式修复和全局整合共享预算、路径保护及实验指纹变化。真实保存结果验证 100 主体函数唯一覆盖、依赖无环及完整原文映射。

wheel 从 sdist 构建并隔离安装；127 个运行模块导入成功，提示词资源可读取，SQLite → 固定响应 triage → 自动材料链运行成功。启动用 `__main__` 不作为可导入运行模块计数。

路径保护的反向测试曾意外使用示例配置调用外部模型 6 次，共 18,406 tokens；这是合成函数测试，不属于正式对照。已增加禁止外部模型请求的自动测试约束，并保留独立事件记录。

本轮真实产物位于 `results/sample3-baseline-20261030-01/`；实际执行结果以该目录的对照报告和预算账本为准。

## 2026-10-30 首轮真实结果

当前有效配置模型为 `gpt-5.6-luna`。自动组 13 个任务及全局整合共 14 次请求，人工组 16 个任务及全局整合共 17 次请求，全部收到 HTTP 502；两组完整任务完成率均为 0，没有有效行为或制品结果。供应商未返回这 31 次请求的用量，账本中的已知 tokens 下界为 0，不能理解为零实际消耗或零成本。

未生成 `_final_report.md`；引用审计标为 `not_run`。冻结输入、错误、依赖不可用原因、账本、对照报告和人工复核表均已保存。人工材料冻结前后的文件哈希一致。正式实验指纹：`69654bff1b64d69424bf3db5eab2d7578107987ff30e4bd50892a54c6170e5ad`。

代码贯通和离线验收已完成；本轮真实调用证明执行入口可达模型服务，但服务失败，尚不能据此评价筛选、组织方式或阶段三分析效果。恢复当前模型服务后，以新的输出目录重跑上述命令；不得混入本轮失败记录或历史成功响应。

## 2026-10-31 服务恢复后的复验

两组获得有效结果并完成整合，总预算均未超额。自动组 2 包完整完成、1 包保留初稿，消耗 175,276 tokens；人工组 3 包完整完成、1 包保留初稿，消耗 170,353 tokens。其余任务预算暂缓；两组全局状态均为 `complete_with_missing_inputs`。

新产物：`results/sample3-baseline-20261031-01/`。效果、预算及下一轮建议见 [真实基线复验说明](sample3-baseline-verification-20261031.md)。
