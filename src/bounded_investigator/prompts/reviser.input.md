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

仅修复审查指出的实质问题，返回精简最终结果；依赖摘要只能用于补足上游语义，不得据此编造当前包不存在的行为。
