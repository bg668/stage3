"""Independent CLI for Stage-2 two-round triage."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .application import TriageOptions, run_triage
from .domain import TriageConfig
from .input import InputContractError
from .llm_config import LLMConfigError
from .provider_factory import build_llm_provider_pair
from .provider_health import ProviderPreflightError
from .reporting.final_summary_renderer import render_final_summary
from .providers.scripted import ScriptedConfigError, ScriptedProvider

ROUND1_LLM_TASK = "triage.round1"
ROUND2_LLM_TASK = "triage.round2"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="triage",
        description=(
            "阶段 2 快速分诊: Round1 + 确定性 second-hop 装配 + Round2 二值收敛"
        ),
    )
    parser.add_argument("--preprocess", required=True, type=Path, help="阶段 1 preprocess.json")
    parser.add_argument("--out", required=True, type=Path, help="阶段 2 输出目录")
    parser.add_argument(
        "--units-dir",
        type=Path,
        default=None,
        help="默认使用 preprocess.json 同级 units/",
    )
    parser.add_argument(
        "--code-dir",
        type=Path,
        default=None,
        help="默认使用 preprocess.json 同级 code/",
    )

    provider_group = parser.add_mutually_exclusive_group(required=True)
    provider_group.add_argument(
        "--llm-config",
        type=Path,
        help="LLM registry JSON；分别由 tasks.triage.round1/round2 选择 model profile",
    )
    provider_group.add_argument(
        "--scripted-verdicts",
        type=Path,
        help=(
            "测试专用：同一 JSON 同时提供 Round1 unit_id 和 Round2 的 R2:F-xxxx verdicts"
        ),
    )
    parser.add_argument(
        "--model-profile",
        default=None,
        help="仅真实 LLM：临时覆盖 tasks.triage.round1 的 model profile",
    )
    parser.add_argument(
        "--round2-model-profile",
        default=None,
        help="仅真实 LLM：临时覆盖 tasks.triage.round2 的 model profile",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=3,
        help="每轮最大并发 provider 调用数，默认 3",
    )
    parser.add_argument(
        "--max-attempts-per-task",
        type=int,
        default=2,
        help="每个 task 最大业务调用次数，默认 2（首次 + 1 次重试）",
    )
    parser.add_argument(
        "--provider-probe-timeout-seconds",
        type=float,
        default=30.0,
        help="启动前模型服务探活超时秒数，默认 30",
    )
    parser.add_argument(
        "--provider-request-timeout-seconds",
        type=float,
        default=120.0,
        help="单次真实 LLM 请求超时秒数，默认 120",
    )
    parser.add_argument(
        "--provider-recovery-max-attempts",
        type=int,
        default=6,
        help="Provider 临时不可用后的共享恢复尝试次数，默认 6",
    )
    parser.add_argument(
        "--provider-recovery-initial-backoff-seconds",
        type=float,
        default=2.0,
        help="Provider 恢复首次退避秒数，默认 2",
    )
    parser.add_argument(
        "--provider-recovery-max-backoff-seconds",
        type=float,
        default=20.0,
        help="Provider 恢复单次最大退避秒数，默认 20",
    )
    parser.add_argument(
        "--second-hop-max-chars",
        type=int,
        default=48_000,
        help="第二跳 supplement 代码总字符预算，不包含 SUBJECT 完整代码，默认 48000",
    )
    parser.add_argument(
        "--second-hop-max-related-functions",
        type=int,
        default=8,
        help="每个第二跳包最多补充的关联函数数，默认 8",
    )
    parser.add_argument(
        "--second-hop-max-code-chars-per-related-function",
        type=int,
        default=12_000,
        help="每个关联函数最多补充的代码字符数，默认 12000",
    )
    parser.add_argument(
        "--agent-name",
        default="员工1号",
        help="实时工作状态中的主体名称，默认 员工1号",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (
        args.max_concurrency < 1
        or args.max_attempts_per_task < 1
        or args.second_hop_max_chars < 1
        or args.second_hop_max_related_functions < 1
        or args.second_hop_max_code_chars_per_related_function < 1
        or args.provider_probe_timeout_seconds <= 0
        or args.provider_request_timeout_seconds <= 0
        or args.provider_recovery_max_attempts < 1
        or args.provider_recovery_initial_backoff_seconds <= 0
        or args.provider_recovery_max_backoff_seconds <= 0
        or args.provider_recovery_max_backoff_seconds
        < args.provider_recovery_initial_backoff_seconds
    ):
        print("输入/配置失败: 并发、尝试次数、超时和第二跳预算参数必须 > 0", file=sys.stderr)
        return 2
    try:
        if args.llm_config is not None:
            selection = build_llm_provider_pair(
                args.llm_config,
                round1_task_name=ROUND1_LLM_TASK,
                round2_task_name=ROUND2_LLM_TASK,
                round1_model_profile=args.model_profile,
                round2_model_profile=args.round2_model_profile,
                max_concurrency=args.max_concurrency,
                max_attempts_per_task=args.max_attempts_per_task,
                second_hop_max_chars=args.second_hop_max_chars,
                second_hop_max_related_functions=args.second_hop_max_related_functions,
                second_hop_max_code_chars_per_related_function=(
                    args.second_hop_max_code_chars_per_related_function
                ),
                provider_probe_timeout_seconds=args.provider_probe_timeout_seconds,
                provider_request_timeout_seconds=args.provider_request_timeout_seconds,
                provider_recovery_max_attempts=args.provider_recovery_max_attempts,
                provider_recovery_initial_backoff_seconds=(
                    args.provider_recovery_initial_backoff_seconds
                ),
                provider_recovery_max_backoff_seconds=(
                    args.provider_recovery_max_backoff_seconds
                ),
            )
            round1_provider = selection.round1_provider
            round2_provider = selection.round2_provider
            config = selection.config
        else:
            if args.model_profile is not None or args.round2_model_profile is not None:
                raise LLMConfigError(
                    "--model-profile/--round2-model-profile 只能与 --llm-config 一起使用"
                )
            assert args.scripted_verdicts is not None
            scripted = ScriptedProvider.from_path(args.scripted_verdicts)
            round1_provider = scripted
            round2_provider = scripted
            config = TriageConfig(
                provider="scripted",
                provider_name="scripted",
                llm_task=ROUND1_LLM_TASK,
                model_profile="scripted-v1",
                model_id="scripted/v1",
                round2_provider_name="scripted",
                round2_llm_task=ROUND2_LLM_TASK,
                round2_model_profile="scripted-v1",
                round2_model_id="scripted/v1",
                max_concurrency=args.max_concurrency,
                max_attempts_per_task=args.max_attempts_per_task,
                provider_probe_timeout_seconds=args.provider_probe_timeout_seconds,
                provider_request_timeout_seconds=args.provider_request_timeout_seconds,
                second_hop_max_chars=args.second_hop_max_chars,
                second_hop_max_related_functions=args.second_hop_max_related_functions,
                second_hop_max_code_chars_per_related_function=(
                    args.second_hop_max_code_chars_per_related_function
                ),
                round2_enabled=True,
            )

        result = run_triage(
            TriageOptions(
                preprocess=args.preprocess,
                out=args.out,
                units_dir=args.units_dir,
                code_dir=args.code_dir,
                config=config,
                actor_name=args.agent_name,
                activity_stream=sys.stdout,
            ),
            round1_provider,
            round2_provider,
        )
    except ProviderPreflightError as exc:
        print("模型服务检查失败: %s" % exc, file=sys.stderr)
        return 3
    except (InputContractError, ScriptedConfigError, LLMConfigError, ValueError) as exc:
        print("输入/配置失败: %s" % exc, file=sys.stderr)
        return 2

    if not render_final_summary(result.payload, args.out, stream=sys.stdout):
        print(result.summary)
        print("输出目录: %s" % args.out)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
