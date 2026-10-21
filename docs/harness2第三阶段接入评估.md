# harness2 第三阶段接入评估与实施顺序 v2

核对日期：2026-10-21。范围：当前目录的第三阶段 v1.7.3；第一、第二阶段只作为已导出材料的来源。harness2 固定参考 `bg668/harness2@1cfe254`（0.1.0）。

本版已复核并合并工程反馈：将交付完整性与证据质量拆开，采用原生文件面，加入基线波动测量、依赖闭合子图任务、跨任务合并、只读规则上下文和同环境培养运行。以下是修订后的统一方案，替代上一版对应建议；集成与真实模型实验尚未实施。

## 1. 判断

**建议采用 harness2 作为离线优化引擎。第一步是让现有第三阶段支持“按运行加载不同 harness、独立执行、记录完整轨迹、严格验收”，随后接适配器。** 现有 PydanticAI、DAG、结构化结果和确定性报告可以继续使用。

需要区分两个里程碑：

- **接入优化引擎**：让 harness2 改进当前 Analyzer、Reviewer、Reviser 的分析与复核策略，并验证改进是否能迁移到其他样本。这个里程碑不依赖主动补料工具，也不要求先重写执行框架。
- **扩展第三阶段 Agent 能力**：让 Agent 主动查材料、补证据、做跨包语义复核。这会扩大现有 Worker 的权限与调用次数，必须另补材料来源、工具预算和审计契约。它能扩大后续优化空间，但不是第一次接入的必要条件。

当前实际执行链是材料包 DAG、逐包 Analyzer→Reviewer→条件 Reviser、全局 ID 关系整理、程序生成报告。`final_assessment=None`；末端没有执行中的自由分析/最终语义评估模型。harness2 只能优化实际存在且被执行器消费的部分，不能靠修改提示词补出一个不存在的工具或分析节点。[E2、E4]

## 2. 附件需要修正或细化的判断

| 附件判断 | 当前代码/上游源码核对 | 对实施的影响 |
|---|---|---|
| 提示词写在 Python 中，需要先文件化 | 当前 `prompts/` 已有角色说明、输入模板和公共质量规则；`load_prompt()` 固定读取安装包资源 | 需要补的是运行级 harness 目录、加载映射和版本快照，不必重新拆一次提示词。[E1] |
| 必须先改成 OpenCode Agent 或实现完整 Substrate | 核心协议只要求 `substrate.surface()`；任务执行由 `BenchmarkAdapter.execute()` 提供。官方 Demo 直接执行 Python | 最小接入可直接调用当前 Python 工作流。不要把完整 `materialise/execute/read_trajectory/preflight` 实现列为首轮必做。[H1、H2] |
| 已有 `llm_calls.jsonl`、`trace.jsonl` | 当前源码未发现这些文件的写入逻辑；实际有紧凑日志、阶段 raw/validated/partial 输出和审计旁路文件 | 需要补轨迹记录与渲染，尤其是当时实际输入和指令；不能假定已有完整日志。[E2、E3] |
| `delivered()` 绑定报告状态即可 | 现有审计有 `not_run` 却 `passed=true` 的分支；现有基线又有失效引用 | `delivered()` 检查流程完整及审计确实执行；证据质量进入可读产物、离线比较与发布，不能把 `incomplete_provenance` 全部当成没有产出。[E4、E5、H3] |
| k=1 没有基线对照 | 所有 k 的选择池都只有合成候选，基线均不在池中；k=1 还跳过模型选择 | “可交付”和“胜过当前基线”必须分开验收。增加 k 不会自动解决退步问题。[H3] |
| 没有 20–50 个标注样本就无法接入 | `score()` 只在选择结束后的 judge 阶段调用，不参与递归核心 | 无标签可以验证接口和运行闭环，但不能证明分析质量提高。正式采用前仍需要独立标注与留出集。[H1] |
| 经验库会使下一次分析自动成长 | 当前库是后续改进模型的上下文，不会自动进入本项目 Analyzer | 线上使用的是经验证后发布的 harness；是否注入记忆，必须由本项目加载器明确实现。[H4] |
| 固定 tag 即可跨运行共享 | tag 只解决命名问题；`run_domain()` 会用 `retain()` 保留当前任务流的前序经验 | 不能直接把训练运行库当生产长期库；共享、审核、发布与回滚要独立管理。[H4] |

运行环境建议统一：当前 Windows 虚拟环境是 **Python 3.14.0**，harness2 声明 `>=3.12,<3.14`、README 推荐 Linux；当前项目声明 `>=3.11`。在 Linux/WSL 的 Linux 文件系统内建立独立培养目录和 Python 3.12 环境，优化器、编辑/选择 CLI 与第三阶段都在其中运行，避免每次 `execute()` 跨 Windows/Linux 转换路径和管理进程。WSL 培养目录优先放在 Linux 用户目录，不直接把 `/mnt/d` 工作区作为运行目录；Windows 现有环境继续正常分析。[E10、H5]

这仍是环境方案，不能仅凭版本声明断言兼容。接入前需在目标环境安装依赖、运行当前离线测试、检查 `claude/opencode` 通道、超时和恢复。上一轮的三项测试失败应先对齐配置/版本断言；本轮未创建或验证 WSL/Python 3.12 环境。

## 3. 建议按此顺序实施

下列文件名中，“新增”表示建议，尚未实现。

### A. 固定可比较的基线与实验边界

1. 保存当前源码、配置、提示词、schema 版本和输入清单的快照/摘要；当前目录没有 Git 元数据，先建立版本记录，避免原地覆盖后无法还原。
2. **正式支持两种任务：整样本 `sample` 和依赖闭合子图 `closed_subgraph`。** 优化试点先用较小子图，发布验收使用整样本。子图必须递归包含目标包的所有 `depends_on` 祖先，生成独立 manifest 和材料白名单，候选与基线每次都重跑这些包，避免复用基线的依赖摘要影响归因。当前 J00–J15 是同一样本的 16 个关联作业；不同子图仍属于同一样本，训练/留出必须按原样本分组。[E4、E8、E11]
3. 固定模型、分析预算、材料、DAG 和报告代码，再比较 harness。不要同时换模型、扩大输入和改提示词，否则无法解释收益来源。
4. 对齐当前三项测试中的旧配置/旧版本断言，得到明确、可重复的基线。这属于接入前准备；本轮没有改测试或业务代码。
5. 对实际比较的同一任务、同一 harness 独立运行 **3 次基线校准**，记录行为/引用总数、失效引用数与比例、缺有效引用的 confirmed 数、复核重大问题数、修订拒绝数、token 和耗时。子图试点测子图波动，全样本发布另测全样本波动；固定温度也不能代替重复测量。三次的极差/中位数只用于初筛噪声，不是可靠置信区间，也不能单凭行为条数增加认定质量变好。

本轮仅验证了图依赖示例：目标 J04 的闭包是 J00、J01、J04，生成的三包 manifest 可通过当前 DAG 校验。这说明子图形式与现有契约相容；尚未验证它是否是最佳业务边界、实际成本或分析质量。包内容与调用量不同，不能按 3/16 直接推算节省比例。

验收：同一份输入和指定版本的 harness 能在独立目录重复运行；每个结果能追溯到实际指令、材料和模型配置。这里的“重复”指配置与输入可复现，不要求模型逐字一致。

### B. 补运行级 harness 加载，限定真正可优化的内容

主要改动点：`prompts.py`、`BoundedInvestigationWorkflow.__init__()`、CLI/config；建议新增小型 `harness_loader.py`，集中处理路径、映射、校验和指令组合。[E1、E2]

首轮只开放 **Analyzer/Reviewer/Reviser 的分析策略文本**，例如核对顺序、source→action→sink 分析方法、函数语义识别提示和防漏查策略。全局整理策略以后单独做实验，便于归因。

以下内容由受信代码固定注入或执行：公共证据规则、输出 schema、输入模板中的必需字段、复核触发阈值、修订范围、IOC 分流、引用与来源审计、预算和报告生成。不能只在一个可编辑文件里写“不可修改”，然后仍允许优化器重写它。

**推荐注册原生 `native` 文件面，并支持按角色装载策略 skill。** 共享策略放 `systemprompt.md`，三个必需角色策略放 `skills/analyzer/SKILL.md`、`skills/reviewer/SKILL.md`、`skills/reviser/SKILL.md`；允许新增领域策略 skill，但只能注入这三个角色的分析上下文，不能扩展执行器权限。每个 skill 的 frontmatter 至少包含匹配目录的 `name`、`description` 和非空 `roles`，roles 只允许 analyzer/reviewer/reviser。加载器按固定顺序拼接适用策略，限定文件数、单文件和总长度；超限拒绝，不能无声截断或忽略。

文件面描述必须与实际消费方式一致：这些 skill 由程序按角色装载，不存在 `skill/task/Bash` 工具循环；首轮禁止 script、plugin、subagent 和未实现的记忆/工具组件。原生 `SurfaceSpec` 仍需配套 `validate_tree()` 的路径白名单，因为上游通用树代码仍识别脚本等路径。[H6、H7]

在集成启动时，为固定版本的 `prompts._SURFACE_TEXTS` 注册 `native` 条目，提供自定义 `surface_md/inv/dom/tts_surface`；尤其要替换 INV/DOM 中对脚本、hook 和按需工具的指引，不能只换文件面标题。首轮固定 `BenchPrompts.prompt_pack="adapt"`，预检拒绝 `tts`：该路径另有 `_TTS_INV` 等硬编码模板，仅注册条目不能使它们适配。注册表和 `_SurfaceText` 都是私有接口，应锁定上游版本，并以无模型合同检查在升级时快速报错；这不需要 fork 或改递归算法。[H6]

基线 `systemprompt.md` 预先点名三个角色 skill；新增 skill 的触发行由编辑器写入，或由 `autotrigger` 补入同一个可编辑的 `systemprompt.md`。`SurfaceSpec.autotrigger_file` 指向此文件，不能指向冻结的 guardrails。保持 frontmatter 与触发行一致，避免仅有两次尝试的阶段因接口矛盾反复回退。[H7、H8]

需要落实的约束：

- `--harness-dir` 对整次 batch 固定，逐包构造 Workflow 时传入同一个已验证快照；禁止改安装包内的提示词实现切换，否则并行候选会相互污染。
- 角色与领域 skill 首轮均按 roles 确定性加载，不声称已有模型自主检索或按需工具功能。
- 声明哪些文件实际会被加载；编辑未支持的脚本、plugin、subagent、工具指导或记忆文件应拒绝，不能接受后悄悄忽略。
- 校验路径、链接逃逸、文件数量与长度、skill 格式和必需角色；失败在调用模型前报出可修正的原因。
- 将实际组合后的 instructions、harness 摘要及冻结规则版本保存到运行目录。

冻结规则同时必须对编辑模型可见：将公共证据规则、复核阈值、IOC 分流及材料边界的只读说明随任务复制到 `source/frozen_rules.md`，在 `BenchPrompts.yardstick/domain_guidance` 给出摘要和定位；执行器仍从可信资源固定注入原规则，保存同一版本摘要。文件面明确这些规则优先于候选策略且不得改动。编辑器上下文副本被篡改不得传回执行器；冻结资源版本不匹配时拒绝。[E1、H1]

`validate_tree()` 可拒绝少量明确的冲突表述，例如直接要求跳过引用核查或将无证据猜测升级为确认；检查要区分否定、引述和正向指令，遇到难判情况保留诊断供审核。关键词检查只能辅助，不能证明任意自然语言策略与规则一致，最终仍靠受信程序约束及行为复验。`BenchPrompts` 会对可见字段中的 benchmark/evaluation/grader/rubric/score 做英文子串拒绝，规则说明应表述分析工作本身，并在模型调用前检查；不要把答案或评分细则混进只读规则。[H1]

验收：修改 Analyzer 策略只影响指定候选的 Analyzer；Reviewer/Reviser 各读取自己的策略；冻结规则保持一致；没有任何候选能修改 schema、审计或执行器源码。

### C. 补可用于改进的轨迹

建议新增 `trajectory.py`，在现有阶段执行与程序决策处记录事件，再渲染为 harness2 的 `[ACTION]/[OBSERVATION]/[ERROR]` 文本。当前 `_run_agent()` 已有集中调用点，可以复用。[E2、E3]

至少记录：

- `run_id/task_id/packet_id/stage/event_id`，实际 instructions、prompt、材料与依赖摘要的快照或可定位引用；
- Analyzer 输出、引用校验结果、原始与 scope guard 后的 Reviewer 决定、修订授权、Reviser 输出、实际采用/拒绝的修订及原因；
- Format Repair、结构化失败、部分保留、超时、全局来源校验和报告审计；
- 各阶段请求量、token、耗时；价格未配置时保留未知，不把未知成本记为零。

记录模型交付的结果与程序事件即可，不需要索取或补造模型私有思维过程。长日志可分文件，渲染时要保留稳定事件定位和截断提示，避免编辑模型因看不全材料而“发现”并不存在的问题。

**轨迹文件开头增加问题索引**：列出审计失败、Reviewer major/critical、修订被拒、格式修复失败对应的 event_id、包、阶段和详情位置；正文仍保存完整事件顺序。harness2 会把轨迹写入 `context/rollouts/*.md`，索引必须出现在该渲染文件内，不能只留在外部原始日志中。索引按事实定位，不混入隐藏标签或离线评分。[H9]

验收：每条改动建议都能指到具体运行、包和事件；同一个事件可还原当时看到的材料与实际输出。

### D. 分离流程完整门槛与证据质量

建议新增 `delivery_gate.py`，同时产出流程检查和证据质量摘要；由可信代码读取本次材料与结构化产物重算，不能采用模型自写的 `passed=true`。两个结果用途不同：

| 层 | 检查内容 | 使用位置 |
|---|---|---|
| 流程完整：硬门槛 | 当前任务 manifest 所有预期包有 schema 合法的产物且完成规定复核；必要阶段按约定执行或合法保留原结果；全局来源账本结构完整；审计确实运行；版本、输入和冻结规则一致 | `delivered()`，不通过的候选不能进入选择池 |
| 证据质量：相对门槛 | 失效引用数与比例、引用总量、缺有效引用的 confirmed、受证据支持的核心事实保留情况、未解决复核问题及修订拒绝 | 可读产物与轨迹、选择后的 `score()`、独立发布比较 |

流程硬门槛的细节：

1. 以 sample 或 closed_subgraph 自己冻结的 manifest 判断覆盖；子图产物须明确标记范围，不能冒充整样本报告。DAG 中“任务成功”不等于已完成复核。
2. 检查报告与本次全局结果、输入和 harness 快照一致；来源账本中的来源缺失、重复冲突和数量不匹配属于结构错误。证据语义或引用质量问题另列，不把它们都合并成流程错误。
3. 对当前任务全部实际材料执行引用审计；材料目录缺失、空目录、审计异常或结果不可解析均拒绝，`status=not_run` 一律拒绝。审计执行过与审计通过分开判断。
4. 复核已完成但仍有重大质量问题，或程序拒绝修订并按约定保留有效初稿时，记录其质量/降级状态；不因它们一律禁止进入试验池。必需阶段缺失、流程中断或无可消费结果仍拒绝。

**不要用 `report.status == complete`、`knowledge_audit.passed` 或“零失效引用”直接决定 `delivered()`。** 当前 knowledge audit 同时汇总结构与证据质量；需分别计算，不能用总布尔值代替分类。现有基线的 36 段失效引用、10 条缺有效引用的 confirmed 必须进入质量摘要；是否满足完整流程仍需逐包检查，不能据此宣称整个 `results_v17` 已具备 delivered 资格。

可读产物展示每次审计的原始数量、分母及明细，供编辑/反思和有选择模型时参考；由适配器根据原始产物统一计算，避免模型自行挑选口径。只包含执行器本身可见的事实核查，不包含隐藏标注或离线评分。**k=1 没有选择模型**，把指标写进 `deliverable_text` 并不会自动产生质量择优，候选仍需外层重复比较和发布验收。[H3]

发布以预先约定的关键质量指标不差于基线、且有可重复收益为准，并检查保留基线中有材料支持的核心事实，防止靠少下结论改善引用数字。允许依据证据纠正错误结论、降级推断或合并等价表述，但需记录理由；条数减少本身不代表漏报。无标签时该保留检查仍有语义局限，不能冒称召回率已得到保障。

“零失效引用”保留为长期质量目标。若以后设置绝对发布下限，应先建立能通过的基线并明确规则，不能让优化试点在没有可比较起点时全部失去候选。流程不完整的失败候选和质量不足的完整候选都留档；只有经过发布流程的版本进入正常分析。

验收要同时验证两类反例：缺材料却返回 `complete`、仅有报告、漏包、未复核或冻结规则变化必须被拒；流程完整且仍有失效引用的候选应能进入试验池，同时如实暴露质量问题；靠删除有证据支持的核心事实改善引用数字的候选不得发布。引用自动窄修复可另设预算，首轮不必先实现。

### E. 实现 MalwareAdapter，接通最小递归试点

建议把集成放在独立的 `integrations/harness2/` 下，使当前正常分析入口不依赖优化器。以下是接口职责，尚非已完成实现。

| 接口 | 本项目应提供的内容 |
|---|---|
| `tasks(split)` | 固定 sample/closed_subgraph 任务及训练/留出划分；同源样本的所有子图同属一个划分 |
| `domain_of(task_id)` | 首轮用预先确定的单一 `static-code` 域；有足够样本后再用已提供的文件类型等元数据细分，禁止使用事后家族结论 |
| `task_prompt()` | 合法分析任务与材料边界，不包含标准答案、评分规则或模型需要命中的结论 |
| `task_files()` | 白名单导出的任务 manifest、闭合材料包、必要说明及只读 frozen_rules；绝不返回项目根目录 |
| `base_harness()` | 已发布基线的不可变快照，内容与 `execute(None)` 实际使用的基线一致 |
| `validate_tree()` | 原生文件面、角色/领域 skill 元数据和长度、冻结资源版本、明确冲突与可消费性检查；不支持的组件直接拒绝 |
| `execute()` | 指定 harness，在同一 Linux 培养环境执行当前任务的 DAG/全局/报告流程，输出到独立 run_id；恢复时核对任务范围及输入摘要 |
| `trajectory()` | 开头提供问题事件索引，按 obs_trunc 渲染正文并保留事件 ID |
| `delivered()` | 仅调用 D 的流程完整硬门槛；不以失效引用数、质量总 passed 或报告状态直接判失败 |
| `deliverable_text/files()` | 报告、结构化事实与统一计算的事实核查摘要，保留条件、限制、引用、分母和未解决问题；不包含隐藏评分 |
| `score()` | 选择结束后的离线评估结果；评估不可用返回 `None`，不能用零伪装评估完成 |

核心只需要一个提供原生文件面定义的轻量 substrate 对象和对应编辑提示注册；实际运行由适配器负责。无需实现所有执行器协议，也不修改上游递归算法。[H1、H2、H6]

模型通道另行验证：现有分析模型仍通过本项目 PydanticAI/API 配置运行；harness2 编辑/选择通道使用其支持的 `opencode` 或 `claude_cli`。前者要求带 provider 前缀的模型 ID。不能假定当前 `config.yaml` 会被优化器自动复用。[H1]

安全边界与最小试点一起做：仅导出静态文本；编辑器只写候选 harness；分析执行器只读材料、只写自己的结果；首轮禁用可执行脚本/plugin。优化器工作区排除 `.env`、`example3标准答案.md`、人工比较、家族标签和 grader 输出。`leak_globs` 是补充，白名单与文件权限才界定实际可见材料。限制不必要网络访问，同时保留必要的模型服务通道；不能把“彻底断网”与远程 API 调用混为一谈。[H5]

验收：先用无付费调用的执行替身检查基线、INV、DOM、合成、两层门槛与恢复；同时检查提示内容、允许路径和实际装载完全一致，并区分“策略无收益”和“文件面冲突导致回退”。再在闭合子图上做 3 次基线校准，运行单一 `parallel, k=1`，对最终候选补 2 次独立执行，使候选也有 3 次观察。

外层 3 次校准不会自动成为 harness2 的基线集合：k=1 内部仍只产生一个 base。按此方案，一次初步比较至少是 **3 次校准＋4 次递归内执行＋2 次候选复验＝9 次所选任务执行**，另加编辑/合成/反思；修复、失败重试和后续全样本验证另计。这不是 9 次 LLM 请求。先限制总预算及并发，避免候选并发与包并发相乘。

### F. 建立对照评估与发布流程

第一份现有样本只适合做接线与回归案例。补充若干独立样本做试验，随后建立覆盖不同能力、来源完整度和难度的标注集；20–50 是可采用的工程起点，不是 harness2 的接口条件。训练、留出、近重复样本与家族关联由评估侧管理，标签不进入执行器和改进上下文。

比较同输入、同预算的基线与候选：核心行为精度/召回、关键条件与数据去向正确性、IOC 精度、失效引用率、未获证据支持的确认结论、丢失结论、请求/token/时间/成本。ATT&CK 映射仅在交付要求包含它并有标注时加入，不能当成当前已实现能力。

重复比较先检验候选是否稳定超出 A 中观察到的波动、是否符合预先约定的关键指标容差；仅有一次较好或落在基线波动内时记为“证据不足”，不宣布提升。三次运行只提供初步区间，样本少、变化混杂或结果边界接近时，应扩大复验，不能把极差当显著性检验。不同子图的原始数量不可直接横比；同源子图的多次 helped 也不能算跨样本迁移证据。

**harness2 的内部候选选择不承担本项目的上线批准。** 对单任务改进做离线分析后，必须冻结候选，在没有再针对该留出样本编辑 harness 的条件下测试迁移效果。候选不能仅因同一道题得分更高就成为下一版基线。`score()` 保持在选择后调用；独立发布流程使用这些结果作采用/拒绝决定，不能把标准答案回灌给递归编辑器。[H1、H3]

**补齐跨任务合并，明确发布候选的来源。** 上游每个新任务都重新读取 `base_harness()`，经验库只是改进参考，不会使下一任务继承上一任务的 harness。单任务采纳结果先进入待审区，不能默认就是待发布版本。[H10]

首轮由人定期合并，按以下顺序推进：

1. 积累多个独立样本的任务记录，以 cue/lesson/evidence、改动 diff、事件定位、编辑判定和重复执行事实为依据。不能只拼接整份单任务 harness，也不能把模型写的 helped 当作已证实的因果关系。
2. 只将跨多个独立任务重复显示收益、无未解释 hurt、且非 confounded 的改动列入合并；优先处理 INV 通用策略，DOM 按预定义领域保留。单样本专用结论和事实不进入通用策略；有潜力但证据不足的改动继续待审。
3. 从固定的已发布基线构建一个新的**合并候选版本**，逐条记录来源任务、适用域、取舍与冲突处理。合并步骤有自己的版本号、记录与成本；若以后由有上限的模型会话辅助，也必须与单任务递归分开记账，且看不到隐藏标签/评分。
4. 合并候选冻结后，先回归训练任务，再在未参与编辑的留出样本上运行整样本测试与必要重复比较；合并后的相互作用可能使原有收益消失，不能省略重测。其关键质量指标不差于基线、具有可重复收益，且通过分析师审核后，才发布为不可变版本并保留上一版回滚。

经验至少保留 cue/lesson/evidence、适用域、来源任务和运行、重复测量、验证状态。人工审核的是候选版本，正常分析无需逐次审批。整个流程为“单任务记录 → 跨任务合并候选 → 回归与留出验证 → 审核发布”，与 harness2 内部的采纳区分。

上岗时直接加载已发布 harness，默认不递归。待审经验库供离线编辑使用；已审核经验若确需线上检索，再实现明确的读取方式。不要让生产分析读取持续变化的训练运行库，也不要仅靠固定 tag 共享生产经验。[H4]

## 4. 下一步能力扩展：在接入闭环后做

**主动补料**：先用当前导出材料建立只读 `get_function/search/callers/callees` 等必要接口；若必须连接上游 SQLite，只定义并验证只读数据契约，不改第一、第二阶段内部流程。当前材料索引仅检查存在性，`material_needs` 不会自动取回材料。[E6]

这一步还要同时解决三个问题：取回内容进入本次材料快照并被引用审计索引；Worker 的可用材料边界随工具返回明确更新；工具调用使用独立上限并为 Reviewer/Reviser 保留预算。现有最多 3 次核心模型请求按 A/R/R 设计，不能直接放进任意次数的工具循环。[E2、E7]

**跨包语义复核**：若目标包含还原全局行为链，应在全包结果可用后单独设计有界分析节点，让它能读取原始材料、识别跨包条件/数据流并提交可审计事实。现有全局节点只决定已有事实的 ID 关系，没有这项职责；简单恢复旧配置或要求报告模型“再想一遍”都不能补齐契约。[E2、E9]

以上两项会扩大 harness2 可以优化的空间。执行器权限、证据规则与预算先由项目明确，再逐步开放策略，避免把框架建设交给递归模型自行决定。

## 5. 核验结果与边界

### v2 本轮复核

- 已依据本会话读取的固定版本源码逐项核对工程反馈，没有重新安装或运行完整 harness2。
- 对固定源码中的纯提示/树校验函数做隔离离线探针：原生注册条目可用于 adapt 的 INV、DOM、合成和顺序组合；三角色基线 frontmatter/触发行可通过树校验；新增领域 skill 的触发行可以补入 `systemprompt.md`，不修改 guardrails。
- 探针同时确认：切到 tts 后仍有硬编码脚本指引；BenchPrompts 拒绝禁用英文词。因此方案固定 adapt，并要求版本兼容检查。
- 当前 DAG 校验接受 J04 对应的 J00/J01/J04 闭合子图。没有运行这些包的模型分析。
- 记录见 [v2 源码合同检查](harness2-v2-review-checks.json)。这些探针在现有 Python 3.14 中隔离加载源码片段，**不能替代 Linux/Python 3.12 的安装、CLI、完整集成及行为验证**。
- 3 次模型基线、候选重复、跨任务合并、WSL/Python 3.12 运行均未执行；本轮没有重跑业务测试或修改业务实现。

### 上一轮已有检查（本轮保留，未重跑）

- 已阅读附件，并核对当前源码及 harness2 固定版本的一手源码；未运行 harness2 真实递归，未调用付费模型或执行样本。
- 上一轮离线测试：`228 passed, 3 failed`。两项失败访问示例配置中已不存在的 `final_evidence_assessment` stage；一项断言版本 `1.7.2`，当时实际为 `1.7.3`。本轮未修改这些测试，也不将旧结果当成本轮新通过。
- 用当前 `project_knowledge()` 和实际材料重新审计 `results_v17`：238 段引用中 202 有效、36 无效；10 条 confirmed 缺有效引用，状态为 `incomplete_provenance`，问题清单与保存审计一致。
- **同一份全局事实不传材料目录时，实际返回 `complete`，引用审计为 `not_run/passed=true`。** 因此“报告完成”不足以作为接入验收。
- 上述计算在内存中完成，未覆盖历史结果；输入文件摘要校验一致。结构化记录见 [本轮离线检查结果](harness2-assessment-checks.json)。

这些结果支持接入设计与风险判断，不能证明 harness2 已提高本项目的分析质量。

## 6. 证据定位

### 当前项目

| 编号 | 依据 |
|---|---|
| E1 | [prompts.py：固定资源加载和角色组合](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/prompts.py:13) |
| E2 | [workflow.py：角色构造、停用的最终评估](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/workflow.py:176)；[集中执行点](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/workflow.py:485)；[单包流程](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/workflow.py:674) |
| E3 | [logging_utils.py：紧凑日志](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/logging_utils.py:11) |
| E4 | [cli.py：确定性报告](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/cli.py:132)；[完整 DAG 执行](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/cli.py:195) |
| E5 | [knowledge_report.py：审计未执行的兼容分支](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/knowledge_report.py:189)；[报告状态](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/knowledge_report.py:254) |
| E6 | [material_inventory.py：索引与需求核对](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/material_inventory.py:352)；[llm.py：当前 Agent 未注册调查工具](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/llm.py:83) |
| E7 | [config.py：核心请求预算](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/config.py:84) |
| E8 | [testdata/manifest.json：单样本 16 个作业](D:/Work/01_processing/bounded_investigator_mvp/testdata/manifest.json:1) |
| E9 | [reconciliation.py：全局模型只接收精简事实与关系任务](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/reconciliation.py:226) |
| E10 | [pyproject.toml：当前项目 Python 声明](D:/Work/01_processing/bounded_investigator_mvp/pyproject.toml:10) |
| E11 | [dag.py：依赖引用和包数量契约](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/dag.py:26) |

### harness2 固定版本

| 编号 | 一手源码 |
|---|---|
| H1 | [core/spec.py：SubstrateLike、BenchmarkAdapter、模型通道](https://github.com/bg668/harness2/blob/1cfe254/harness2/core/spec.py#L190) |
| H2 | [demo.py：直接执行 Python 的适配器](https://github.com/bg668/harness2/blob/1cfe254/harness2/demo.py#L41)；[OpenCode 文件面](https://github.com/bg668/harness2/blob/1cfe254/harness2/substrate/opencode.py#L42) |
| H3 | [selection.py：只含 finals 的选择池](https://github.com/bg668/harness2/blob/1cfe254/harness2/core/selection.py#L93)；[k=1 无选择模型](https://github.com/bg668/harness2/blob/1cfe254/harness2/core/selection.py#L360) |
| H4 | [library.py：经验库、retain、add](https://github.com/bg668/harness2/blob/1cfe254/harness2/core/library.py#L79)；[schedule.py：运行调度与经验流](https://github.com/bg668/harness2/blob/1cfe254/harness2/core/schedule.py)；[context.py：经验供改进上下文使用](https://github.com/bg668/harness2/blob/1cfe254/harness2/core/context.py#L161) |
| H5 | [pyproject.toml：Python 版本要求](https://github.com/bg668/harness2/blob/1cfe254/pyproject.toml)；[README：安装与网络默认设置](https://github.com/bg668/harness2/blob/1cfe254/README.md) |
| H6 | [core/prompts.py：仅注册 opencode/codex 文件面说明](https://github.com/bg668/harness2/blob/1cfe254/harness2/core/prompts.py#L617) |
| H7 | [tree.py：autotrigger 的写入位置](https://github.com/bg668/harness2/blob/1cfe254/harness2/core/tree.py#L192)；[skill 名称与 frontmatter 校验](https://github.com/bg668/harness2/blob/1cfe254/harness2/core/tree.py#L267) |
| H8 | [recursion.py：两次阶段尝试](https://github.com/bg668/harness2/blob/1cfe254/harness2/core/recursion.py#L36)；[先 autotrigger 再验证](https://github.com/bg668/harness2/blob/1cfe254/harness2/core/recursion.py#L108) |
| H9 | [context.py：轨迹与产物写入编辑上下文](https://github.com/bg668/harness2/blob/1cfe254/harness2/core/context.py#L219) |
| H10 | [schedule.py：每任务重读 base_harness 并生成 k 个基线](https://github.com/bg668/harness2/blob/1cfe254/harness2/core/schedule.py#L616) |
