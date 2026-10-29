<packet name="{{packet_name}}">
<dependency_handoffs>
{{dependency_context}}
</dependency_handoffs>

<material>
{{packet_text}}
</material>

<analyzer_result>
{{draft_context}}
</analyzer_result>

<evidence_reference_validation>
{{evidence_validation_context}}
</evidence_reference_validation>

<coverage_review>
{{review_context}}
</coverage_review>
</packet>

按程序生成的最终允许范围修订，返回完整最终结果；可以精简文字，不得省略条目。未删除的原条目逐项保留，新增条目只追加到末尾，所有修订路径按 Analyzer 原始零基编号解释。依赖摘要只能用于补足上游语义，不得据此编造当前包不存在的行为。
