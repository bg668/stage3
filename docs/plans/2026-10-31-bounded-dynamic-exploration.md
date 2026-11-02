# 有界动态函数探索 Implementation Plan

> **实施状态（2026-11-02）：** 代码与离线验证已完成，完整回归 576 项通过、1 项跳过，安装包资源验证通过。样本 3 已真实尝试，但分析和规划均收到模型服务 HTTP 502，尚未完成真实闭环及业务效果验收。运行说明及限制见 [有界动态探索](../explore.md)，记录保存在 `results/sample3-explore-20261102-01`。现有静态运行保留作对照。

**Goal:** 在固定候选集合和样本预算内，先分析入口及重点函数的可见事实，再由 AI 根据问题价值选择后续函数，程序负责取材、证据、去重、停止和范围记录。

**Architecture:** 增加独立的 `explore` 模式，不替换现有静态 batch/baseline。新增一个串行探索循环，复用现有三角色 Workflow、StageRunner、SampleBudget、引用检查和全局整合。每次只执行一个有明确问题的任务，取得结果后再决定下一项。

**Tech Stack:** 现有 Python / PydanticAI / Pydantic / Typer / pytest；首版不增加框架依赖。

---

## 1. 当前代码依据

| 已核实情况 | 当前实现 | 方案影响 |
| --- | --- | --- |
| 跨包调用直接变为整包前置依赖 | [upstream/packets.py](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/upstream/packets.py:173) | 动态模式保留函数关联，不沿用整包前置分析 |
| 当轮全部就绪任务会先排入队列 | [orchestration/batch.py](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/orchestration/batch.py:198) | 新循环每次只选择并执行一个任务，结果回来后重新规划 |
| 缺失材料请求在批处理后汇总写文件 | [orchestration/batch.py](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/orchestration/batch.py:242) | 把 material_needs/handoffs 作为下一步规划的候选来源 |
| MaterialNeed 已有 targets、原因、影响和证据 | [schemas.py](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/schemas.py:130) | 复用该合同，不把它改成任意文件访问请求 |
| Workflow 支持独立包、依赖上下文和共享样本预算 | [workflow.py](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/workflow.py:209) | 单任务继续调用现有分析—审阅—必要时修订 |
| skill 加载器只服务 analyzer/reviewer/reviser | [harness/loader.py](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/harness/loader.py:13) | 规划器使用独立提示词；首版不扩充技能加载系统 |
| PacketCoverage 统计包交付，不是候选函数覆盖 | [evidence/scope.py](D:/Work/01_processing/bounded_investigator_mvp/src/bounded_investigator/evidence/scope.py:86) | 动态模式新增候选函数与探索范围记录，不能只统计已创建任务 |

样本3现有自动保留集合为100函数。入口F-0500所在J12的7个直接前置包，递归展开为11包、92主体函数。人工入口包只提供F-0500、F-0367、F-0373、F-0370，实际完成了入口分支和输出绑定分析，并保留了数据来源未知的限制。这里支持的是入口初析先行的可行性，不能据此承诺动态模式的成本或准确率。

## 2. 首轮范围与确定选择

1. 新增 `explore` 命令；既有 `prepare-packets`、`run`、`batch`、`baseline` 默认行为保持原样。
2. 首轮仍用样本3保存的preprocess/triage，不重新筛选，候选主体集合固定为100函数。
3. 入口由preprocess的 `is_entry` 识别，不硬编码F-0500；无入口时由规划器从保留集合选首项。多个入口逐项进入候选，不能一次全部展开。
4. 初析只处理少量重点函数，不要求先浅析全部100函数。
5. 每个任务以一个函数或其不可拆分的递归分量为主体；允许有理由地添加少量支持函数。支持函数既可来自保留集合，也可来自同一样本原始代码目录中未保留的函数。
6. 非保留函数作为支持材料查询/分析时单独记账，不改变原100函数的分母，不静默宣称上游筛选已覆盖它们。
7. 允许重访已分析函数，但须有新问题或相关新证据；同一问题、同一材料和相同相关证据状态不重复执行。首版每函数至多一次重访；无关任务完成或改写问题措辞不算新证据。
8. 每组仍为200,000输入＋输出tokens；分析、规划、格式修复共同使用前160,000，整合使用总余额，预留40,000。保持请求边界停止、跨线如实记录。
9. 首版最多6个分析轮次，每轮最多1个规划决策；这是有限试点的停止上限，后续根据实际规划成本调整。分析与模型请求均串行。
10. 本轮不接知识库、不接harness2、不执行二进制、不增加网络或资源自动获取。真实调用在实施验收阶段执行，本方案编写不调用项目模型。

## 3. 新流程

```text
冻结候选函数、源代码、配置和策略
              ↓
选择入口／重点函数，生成一个有界材料包
              ↓
现有 Workflow：分析 → 审阅 → 必要时修订
              ↓
保存事实、引用检查、未知点、材料请求和本轮成本
              ↓
规划器：基于现有证据和剩余预算，选择一个后续问题或停止
              ↓
程序校验目标、取材范围、重复任务及预算
              └──────────────→ 下一次有界分析

停止 → 整合已有结果 → 输出探索范围、未完成事项和报告
```

调用关系用于找线索和取材，不要求整个callee所在包先分析。若某项结论确实依赖一个未知辅助函数，则该结论保留未知或推测状态，后续只补与该问题有关的函数证据。真实缺失的材料不能靠解除依赖或模型推断补齐。

## 4. 数据与模型接口

### 4.1 SourceCatalog：可信材料目录

新增 `src/bounded_investigator/upstream/catalog.py`，从现有prepare_packets提取并复用样本身份、preprocess指纹、判定完整性/唯一性、代码长度和原文校验。

目录包括：selected_refs、函数ID/符号/地址、is_entry/is_export、筛选理由、调用关联、代码位置/指纹。建立函数ID与符号的精确映射；未知或歧义目标显式拒绝，不猜测函数号。开始运行时冻结允许读取的现存代码文件，不仅冻结已选中的100个函数；未保留函数若源代码缺失则记不可用，保留函数缺失仍预检失败。目录边界仅为指定上游产物，不扫描整个项目。

原有静态分包仍使用同一校验结果；SCC与原始行号处理继续复用。旧模式的材料内容、依赖规则和覆盖语义不随新模式变化。

### 4.2 探索状态与规划输出

新增 `src/bounded_investigator/orchestration/exploration_models.py`，与现有业务结果schema分离。

| 对象 | 必需内容 |
| --- | --- |
| ExplorationTask | task_id、overview/question、主体及支持函数、明确问题、对应目标、依据、已完成的上下文任务 |
| Candidate | 程序分配的candidate_id、primary_refs/support_refs、待回答问题、来源任务、相关证据ID、代码体量；由当前缺口及未访问重点组成 |
| PlanDecision | analyze/stop、candidate_id、选择理由、预期价值；stop时记录原因 |
| FunctionProgress | 原始候选/支持材料身份、送入过哪些任务、是否有有效初稿、是否完成审阅、待查问题 |

规划器选择程序提供的candidate_id，不能输出shell命令或任意文件路径。每个候选只提供紧凑元信息、相关已保存事实和缺口，不重新塞入完整源码、完整triage判定或全部历史输出。

候选须先按取材限制构造为可执行任务，再交给规划器选择。多目标MaterialNeed按函数/SCC拆成候选；支持函数由明确的请求和来源关系构造，不自动附上全部邻居。问题身份由来源调用点、规范函数目标和目标类别等稳定信息关联，不以自由描述文本作为唯一去重键。重复的旧材料请求不会因历史输出增加或换一种说法自动获得新的执行机会。

初析和深入分析继续输出 InvestigationResult，不要求新增一套行为结果schema。`complete_with_gaps`继续表达局部事实已分析、跨函数问题未闭合。初析的任务问题/深度写入材料包头部，分析、审阅和修订看到同一任务边界。

“已访问”“已调查”“材料可取得”“问题已解决”分开记录。首版不将一次分析成功或材料存在自动认定为业务问题已解决；问题闭合效果由评价端核对。规划器的“目标已足够”只是停止理由，不是程序出具的业务验收结论。

### 4.3 按需生成材料

在 `src/bounded_investigator/upstream/packets.py` 新增 `prepare_task_packet`，复用现有原文行编号和出处格式。

- 每个任务生成J0000、J0001等独立材料包，兼容现有J*.json扫描、引用和报告。
- 初始任务为入口完整源码及必要的函数关联表，不自动把所有callee正文加入。
- 后续由规划器选择主体问题及少量支持材料；全函数供给，不静默截断。沿用主体12,000字符与不可拆SCC超限单包规则；支持先采用最多8函数/12,000字符，由程序记录未提供项。
- 支持内容不再按函数编号从低到高机械填满：先按选中问题的有依据请求选材，再做数量和字符检查。
- 保存原文件、指纹和逐行映射；长路径、完整筛选理由放映射文件，提示词只保留必要定位和导航信息。
- 同一函数可以因新问题重访；“主体只出现一次”仅适用于旧静态分包。动态覆盖按唯一函数、重复体量和重访原因分别统计。

### 4.4 规划器与预算

新增 `prompts/exploration_planner.instructions.md`，通过现有build_stage_agent与StageRunner调用，输出PlanDecision。规划器不作为现有三个harness角色之一；其提示词另行冻结指纹。

`config.py`的StageName增加可选planner；旧命令不要求配置该stage，explore在任何模型调用前检查配置存在。

```yaml
stages:
  # 现有各stage保持原配置
  planner:
    model: investigator
    max_output_tokens: 1024
exploration:
  max_rounds: 6
```

规划器使用当前已配置模型profile，不另外假定某个便宜模型可用。每次规划最多1次模型请求，禁用其原生输出重试和模型格式修复；格式或选择无效时记录planning_failed并停止探索，仍尝试整合已有结果。这样不增加一个自动纠错重试循环。

规划器与分析器使用同一个SampleBudget、analysis阶段额度。所有已知失败用量、未知用量、原始输出和暂停原因按现有账本合同保留。预算停止不得触发修复。

## 5. 按任务实施

### Task 1：提取源目录与输入校验

**文件：** 新增catalog.py；调整upstream/packets.py；新增tests/test_source_catalog.py，保留test_upstream_pipeline.py。

1. 先写失败测试：样本3选中100个、入口标记来自数据、符号精确映射、歧义/缺失/重复/样本不匹配拒绝。
2. 运行测试确认未实现。
3. 抽取现有校验，静态分包调用新目录；不改变原筛选语义。
4. 验证原有100函数与静态分包回归；逐行原文、SCC与输出拒绝覆盖检查仍通过。

### Task 2：探索任务合同和材料构建

**文件：** 新增exploration_models.py；在packets.py新增prepare_task_packet；新增tests/test_exploration_materials.py。

1. 先写测试：入口无整包前置；选中函数可作为支持材料；未保留支持另统计；完整源码/原行号/递归分量不破坏。
2. 记录任务指纹：目标、问题、源码指纹和所用事实版本；同状态重复任务拒绝，新证据可支持重访。
3. 为每个已分配任务写材料和映射，写入问题/深度约束，处理源不可用与体量边界。
4. 验证编号J0000可进入现有引用解析、原文摘录、离线回放和报告。

### Task 3：有界规划器

**文件：** 调整config.py；新增规划提示词；在exploration.py中实现规划调用；新增tests/test_exploration_planner.py。

1. 先用固定响应测试选择一个合法候选或停止；未知目标、歧义符号、非法ID和无理由重访被拒绝。
2. 新增可选planner stage；保证旧配置和旧命令仍加载。
3. 使用StageRunner和单次UsageLimits执行规划，显式传入同一SampleBudget。
4. 验证规划费计入analysis；预算拒绝零额外调用；无效规划不触发格式修复；失败保留轨迹和已有分析。

### Task 4：串行探索闭环

**文件：** 新增orchestration/exploration.py；新增tests/test_exploration_runner.py。不改现有静态DAG调度器的语义。

核心流程：

```python
catalog = load_catalog(preprocess, triage)
state = create_state(catalog, budget, frozen_policies)
task = choose_entry_seed(catalog)  # 没有入口时才先调用规划器

while state.rounds < max_rounds and budget.remaining('analysis') > 0:
    if task is None:
        decision = await planner.choose(state.compact_snapshot())
        task = validate_and_build_task(decision, catalog, state)
    if task is None:
        break
    packet = prepare_task_packet(task, catalog, state)
    outcome = await workflow.run(
        packet_name=task.task_id, packet_text=packet.text,
        dependency_context=state.validated_context(task),
        dependency_ids=task.context_task_ids,
        artifact_dir=state.artifact_dir(task),
    )
    state.record_task_and_outcome(task, packet, outcome)
    state.save()
    task = None

await reconcile_available_results_and_save_scope(state)
```

每轮候选来自已完成任务的material_needs、handoffs、gaps，以及尚未访问的入口/重点候选。不是“所有材料请求自动递归执行”。存在性/可用性和业务价值分开判断。

只有通过现有检查的结果进入事实上下文；未完成审阅的草稿单独标识，不作为已解决问题的依据。失败任务保留诊断和待查状态；程序不能把预算拒绝当成“没有相关行为”。没有任何可用结构化分析结果时只保存执行摘要，不再为空结果启动整合模型；整合被配置关闭或余额不足时也须显式记录。

测试以固定响应展示：入口→请求写出函数→下一任务补证；根据新证据切换下一问题；同一问题/材料不循环；无关结果不解锁旧任务；相关新证据允许受限重访；轮次、规划失败、预算和正常停止均保存范围与结果。

### Task 5：冻结、覆盖和报告

**文件：** 探索状态由exploration.py生成；调整reporting/service.py、reporting/knowledge_report.py的可选参数；核对orchestration/replay.py的覆盖信息回放；新增tests/test_exploration_reporting.py。

运行开始冻结原始来源、保留集合、配置、模型、harness、规划提示词和执行策略；每个动态材料包在调用前另存其输入指纹。动态材料是在运行中生成的，不能宣称全部在运行前已冻结。

输出最少包括：

```text
experiment_manifest.json          来源、配置、策略、模型与初始指纹
source_catalog.json               固定候选及可用函数索引
input/J0000.md ...                实际执行材料
input/material_mapping.json       任务与原函数/原行映射
results/J0000.json ...            原有RunEnvelope
results/_artifacts/...            原始/审阅/修订/引用记录
results/budget.json               含规划的统一账本
results/exploration_events.jsonl  选择、执行、成本、结果和停止理由
results/exploration_scope.json    候选覆盖、支持材料、未访问、草稿和待查问题
results/_global_reconciliation.json
results/_report_ready.json
results/_final_report.md
```

全局整合继续使用已有Workflow。报告新增可选exploration_scope段：候选总数、已供给/已审阅函数、支持函数、重访、未访问/预算暂缓、模型停止理由。静态模式不传此参数，输出保持原语义。

整合成功不代表全样本完成；规划器停止也不能令程序把100函数全部标为已覆盖。原先任务留下的gaps保留历史出处，后续结果与这些问题建立关联，不能静默删除或把“材料已提供”等同于已解决。

report/回放若用于该运行，必须保留exploration_scope；如缺少原目录或覆盖状态，明确标记未知，不能再生成貌似全范围完成的报告。

### Task 6：命令与回归

**文件：** cli.py、config.example.yaml；新增tests/test_explore_cli.py；更新运行说明。

新增命令：

```powershell
.venv/Scripts/python.exe -m bounded_investigator explore `
  --preprocess reference/examples/out/debug_03/preprocess/preprocess.json `
  --triage reference/examples/out/debug_03/triage/triage.json `
  --config config.yaml `
  --output-dir results/sample3-explore-NEW `
  --total-tokens 200000
```

explore先做离线预检：来源一致、planner配置存在、输出是新目录且不嵌入输入、入口/候选可定位；通过后才构建模型。模型配置需事先补上planner阶段，不能原样执行当前缺失该阶段的配置后再临时猜测模型。

默认不生成HTML，不覆盖reference、人工材料或历史结果。既有baseline保持不变，首轮通过独立比较脚本读取冻结的静态基线与新运行，明确模型/配置/策略差异；不将历史响应混入新执行。

离线测试必须禁止外部模型请求。逐任务先看到失败，再实现最小功能并验证；最后运行tests目录完整回归与安装包资源检查。Windows异步测试使用本项目内新的basetemp，必要时沿用已验证的执行方式。

建议命令：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_source_catalog.py tests/test_exploration_materials.py tests/test_exploration_planner.py tests/test_exploration_runner.py tests/test_exploration_reporting.py tests/test_explore_cli.py -q --basetemp .pytest_cache/explore-UNIQUE
.venv/Scripts/python.exe -m pytest tests -q --basetemp .pytest_cache/explore-full-UNIQUE
```

以上新测试文件已创建并纳入完整回归。用户既有删除文件未恢复；未通过恢复无关文件取得测试通过。

## 6. 真实验收与评价

离线通过后，用当前有效模型配置和新目录运行样本3一次。所有规划、分析、修复均纳入200,000请求边界预算；先观察实际规划成本，不预先承诺更便宜。

工程验收：

- 第一项实际分析来源于入口元数据，不等待原J12的7个包，也不硬编码样本函数编号。
- 至少能演示一次结果驱动的后续选择；下一项不是固定编号顺序。
- 程序拒绝不在来源目录的目标、重复任务和预算外请求，保留每次选择和停止原因。
- 原文、来源映射、引用和预算账本可核对，草稿与已审阅结果分开。
- 未访问范围显式保留；已有结果可整合，不能把少量任务全部成功解释为全样本完成。

业务评价端另行核对：入口分支与条件、固定输出的来源绑定、首条凭据或注入证据链、检测线索价值、错误确认。比较首次得到关键结论的消耗、同预算下闭合问题、输入/规划成本与退步项，不以行为数量或任务数量替代准确率。

固定标准答案只用于评价端；不能放入规划上下文或来源目录。样本3用于开发和诊断，现有其他样本在同口径基线准备后用于留出验证。

## 7. 后续接入位置

先完成这套可运行、可观察的闭环。后续知识卡只补规划/分析所需的少量上下文；skill沉淀实际纠错原则；harness2在离线侧提出候选并利用这套运行器验证。首版不提前增加相关依赖或扩展优化器权限。
