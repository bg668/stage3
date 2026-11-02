# 单独验证全局整合

脚本 `scripts/verify_global.py` 只读取已有动态探索结果、调用全局整合、生成结构化结果与 Markdown。不重新执行入口、规划器、Analyzer、Reviewer 或 Reviser，不生成 HTML。

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe scripts/verify_global.py `
  --source-run results/sample3-explore-20261102-rerun-01
```

默认创建带时间戳的新目录 `results/global-check-*`，不会覆盖源运行。默认读取源运行的 effective_config.json 和项目 .env；只接纳已审阅、未修订失败且引用位置检查通过的结果。若没有合格结果，调用前直接失败。

预检（不构造模型、不发送请求）：

```powershell
.\.venv\Scripts\python.exe scripts/verify_global.py `
  --source-run results/sample3-explore-20261102-rerun-01 `
  --check-only
```

可用 `--output-dir` 指定新目录，用 `--config` / `--env-file` 指定配置。`--total-tokens` 可显式指定独立实验预算；默认取源运行账本的已知总余额，本轮为 29,542 tokens；没有源账本时默认 40,000。使用独立账本，不续写源账本。历史失败请求的未知用量会在 summary.json 中保留，不能将已知余额理解为精确实际余额。

所有本次全局模型及格式修复请求都受该预算约束；SDK 自动重试为 0，其他角色的构造不会发起请求。预算在请求边界检查，最后一项被接受的请求可能跨线。模型与 harness 使用原配置，harness 指纹若与原运行不一致会在调用前失败。

主要产物：

- `summary.json`：成功/失败状态、接纳和排除任务、来源哈希、成本及历史未知用量。
- `error.json`：失败时保存脱敏的异常链、底层 API 错误体、HTTP 状态与请求 ID（供应商若提供）。不输出 Authorization 或密钥。
- `budget.json`、`application.log`、`_global_artifacts/`：本次请求记录与原始输出。
- `_global_reconciliation.json`、`_final_report.md`：有可交付整合结果时生成；报告保留原探索范围，整合成功不代表全样本分析完成。
- `input/`、`effective_config.json`、`exploration_scope.json`：本次输入和配置快照。

退出码：预检通过或整合成功为 0；预检错误、模型异常或未完整交付为 1。以 summary.json 的 status 和 delivery_status 为准；原始输出出现 JSON 不等于验收成功。
