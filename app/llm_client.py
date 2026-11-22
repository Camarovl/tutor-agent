"""模型调用封装 —— 全项目唯一允许发起网络请求的文件。

负责人：B
契约：docs/interfaces.md 第三节「AI 能力层」

硬性约束（分层不许打穿）：
- 只有本文件可以 import openai、发起网络请求
- qa.py / intake.py 只调用这里的函数，不直接碰 SDK
- 提示词一律写在 prompts.py，本文件不写业务话术

当前阶段：第 4 步。只做文本对话，vision() 留到第 5 步实测后再实现。
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 配置：三项必填，没有默认值（代码里不许写死地址或密钥）
# ---------------------------------------------------------------------------

REQUIRED_ENV = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")

_DEFAULT_TIMEOUT = 120.0
_DEFAULT_MAX_TOKENS = 4000
_DEFAULT_TEMPERATURE = 0.0


class LLMError(Exception):
    """模型调用失败。上层负责降级，不要把异常直接抛到界面。"""


@dataclass
class LLMResult:
    """一次调用的结果。字段名与契约一致。"""

    text: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    retried: bool = False
    success: bool = True
    raw: Any = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# 环境变量
# ---------------------------------------------------------------------------


def missing_config_keys() -> list[str]:
    """返回缺失的必填配置项，全部齐全时返回空列表。"""
    return [key for key in REQUIRED_ENV if not os.getenv(key, "").strip()]


def is_configured() -> bool:
    return not missing_config_keys()


def _require_configured() -> None:
    missing = missing_config_keys()
    if missing:
        raise LLMError(
            "缺少模型服务配置：" + ", ".join(missing) + "。请检查项目根目录的 .env"
        )


def _load_dotenv_once() -> None:
    """尽力加载 .env；没有安装 python-dotenv 或没有文件时静默跳过。

    界面入口（streamlit_app.py）同样会加载一次，重复调用无副作用。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - 依赖缺失时不影响导入
        return
    try:
        load_dotenv(override=False)
    except Exception:  # pragma: no cover - .env 格式异常不该阻断导入
        pass


_load_dotenv_once()

# ---------------------------------------------------------------------------
# 客户端（懒加载，避免仅仅 import 就建立连接）
# ---------------------------------------------------------------------------

_client: Any = None


def _get_client():
    global _client
    _require_configured()
    if _client is None:
        from openai import OpenAI

        _client = OpenAI(
            base_url=os.getenv("LLM_BASE_URL"),
            api_key=os.getenv("LLM_API_KEY"),
            timeout=float(os.getenv("LLM_TIMEOUT") or _DEFAULT_TIMEOUT),
        )
    return _client


def _max_tokens() -> int:
    try:
        return int(os.getenv("LLM_MAX_TOKENS") or _DEFAULT_MAX_TOKENS)
    except ValueError:
        return _DEFAULT_MAX_TOKENS


def _temperature() -> float:
    try:
        return float(os.getenv("LLM_TEMPERATURE") or _DEFAULT_TEMPERATURE)
    except ValueError:
        return _DEFAULT_TEMPERATURE


# ---------------------------------------------------------------------------
# 消息与输出解析
# ---------------------------------------------------------------------------


def _guard_messages(messages: list[dict]) -> None:
    """🔴 发给模型的序列不能以 assistant 开头。

    部分模型的 chat template 会直接报 "No user query found in messages"。
    开场白只做界面渲染、不进历史，这里做最后一道拦截。
    """
    if not messages:
        raise LLMError("messages 不能为空")
    if messages[0].get("role") == "assistant":
        raise LLMError(
            "messages 不能以 assistant 开头（开场白不要写进对话历史）"
        )


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def extract_json(text: str) -> dict | None:
    """从模型输出里抠出一个 JSON 对象。

    容忍三种常见情况：纯 JSON、被 ``` 包裹、前后夹带解释文字。
    """
    if not text:
        return None

    candidates: list[str] = []
    fenced = _FENCE_RE.findall(text)
    candidates.extend(block.strip() for block in fenced)
    candidates.append(text.strip())

    # 兜底：截取第一个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _usage_of(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}

    def _int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    details = getattr(usage, "completion_tokens_details", None)
    return {
        "prompt_tokens": _int(getattr(usage, "prompt_tokens", 0)),
        "completion_tokens": _int(getattr(usage, "completion_tokens", 0)),
        "reasoning_tokens": _int(getattr(details, "reasoning_tokens", 0)),
    }


def _empty_content_error(choice: Any, usage: dict[str, int]) -> LLMError:
    """把「返回空内容」翻译成人能看懂的提示。"""
    finish = getattr(choice, "finish_reason", None)
    reasoning = usage.get("reasoning_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    if finish == "length" and reasoning and completion and reasoning >= 0.9 * completion:
        return LLMError(
            "模型返回空内容：token 全花在内部推理上了"
            f"（completion={completion}, reasoning={reasoning}）。"
            "请调大 LLM_MAX_TOKENS，或换非思考模型。"
        )
    return LLMError(f"模型返回空内容（finish_reason={finish}）")


def _call_once(
    messages: list[dict],
    *,
    purpose: str,
    json_mode: bool,
) -> tuple[str, dict[str, int], Any, Any]:
    client = _get_client()
    kwargs: dict[str, Any] = {
        "model": os.getenv("LLM_MODEL"),
        "messages": messages,
        "temperature": _temperature(),
        "max_tokens": _max_tokens(),
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as exc:  # 网络、鉴权、限流都归到这里
        raise LLMError(f"模型调用失败（{purpose}）：{exc}") from exc

    if not getattr(response, "choices", None):
        raise LLMError(f"模型没有返回任何候选结果（{purpose}）")

    choice = response.choices[0]
    text = (choice.message.content or "") if getattr(choice, "message", None) else ""
    usage = _usage_of(response)
    return text, usage, choice, response


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def chat(
    messages: list[dict],
    *,
    purpose: str,
    json_mode: bool = False,
    schema_hint: str | None = None,
    validator: Callable[[dict], str | None] | None = None,
    max_retry: int = 2,
) -> LLMResult:
    """主入口。失败抛 LLMError，由上层决定降级方式。

    validator: 输入解析后的 dict；合规返回 None，不合规返回错误描述。
               不合规时会把原因回喂给模型重试，最多 max_retry 次。
    """
    _require_configured()
    convo = [dict(m) for m in messages]
    _guard_messages(convo)

    attempts = max(0, int(max_retry))
    last_problem = ""

    for attempt in range(attempts + 1):
        text, usage, choice, response = _call_once(
            convo, purpose=purpose, json_mode=json_mode
        )

        if not text.strip():
            raise _empty_content_error(choice, usage)

        needs_check = json_mode or validator is not None
        if not needs_check:  #validator=none时，但是json_mode=true时应该跑解析错误
            return LLMResult(
                text=text,
                retried=attempt > 0,
                success=True,
                raw=response,
                **usage,
            )

        data = extract_json(text)
        problem: str | None
        if data is None:
            problem = "返回内容里没有可解析的 JSON 对象"
        elif validator is not None:
            problem = validator(data)
        else:
            problem = None

        if problem is None:
            return LLMResult(
                text=text,
                retried=attempt > 0,
                success=True,
                raw=response,
                **usage,
            )

        last_problem = problem
        if attempt >= attempts:
            break

        # 把失败原因回喂，比盲目重问成功率高得多
        convo = convo + [
            {"role": "assistant", "content": text},
            {
                "role": "user",
                "content": (
                    f"上面的输出不合格：{problem}。"
                    "请只输出修正后的 JSON 对象，不要任何解释文字。"
                    + (f"\n必须符合的结构：{schema_hint}" if schema_hint else "")
                ),
            },
        ]

    raise LLMError(
        f"模型输出连续 {attempts + 1} 次未通过校验（{purpose}）：{last_problem}"
    )





# ---------------------------------------------------------------------------
# 自检：python -m app.llm_client [--live]
# ---------------------------------------------------------------------------


def _ensure_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，输出 emoji 会抛 UnicodeEncodeError。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _self_check(live: bool) -> int:
    _ensure_utf8_stdout()
    print("=" * 50)
    print("模型服务配置检查")
    print("=" * 50)

    for key in REQUIRED_ENV:
        value = os.getenv(key, "").strip()
        if key == "LLM_API_KEY":
            shown = f"已设置（{len(value)} 字符）" if value else "❌ 缺失"
        else:
            shown = value or "❌ 缺失"
        print(f"  {key:<15}: {shown}")

    for key in ("LLM_TIMEOUT", "LLM_MAX_TOKENS", "LLM_TEMPERATURE"):
        print(f"  {key:<15}: {os.getenv(key) or '(用默认值)'}")

    missing = missing_config_keys()
    if missing:
        print(f"\n❌ 缺少必填项：{', '.join(missing)}")
        print("   检查项目根目录的 .env 是否与本文件同级目录存在。")
        return 1

    print("\n✅ 三项必填配置齐全")

    if not live:
        print("\n提示：加 --live 会真实调用一次模型（消耗少量 token）。")
        return 0

    import time

    print("\n正在真实调用模型……")
    started = time.perf_counter()
    try:
        result = chat(
            [{"role": "user", "content": "只回答两个字：收到"}],
            purpose="self-check",
        )
    except LLMError as exc:
        print(f"❌ 调用失败：{exc}")
        return 2

    elapsed = time.perf_counter() - started
    print(f"✅ 调用成功，用时 {elapsed:.2f}s")
    print(f"   回复：{result.text.strip()[:200]}")
    print(f"   token：prompt={result.prompt_tokens} completion={result.completion_tokens}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK
    except Exception:
        pass
    raise SystemExit(_self_check("--live" in sys.argv))
