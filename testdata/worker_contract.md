# Bounded Investigation Worker Contract v4

输入是一个边界明确的代码材料包，可同时包含已完成上游包的 `dependency_handoffs`。Worker 必须完整浏览当前包材料，但只输出会影响恶意性、行为条件、数据流、关键 artifact 或分析边界的高价值事实。

输出由 Pydantic schema 强制为四组核心事实和一组按需补料需求：

- `claims`: condition → source → action → sink → evidence 的核心行为结论，必须单独标注 `evidence_level`（confirmed / suspected / lead）；不要把 confidence 当作证据闭合程度。
- `artifacts`: 代码中出现的重要制品，同时给出 `role` 与 `detection_specificity`。
- `gaps`: 当前材料和依赖摘要仍无法闭合的问题。
- `handoffs`: 建议 DAG 下游/补料阶段继续处理的函数或数据，不由当前 Worker 自行无限追踪。
- `material_needs`（可为空）：基于代码证据发现的精确函数/资源/数据/运行记录需求；不是预设样本目标或材料缺失结论。

## DAG handoff

上游结果不以完整 JSON 注入下游，而由程序投影为紧凑摘要：`summary + claims + artifacts + unresolved gaps/handoffs`，并保留来源包 ID。依赖失败不会拖垮整个 DAG；下游会收到 `dependency unavailable` 标记并继续分析自身材料。

## Artifact role

- `created_output`: 样本产生的输出文件/数据。
- `created_mutex`: 样本创建/检查的互斥体。
- `persistence_value`: 持久化键值/派生项。
- `config_marker`: 资源名、分隔符、特异 GUID 等静态配置标记。
- `read_source`: 被样本读取的数据源。
- `environment_probe`: VM/调试器/环境探测对象。
- `system_path`: 通用系统路径。
- `parser_marker`: 内部解析标记。
- `other`: 其他制品。

## Detection specificity

`role` 表示行为角色，`detection_specificity` 表示作为感染/检测指标的价值，两者正交：

- `high`: 强样本/家族/感染特异性，可直接或近似直接用于高价值检测。
- `medium`: 与行为、进程或其他 artifact 组合后具有较好检测价值。
- `low`: 通用、弱相关，只能作为辅助上下文。
- `none`: 正常系统对象/数据源/环境探针等，不应作为感染 IOC。

Reviewer 只有在错误会改变以下内容时才允许判 major/critical 并触发 Reviser：核心行为存在性、触发条件、source/action/sink、关键 artifact role/specificity、证据边界。普通 cleanup、SEH、局部内存管理等默认是 minor。
