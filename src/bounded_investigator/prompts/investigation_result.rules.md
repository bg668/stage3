InvestigationResult 公共质量规则：Analyzer、Reviser 按此生成分析结果，Reviewer 按此验收分析结果；各角色的输出结构与职责仍由角色说明规定。

1. 材料边界：只使用本次材料包中的源码、注释、任务说明，以及明确标记的 dependency_handoffs；不用家族常识或猜测补齐事实。dependency_handoffs 是已验证的上游事实，可解释调用来源、数据含义和触发条件，不能替代当前包新行为所需的证据，也不能被重复包装为本包新发现的 claim。
2. 核心内容：只输出影响恶意性、行为条件、source → action → sink、关键 artifact 或当前边界判断的内容。每条 claim 尽量闭合 condition → source → action → sink → evidence；缺失的关键关系必须在 limitations 中说明。不复述函数清单、material_map、普通 cleanup、SEH 或无关运行时细节，除非它们会改变核心结论。
3. 引用准确性：每条 claim、artifact、material_need 都须有对应证据。evidence.location 统一使用 F-XXXX Lx[-Ly]，例如 F-0483 L122-L153；函数名可以写在 observation 中，不得插在编号与行段之间。引用已验证依赖事实时，复用 dependency_handoffs 中该事实的原始源码位置；不得使用 dependency J00 summary、claim 1、材料包标题或摘要中的节作为 location。依赖只提供语义而未提供原始位置时，将其用于上下文，当前包新行为仍须引用当前材料；不编造行号补齐。不得扩大引用范围，或把材料包/报告的排版行号冒充原始源码行号。evidence.observation 必须描述该位置实际可见的内容，并支持所关联的行为、制品或需求。纠正引用应重新核对来源，不能通过改写 observation 去迁就错误的 location。
4. 证据等级与置信度独立：对每条 claim 填写 evidence_level，不能从 confidence 自动推定。
   - confirmed：本次代码或已验证依赖证据直接支持所描述的行为能力，无需证明运行时一定成功。
   - suspected：有具体证据指向该行为，但关键 source/action/sink 或触发关系尚未闭合；在 limitations 指出具体限制。
   - lead：仅有初始线索，不足以形成行为结论；优先写入 gaps/handoffs，保留为 claim 时不得写成已确认行为。
   - unclassified：历史数据的未分类状态，不等于 confirmed。接收历史结果时不得仅凭 confidence 自动升级；生成或修订 claim 时应根据证据明确分类。
   不得仅因为 API 名称可疑就填写 confirmed，也不得把 confidence=high 当作已确认标签。
5. 能力与效果：不把“代码具备能力”写成“运行时一定成功”。静态代码不能直接证明的运行结果应明确限制。
6. 制品粒度：一个 ArtifactFinding 只能保存一个具体对象；不得将多个文件、互斥体、注册表路径或 IOC 拼入同一 value，多个具体值应拆为独立 artifact。
7. 制品分类：分别判断 role 与 detection_specificity，两个维度不绑定。
   - role 表示行为角色：created_output / created_mutex / persistence_value / config_marker / read_source / environment_probe / system_path / parser_marker / other。
   - detection_specificity 表示感染/检测价值，不是字符串是否罕见：high 为可单独或近似单独使用的固定输出、固定互斥体、样本/家族强特异资源名/分隔符/GUID 等；medium 为需结合进程、行为或其他制品才有较好检测价值；low 为通用、弱相关的辅助上下文；none 为不应作为感染 IOC 的正常系统路径、通用读取源、环境探针目标或解析内部标记等。
   不因 role=read_source 就机械判 none，也不因 role=persistence_value 就机械判 high；config_marker 可以是 high，persistence_value 也可能是 low/none。
8. 缺口与交接：依赖包外函数、数据或截断源码时记录 gaps/handoffs，不猜测答案；依赖摘要已经回答的上游语义不重复报为缺口。gap 应填写 gap_type、question、reason、scope、impact；handoff 应填写 target、reason、expected_value、priority。outside_packet 问题只能作为 gaps/handoffs/material_needs 记录，不把未知包外行为写成已确认事实。
9. 补料需求：关键结论确实依赖未读材料时输出有证据支持的 material_needs。function_code 优先使用结构化 targets 数组，每个元素只填写一个 identifier（F-XXXX 或 sub_地址），需要行段时同时填写 start_line/end_line，使用原始源码行号；多个函数不得拼入一个 target。其他材料使用单个 target。保留 kind、reason、impact 及完整 evidence（location 与 observation）。只记录需求，不擅自断言材料已从原始样本中消失；不要求列出所有普通 helper。
10. 状态一致性：status=complete 仅用于当前目标已闭合且 gaps、handoffs、material_needs 均为空；有明确缺口用 complete_with_gaps；核心目标无法判断才用 blocked。summary、claims、limitations 与缺口、补料需求及 status 应相互一致。
