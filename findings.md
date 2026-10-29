# 梳理依据

## 2026-10-21 harness2 v3 顺序反馈核实
- 当前正常 DAG batch 会向 _save_final_outputs 传入材料目录；assess/report 与 reconcile 的 packets_dir 为可选。not_run 缺陷成立，但“影响每份生产报告”超出代码证据，应按实际入口与审计状态限定范围。
- DagManifest/load_manifest 消费已有 manifest，当前目录没有第一二阶段产物生成链。样本准备应从一开始作为输入依赖推进，但不应把上游实现纳入本轮工作。
- 基线校准须在目标环境、加载器、轨迹、指标与适配器替身稳定后开展；“一套基线终身复用”不成立，能力/材料/预算或指标口径变化后需重测受影响基线。
- B/C/D 共享 Workflow 的构造、_run_agent、修订保存与 CLI 报告调用；可在字段/接口契约固定后分工，不是代码完全独立。先冻结运行身份和数据契约，再整合共享文件。
- 当前 max_model_requests 默认 3、可配置上限 10；固定工作流没有因此变成自由工具循环。能力分流须区分策略、程序契约、输入/能力、原因不确定；不能按数量多数或一次无收益直接建设工具。
- 本轮合成确定性投影复现四分支：无材料和空目录都 complete/not_run/passed=true；有效引用 complete/1 valid；越界引用 incomplete_provenance/1 invalid。调用路径和证据保存于 docs/harness2-v3-review-checks.json，未改业务代码。
- 原 results_v17 目录当前不存在；上一轮 36 段失效和 10 条 confirmed 无有效引用仅作历史记录，不作为本轮重新核验的分布。未进入其他目录找材料。
- 方案 v3 把 native 注册放到 E-1、校准放 E-2、加入 G 决策；审计修复可在现有环境独立推进，环境步骤 0 的已知旧测试失败在 A 对齐，避免依赖循环。
- 最终环境复查：.git 当前已存在（与旧轮次不同）、results_v17 不存在、Python 为 3.14.0。A 改为记录 Git 提交与实际工作区差异，不再要求先建版本库。

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
# 2026-10-23 清理依据

- 旧最终评估模型始终 None，旧 Gap Reconciliation 无当前生产调用；final_assessment 模块中的材料建议仍在使用，必须迁出。
- workflow 同时持有阶段调用/格式修复/流式输出和调查编排；CLI 同时持有批调度、重放、报告保存，适合按实际职责拆分。
- legacy 字样不等于无效代码：当前制品规范化、补料解析和证据未分类保护继续保留。

- 本轮清理完成后：CLI 752→129 行，workflow 1354→695 行。模型类型的描述也是模型 schema 的一部分，保留其原文以避免整理时改变指令契约。

# 2026-10-23 HTML 模板与编排

- 原 skill 的 CSS、校验器及渲染结构可随包发布，无额外依赖。原模板强制至少一个 hash；显式空/null 不填假值，会在模型调用前拒绝。
- 元信息/外部模块使用独立上下文；canonical 行为、IOC、证据和源码由程序按 ID 装配。引用合法不能证明摘要/顺序/风险语义正确。
- 独立复核复现并修正：通用限制冗余、未读取已有片段、上下文迟校验、超时丢流式诊断、单包输入输出重合，以及完成 manifest 写失败残留 HTML。
- 原本运行完的引用审计没有 status 键，只有 not_run 分支有该键；预览 manifest 采用 completed 表示确已运行，不修改原审计。
- HTML 限制过滤只剔除完整的通用提醒；包含具体缺失条件的混合句保留原文，避免按“运行时”等词子串误删有效限制。现有 Markdown 过滤行为不改。

# 2026-10-25 报告 Agent 与 HTML skill 边界

- 既有harness加载器仅接收三个调查角色，不能直接加载report或带渲染资产的skill；报告模块采用独立的固定资源加载入口，原harness不变。
- 原章节组装固定追加全字段表与全部代码，需由类型内容块取代；原值通过FactValue、表格字段和代码引用绑定，叙述由Agent组织。
- 通用模型构建会为native/prompted追加JSON约束；抽取纯stage_instructions供构建与报告快照共用，保持已有模型输入字节语义。
- HTML文档与页面发布分开：报告成功先保存_report_document.json，模板编译/渲染失败只撤去HTML及模板JSON。
## 2026-10-25 报告边界复核

- 内部事实 ID 的文字检查不能用 Unicode `\w` 边界：中文邻接文字会被当作同一单词而漏检。改用 ID 使用的 ASCII 字符边界；仅约束编排文字，原始绑定值继续保真。
- 引用结构合法不代表产生了可见内容。HTML skill 拒绝空绑定值与全空表格行，避免空 limitations 被误当成已展示行为；不引入语义审计或取舍台账。
- 源码分发包的现有离线报告测试依赖未分发的私人 config.yaml。测试改用随包提供的 config.example.yaml；不变更运行配置或命令行为。合成示例随 sdist 发布，便于离线复现和测试。

## 2026-10-26 Reviser 条目完整性

- J07 原 3 条 claims、授权新增 2 条，应输出 5 条；失败候选仅 4 条并丢失 SID 原事实。J11 原 2 条、新增 1 条，应输出 3 条；候选仅 2 条并丢失 NSS 加载/解析原事实。
- 原稿和候选按既有合并规则重放，均因长度不符拒绝。仅恢复原条目与正确位置即可通过原合并规则；保护逻辑不应放宽。
- Reviser 角色要求完整结果，但输入模板末句要求“精简最终结果”；执行说明仅列权限路径，没有最终数量及原/新槽位映射。
- 当前默认策略与本次 results 快照指纹相同；旧指令夹具必须保留。Analyzer/Reviewer 不变，Reviser 新建当前预期，冻结旧角色原文以重建历史策略。
# 2026-10-27 本轮实施依据

- 报告输入将全部历史 gaps 作为当前 gaps 提供，编译器又直接展示原始 question；改为历史上下文及独立、简短的报告限制说明，不删除原记录。
- IOC note 自动拼接 meaning 与全部证据，绕过报告角色的内部编号隐藏要求；改为报告可选短说明，原值仍由程序绑定。
- 公共规则允许依赖摘要的节，而源码解析器仅接收 F-XXXX Lx[-Ly]；统一指令与字段说明，不放宽解析/合并。
- 依赖交接的 claims 已携带原证据位置，artifacts 尚未携带；补齐完整和部分结果的 artifact 交接。
- 已验证基线为 398 passed、1 项 Windows 符号链接权限跳过。保护当前 215 个结果/HTML 文件，禁止本轮真实模型请求。
- 修改后工作区与隔离 sdist 均为 414 passed / 1 skipped。wheel/sdist 各自隔离加载 42 模块并验证实际资源和渲染；模板字节、旧夹具、严格合并代码及 215 个产物均未变。
- 交叉复核独立从 J07 控制流确认 LSA 位于逐条目循环之前，DialParamsUID 在循环内另受路径缓冲区条件控制。新技能的实际项目模型遵循效果尚需重跑验证。
# 2026-10-27 batch 自动 HTML

- batch 命令编排在 DAG 完成、全局结果有效后复用既有报告 Agent；默认输出 `<output-dir>/html_report/_final_report.html`，可显式关闭并透传补充上下文。
- HTML 阶段与全局分析异常处理分开；报告失败保留分析产物，先保存 DAG 中的失败状态再让 CLI 非零退出。
- 独立复核发现直接传目录会破坏自定义 pattern 并丢专家scope/外部材料，已补复现并改用保存的 scope/inventory 与本批次实际包列表。严格引用解析与报告门禁未放宽。
- 新清理函数仅移除4个自有发布文件，不递归删除；在清理前拒绝目录别名冲突及占用发布名的上下文输入。
- 最终19项batch专用用例通过，工作区和隔离sdist均433通过/1项权限跳过。wheel/sdist加载42模块并验证自动生成；286个既有制品保持原样，未调用真实项目模型。
# 2026-10-27 报告遗漏 G:C037

- 实际报告响应有58项行为，G:C037（CredentialManager解密输出字段提取）未在正文表述；IE的User/Password字段处理属于另一条代码路径，不能当作已覆盖。源码审计通过，错误发生在报告Agent最终正文覆盖检查。
- 固定真实响应后先复现4项失败。单条恢复只依据相同来源包、重叠源码和唯一已有章节，不使用函数名关键词或材料DAG猜测机制；无法唯一定位仍拒绝。
- 补入遗漏行为和条件；若只有综合callout引用了相关事实，则连同唯一上下文行为的条件和确定程度保留，避免误归Firefox/IE等其他机制。真实模型响应保持原样，生效版本单独保存。
- 已用保存响应离线恢复 results/html_report/_final_report.html，58项正文覆盖通过，模板校验无错误，新增模型请求0；DAG仅更新HTML结果状态，原失败manifest/summary另存诊断。

# 2026-10-28 Content Studio 对照分析依据
- 官方原文：https://www.langchain.com/blog/design-agent （页面标注 2026-10-27）；AIHOT 于北京时间 2026-10-28 展示。
- 当前工作区含既有未提交修改，本次以工作区源码而非 HEAD 为准。
- 已核对主要入口、workflow、harness loader、DAG、replay、模型构造、知识报告与引用审计实现。
- knowledge_report.py 的无源码分支返回 status=not_run 且 passed=True，issues 收集跳过该状态；需要以离线探针核实报告状态和发布影响。
- 当前业务角色由程序固定选择并展开技能；Agent 构造未注册业务工具。此处 output_mode=tool 是结构化输出，不代表主动查阅材料工具。
- 本次个人记忆检索无相关命中，判断以当前源码和原文为准。
- 离线探针确认：无源码且引用不存在函数时，project_knowledge 返回 complete、audit.passed=true，引用审计 status=not_run/passed=true。
- 有源码且引用错误时：知识审计失败，仍生成标记 incomplete_provenance 并含完整性警告的 Markdown。
- HTML 使用既有测试固定响应复现：source_audit.passed=false 时仍可生成 status=completed、preview_only=true 的页面；外部模型请求0。当前路径明确为预览，正式交付需要另行定义门槛。
- 依赖交接探针确认：保留 suspected，但丢失 claim.limitations、review_completed=false、revision_failed=true。
- tests 目录本轮结果为470 passed/1 skipped；根目录pytest收集被test_1.py:5旧导入阻断，不宣称根目录完整测试通过。
- 详细分析及建议写入 docs/content-studio-src-analysis.md。业务源码未修改；真实模型质量、规则简化和成本收益均未进行实验。
