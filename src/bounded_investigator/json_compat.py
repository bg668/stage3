from __future__ import annotations

"""Chat gateway compatibility for JSON-object structured output.

Some OpenAI-compatible endpoints inspect *message text*, not JSON schema
metadata, when `text.format=json_object` is enabled. PydanticAI's output
schema alone may not satisfy their literal 'json' keyword check.
"""


def ensure_json_keyword(prompt: str, output_mode: str) -> str:
    if output_mode in {"native", "prompted"} and "json" not in prompt.casefold():
        return "Return exactly one valid JSON object matching the output schema.\n\n" + prompt
    return prompt
