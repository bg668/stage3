"""Thin command-line adapter for stage-1 preprocessing."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import view
from .application import PreprocessOptions, run_preprocess
from .source import SourceError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="preprocess",
        description="阶段 1 预处理:DeepExtract sqlite -> 分诊可消费的数据(零 LLM)",
    )
    parser.add_argument("--sqlite", required=True, type=Path, help="DeepExtract 导出的 sqlite")
    parser.add_argument("--out", required=True, type=Path, help="输出目录")
    parser.add_argument("--key-functions", type=Path, default=None,
                        help="关键函数地址清单,每行一个地址")
    parser.add_argument("--sample-name", default=None)
    parser.add_argument("--max-functions-per-unit", type=int,
                        default=view.DEFAULT_MAX_FUNCTIONS_PER_UNIT)
    parser.add_argument("--max-chars-per-unit", type=int,
                        default=view.DEFAULT_MAX_CHARS_PER_UNIT)
    parser.add_argument("--trivial-max-size", type=int, default=64,
                        help="r006_trivial_leaf 的体量阈值")
    parser.add_argument("--code-view", default=view.DEFAULT_CODE_VIEW,
                        choices=view.CODE_VIEW_MODES,
                        help="特征块里的代码片段形态。full=默认,单函数有界全文;"
                             "minimal=头2尾1;landmark=按体量分档+地标提取")
    parser.add_argument(
        "--remove-variable-declarations",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="渲染 unit 前去除函数体开头的局部变量声明(默认开启)",
    )
    parser.add_argument(
        "--function-code-max-chars",
        type=int,
        default=view.DEFAULT_FUNCTION_CODE_MAX_CHARS,
        help="full 视图单函数最多写入的代码字符数",
    )
    parser.add_argument("--compare-code-views", action="store_true",
                        help="输出三种 code-view 的装箱对照表(零 LLM),不写 units/")
    parser.add_argument("--no-code-dump", action="store_true",
                        help="特殊用途:不导出 code/ 目录;默认始终导出完整代码")
    parser.add_argument(
        "--no-capability-rules",
        action="store_true",
        help="关闭 triage 前的最小可疑能力规则",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    options = PreprocessOptions(
        sqlite=args.sqlite,
        out=args.out,
        key_functions=args.key_functions,
        sample_name=args.sample_name,
        max_functions_per_unit=args.max_functions_per_unit,
        max_chars_per_unit=args.max_chars_per_unit,
        trivial_max_size=args.trivial_max_size,
        code_view=args.code_view,
        remove_variable_declarations=args.remove_variable_declarations,
        function_code_max_chars=args.function_code_max_chars,
        compare_code_views=args.compare_code_views,
        no_code_dump=args.no_code_dump,
        capability_rules_enabled=not args.no_capability_rules,
    )
    try:
        result = run_preprocess(options)
    except SourceError as exc:
        print("读取失败: %s" % exc, file=sys.stderr)
        return 2

    print(result.summary)
    print("输出目录: %s" % options.out)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
