<repair target_stage="{{target_stage}}" attempt="{{attempt}}">
<TARGET_SCHEMA>
{{target_schema}}
</TARGET_SCHEMA>

<VALIDATION_ERROR>
{{validation_error}}
</VALIDATION_ERROR>

<ORIGINAL_RAW_OUTPUT>
{{original_raw_output}}
</ORIGINAL_RAW_OUTPUT>

<CURRENT_CANDIDATE>
{{current_candidate}}
</CURRENT_CANDIDATE>
</repair>

返回修复后的 JSON 对象；如果无法在不编造语义的前提下修复，返回 __repair_failed__。
