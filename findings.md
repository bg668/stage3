# 梳理依据

## 2026-10-21 harness2 v2 工程反馈复核
- 已逐项复核本会话中读取的固定版本源码：selection._render_pool 会过滤未交付候选，k=1 不运行选择模型；流程门槛应与证据质量分离，后者不能只寄希望于选择器。
- prompts._SURFACE_TEXTS 为模块级私有注册表，注册 native 在该固定版本可行，但必须同时替换 surface_md/inv/dom/tts_surface，不能只换文件面标题而继承 OpenCode 工具语义。
- recursion._accept_tree 在 validate_tree 前执行 autotrigger；新增 skill 的触发行必须进入受支持的可编辑常驻文件，不能写入冻结 guardrails。
- schedule.run_task 每次读取 base_harness，k 个基线不是独立于 k 的波动测量；需要外层基线重复与候选复验，不能把一次 helped 判定当因果证据。
- 隔离加载固定源码的提示/树校验函数已通过 7 项检查：native adapt 的 INV/DOM/aggregate/sequential、角色 skill 基线、领域 skill 自动触发、tts 仍有硬编码脚本指引、BenchPrompts 禁词、闭合子图契约。记录于 docs/harness2-v2-review-checks.json；未安装或执行完整 harness2。
- 新方案选择 native 注册与按角色可扩展策略 skill；固定 adapt，因 tts 的 _TTS_INV 不随注册条目替换。注册私有接口必须锁版本并做兼容检查。
- 基线 3 次与最终候选 3 次仅作初步波动观察；k=1 没有选择模型，质量指标进入 deliverable_text 不能替代外层复验。单任务 helped 也不能代替跨独立样本收益证据。
- 闭合子图 J04 的祖先为 J00/J01，三包 manifest 通过当前 DagManifest 校验；未运行模型分析，不能据包数声称实际成本降幅。
- v2 以流程完整决定 delivered；证据质量单独呈现与比较，加入核心事实保留防止少下结论。每任务重启基线，发布需另行跨任务合并后做全样本留出验收。该节替代上一轮复用 OpenCode 文件面和零失效引用交付门槛的建议。

## 2026-10-21 harness2 评估（本轮证据）
- 已读取用户附件，评估范围仅当前第三阶段。
- 当前 pyproject.toml 版本为 1.7.3，提示词目录已有各角色 instructions/input 文件，附件关于提示词完全写死的表述需纠正。
- 本轮尚未验证模型表现；已有工作记录只作定位线索，结论将以当前源码与离线结果为准。
- 当前 load_prompt() 从包资源加载角色提示词与共享规则，但没有按运行指定 harness 目录的入口；因此是补运行级加载与版本隔离，不是首次拆文件。
- harness2 1cfe254 的 core/spec.py 只要求 SubstrateLike.surface()，BenchmarkAdapter.execute() 自己负责执行；官方 demo.py 直接 subprocess 执行 Python。最小试点可保留 PydanticAI 和 DAG，不把完整 Substrate/工具循环当作接入前提。
- knowledge_report.py 在没有材料目录时引用审计 status=not_run、passed=true。接入门禁必须要求审计已实际执行、检查包覆盖/复核状态，并重算审计，不能只看报告存在或 passed。
- 当前 .venv Python 3.14.0；harness2 pyproject 要求 >=3.12,<3.14，README 建议 Linux。需隔离优化器环境，不能直接装入当前 .venv。
- 当前 logging_utils.py 只记录紧凑进度，未发现当前源码创建 llm_calls.jsonl/trace.jsonl；需新增输入、指令快照与事件轨迹，不能按附件假定日志齐全。
- harness2 的 selection_pool 所有 k 都只含候选 finals，不含基线；k=1 跳过模型选择。交付合格和质量胜过基线是两道不同门槛。
- 经验库传给后续改进模型，不会自动注入当前 Analyzer；固定 tag 也不足以构成线上发布，run_domain 的 retain 会按当前任务前缀清理经验。
- 上游 prompts.py 仅注册 opencode/codex 文件面说明；首次接入应复用已有文件面并显式限制可编辑组件，不能仅新增 SurfaceSpec(name=native) 就声称可用。
- 当前全套离线测试实际为 228 passed、3 failed；两项访问示例中不存在的 final_evidence_assessment stage，一项仍断言 1.7.2。已读取失败测试与配置确认，不修复本轮范围外的测试。
- 本轮重算 results_v17：238 段引用中 202 有效、36 无效，10 条 confirmed 无有效证据，问题清单与保存审计一致。相同事实不传材料目录则实际返回 complete，审计 not_run/passed=true；严格门禁应优先补齐。
- 当前 manifest 为 example3 单样本的 16 个关联作业，不能把 16 包当成 16 个独立训练/验证样本。

## 2026-10-21 当前源码核对
- pyproject.toml 当前为 1.7.3；README 标题为 v1.7。
- cli._save_final_outputs 通过 save_knowledge_outputs 生成报告，model_requests 固定为 0，并以 audit.passed 标记 completed。
- workflow 当前包含 validate_revision_scope / merge_scoped_revision；修订规则需按本轮源码核对，不能沿用旧工作记录。
- README 引用 docs/src-architecture.md，但当前 docs 不存在。
- 单包链：Analyzer → 程序引用检查 → Reviewer → scope guard → 条件 Reviser → 程序按 fixes 合并授权字段并检查新增无效引用 → 最终单包引用审计。没有修订后再调 Reviewer 的循环。
- 全局链：确定性去重 → 模型仅判断 ID 关系及元数据冲突 → 程序组装 → 来源/覆盖校验 → 确定性分层报告。格式修复失败有保守全保留分支；API/超时异常不等同此降级。
- 材料索引在 DAG 前建立，补料需求在任务完成后核对；旁路文件索引不等于模型已分析内容。
- 本轮用当前 project_knowledge 对 results_v17 全局结果及 testdata/packets 做离线投影：47 confirmed、9 suspected、89 调查线索（其中历史 gap 需区分）、13 IOC；238 段引用中 202 有效、36 无效，10 条 confirmed 无有效源码引用；issues 与保存的审计一致。无模型请求、不修改已有结果。

- pyproject.toml 声明版本 1.7.0，入口 bounded_investigator.cli:app。
- README 标题仍为 v0.8，不能直接作为当前流程依据。
- config.py 明示 gap_reconciliation 配置仅兼容，不再执行；旧 final_evidence_assessment 配置在 v1.7 自动报告流程被忽略。
- 需要区分 run 单包、batch 编排、reconcile 已有结果重聚合。
- cli.py 的 _run_batch_dag 实际执行动态调度；dag.py 负责 manifest 校验和依赖摘要，不是调度循环。
- workflow.run 无二次复核；仅在有重大包内问题且启用修订时执行一次 Reviser。
- 格式降级与普通执行异常不同：仅结构化失败进入 Repair/Salvage；API/超时/预算异常可传播。
- reconciliation 使用 Python reducer + 模型 ID 关系决策 + Python assembler + provenance/coverage gate。
- knowledge_report 按 evidence_level 分三层，保留历史 gaps 为调查附录，生成每来源的去向审计。
- material_inventory 索引旁路文件不等于将文件内容交给模型语义分析；补料建议不代表原始样本材料不存在。
- 个人记忆索引无相关命中；未使用历史项目副本作为当前架构依据。

## 2026-10-20 v17 差距核查
- 标准答案为人工优化交付报告；v17 为 54 条全局行为结论、IOC、历史缺口的自动报告，需分别评估技术事实与交付完整度。
- 当前输入只有 testdata/packets/J00-J15 共 16 包；不能将标准答案的 500 函数覆盖率直接套用到本次。
- 报告开头保留 J00 的局部缺口，而后续已有其他包的相关结论；需核查是否全局关系未闭合。
- 已确认输入有 124 个函数片段、去重 123 个 F-ID（F483 分两包），不能认定覆盖标准答案所述 500 个函数。
- 重跑源码引用校验与保存审计完全一致：270 段中 267 有效、3 无效；所有 claim 仍至少有一条有效引用。
- J01 公共辅助语义未结构化，J09 的 F461 哈希算法漏报，J13 的挂起创建参数未解释；全局 resolver 保留全部 54 claim，未合并或替代任何 claim。
- J12 审阅将输出参数与 EAX 返回值链混淆的风险需明确；n3_4/n4 的解密数据对应关系仍是真缺口，不能据此全部升格。
- 最终对照矩阵：30 项中 14 充分、12 部分、4 未形成；该口径为人工验收拆项，不是系统评分或识别准确率。
- 输入范围额外确认：inventory.additional_files 为空，故标准答案的哈希/家族外部情报/500 函数账本没有作为本次运行输入。
- 确认 Markdown confirmed 行为不显示 source 字段、逐项截断到 250 字符，造成部分来源信息仅存在 JSON 或散落 artifact；摘录上限 24 也降低后半部分可复查性。

## 2026-10-20 框架分类补充
- 将前轮问题合并为 R01-R12，按流程/节点/输入边界及框架保障/持续优化两个维度分类，完整追加至对照报告第 7 节。
- 流程建议限于全量结果后的跨包复核和审计失败处置；共享语义、输出通道、模板、IOC 类型等归为节点/交接契约。
- 公开行业依据已读取 OASIS STIX 2.1、capa limitations、MITRE 原始数据映射培训、Zeltser 威胁报告写作原文。框架具体设计与模板产品要求标为本项目建议，不冒称行业标准要求。

## 2026-10-20 两项改动的代码核对
- 已核对现有全局 resolver 只有 ID 关系决策，原始源码未注入，gaps 虽收集到 projection 但不进入当前 resolver 提示文本。
- 当前已有末端全材料审计和 incomplete_provenance/completed=false；第二项建议明确为引入审计后的修正/降级路径，不能说当前完全没有门槛。
- 已核对旧 gap_reconciliation 仅 resolved/still_open 且移除已解决 gap，不能原样启用来替代综合层/部分解决/保留历史需求。
- 内存副本修正三处引用，再使用现有投影、组装、来源校验与引用校验：267/270 → 270/270 有效，54 claim/79 artifact 不变，无未对账来源；未覆盖 v17 文件。

## 2026-10-20 最终环节与修改机制核对
- 当前没有执行中的最终语义分析 Agent：workflow.__init__ 将 final_assessment=None，run_final_evidence_assessment 仅有定义；assess/report 明确零模型调用。当前实际为全局关系整理与确定性报告。
- 已有引用审计摘要进入 Reviewer/Reviser，但修改由 Reviewer critical/major packet_internal 决定；末次审计没有反馈修复分支。
- 新核对：J07 Analyzer 审计 0 错，Reviser 后新增 2 个 invalid_format；J03 Analyzer 0 错/0 延期，Reviser 后新增一个延期范围，最后全材料判越界。说明必须检查并处置最终产物。
- 最小方案已列入对照报告第9节：保留语义审阅，在全部包到齐/全局整理前统一全材料校验，对受影响包最多一次窄引用修复，程序应用并复验，保留原始结果；offline report 不暗中调用模型。
