"""模型调用封装 —— 全项目唯一允许发起网络请求的文件。

负责人：B
契约：docs/interfaces.md 第三节「AI 能力层」

硬性约束（分层不许打穿）：
- 只有本文件可以 import openai、发起网络请求
- qa.py / intake.py 只调用这里的函数，不直接碰 SDK
- 提示词一律写在 prompts.py，本文件不写业务话术

当前实现范围：
- 文本：chat()，含 JSON 校验失败后的"带原因回喂重试"
- 图片：vision()，模型不支持图片时抛 VisionUnsupported，由 intake 降级成手输
- 记账：每次调用结束写一行 llm_log（db 不可用时静默跳过）
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

#: 本文件版本号。提示词/调用行为有较大变化时手动 +1，便于在群里对齐"你跑的是哪版"
__version__ = "0.5.0"

# ---------------------------------------------------------------------------
# 配置：三项必填，没有默认值（代码里不许写死地址或密钥）
# ---------------------------------------------------------------------------

REQUIRED_ENV = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")

_DEFAULT_TIMEOUT = 120.0
_DEFAULT_MAX_TOKENS = 4000
_DEFAULT_TEMPERATURE = 0.0


class LLMError(Exception):
    """模型调用失败。上层负责降级，不要把异常直接抛到界面。"""


class VisionUnsupported(LLMError):
    """模型/网关不支持图片输入。

    单独一个类是为了让 intake.analyze_image 能精确捕获它并降级成手输，
    而不是把"图片不支持"和"网络超时"混在一起处理。
    """


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


_dotenv_status = "unknown"


def _load_dotenv_once() -> None:
    """尽力加载 .env；没有安装 python-dotenv 或没有文件时跳过。

    界面入口（streamlit_app.py）同样会加载一次，重复调用无副作用。
    加载状态记在 _dotenv_status，供自检给出准确提示——
    否则「没激活虚拟环境」会被误报成「配置缺失」。
    """
    global _dotenv_status
    try:
        from dotenv import load_dotenv
    except ImportError:  # 依赖缺失时不影响导入，但自检要能说清楚
        _dotenv_status = "missing"
        return
    _dotenv_status = "ok"
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
# 调用记账
# ---------------------------------------------------------------------------


def _log_call(
    purpose: str,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    retried: int = 0,
    success: bool = True,
) -> None:
    """把一次调用记进 llm_log 表。

    🔴 为什么放在这里：db.log_llm_call() 的 docstring 写明"由 llm_client 在每次调用结束后调用"。
       放在 chat()/vision() 里，才能保证不漏记（上层只关心业务，不该记着记账）。

    ⚠️ 记账失败绝不能影响主流程：库还没建、表还没迁移时静默跳过。
       宁可丢一条统计，也不要让一次问答因为"写日志失败"而废掉。
    """
    try:
        from app import db

        db.log_llm_call(
            purpose or "unknown",
            int(prompt_tokens or 0),
            int(completion_tokens or 0),
            int(retried or 0),
            1 if success else 0,
        )
    except Exception:  # noqa: BLE001 —— 记账是旁路，永不向上抛
        pass


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
# 对外接口：文本
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

    validator: 输入解析后的 dict；合规返回 None，不合规返回错误描述字符串。
               不合规时把原因回喂给模型重试，最多 max_retry 次（默认 2 → 共 3 次尝试）。

    :param purpose: 调用用途，写进错误信息与 llm_log（qa / intake / practice / self-check）
    """
    _require_configured()
    convo = [dict(m) for m in messages]
    _guard_messages(convo)

    attempts = max(0, int(max_retry))
    last_problem = ""

    try:
        for attempt in range(attempts + 1):
            text, usage, choice, response = _call_once(
                convo, purpose=purpose, json_mode=json_mode
            )

            if not text.strip():
                raise _empty_content_error(choice, usage)

            needs_check = json_mode or validator is not None
            if not needs_check:
                _log_call(
                    purpose,
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                    retried=attempt,
                    success=True,
                )
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
                _log_call(
                    purpose,
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                    retried=attempt,
                    success=True,
                )
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
    except LLMError:
        _log_call(purpose, success=False)
        raise

    _log_call(purpose, success=False)
    raise LLMError(
        f"模型输出连续 {attempts + 1} 次未通过校验（{purpose}）：{last_problem}"
    )


# ---------------------------------------------------------------------------
# 对外接口：图片
# ---------------------------------------------------------------------------

#: 报错里出现这些字样时，判定为"模型不支持图片"，交给 intake 降级成手输。
#: 这是启发式判断，偏脆弱 —— 已记录在 docs/limitations.md 第 13 条。
_VISION_UNSUPPORTED_HINTS = (
    "does not support image",
    "image input is not supported",
    "vision is not supported",
    "unsupported image",
    "invalid content type",
    "multimodal",
    "not a multimodal",
    "不支持图片",
    "不支持多模态",
    "无法识别图片",
)

#: 图片字节头 → MIME。只判最常见的两种，判不出就按 png 试（错了模型会报错，不会静默错）
_MAGIC_PNG = b"\x89PNG\r\n\x1a\n"
_MAGIC_JPEG = b"\xff\xd8\xff"
_MAGIC_GIF = b"GIF8"


def _image_to_data_url(image: bytes | str) -> str:
    """把图片转成 data URL；已经是 URL 或 data URL 的原样返回。"""
    if isinstance(image, str):
        return image
    if image.startswith(_MAGIC_PNG):
        mime = "image/png"
    elif image.startswith(_MAGIC_JPEG):
        mime = "image/jpeg"
    elif image.startswith(_MAGIC_GIF):
        mime = "image/gif"
    else:
        mime = "image/png"
    encoded = base64.b64encode(image).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _looks_like_vision_unsupported(message: str) -> bool:
    lowered = (message or "").lower()
    return any(hint in lowered for hint in _VISION_UNSUPPORTED_HINTS)


def vision(
    prompt_text: str,
    images: list[bytes] | list[str],
    *,
    purpose: str,
    system: str | None = None,
    json_mode: bool = False,
    schema_hint: str | None = None,
    validator: Callable[[dict], str | None] | None = None,
) -> LLMResult:
    """多模态入口：文本 + 一张或多张图片。

    与 chat() 的差别只在消息构造：user 的 content 变成
    [{"type": "text"}, {"type": "image_url"}, ...]，其余（异常、记账、JSON 解析）同理。

    不做重试：图片请求更贵、更慢，识别失败时让用户手输比反复重试更划算。
    """
    _require_configured()

    items = [item for item in (images or []) if item]
    if not items:
        raise LLMError("vision() 至少需要一张图片")

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for image in items:
        content.append({"type": "image_url", "image_url": {"url": _image_to_data_url(image)}})

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})
    _guard_messages(messages)

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
    except Exception as exc:
        message = str(exc)
        if _looks_like_vision_unsupported(message):
            _log_call(purpose, success=False)
            raise VisionUnsupported(f"模型不支持图片输入：{message}") from exc
        _log_call(purpose, success=False)
        raise LLMError(f"图片调用失败（{purpose}）：{message}") from exc

    if not getattr(response, "choices", None):
        _log_call(purpose, success=False)
        raise LLMError(f"模型没有返回任何候选结果（{purpose}）")

    choice = response.choices[0]
    text = (choice.message.content or "") if getattr(choice, "message", None) else ""
    usage = _usage_of(response)

    if not text.strip():
        _log_call(purpose, success=False)
        raise _empty_content_error(choice, usage)

    if json_mode or validator is not None:
        data = extract_json(text)
        problem = "返回内容里没有可解析的 JSON 对象" if data is None else None
        if problem is None and validator is not None:
            problem = validator(data)
        if problem:
            _log_call(purpose, success=False)
            raise LLMError(f"图片识别结果未通过校验（{purpose}）：{problem}")

    _log_call(
        purpose,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        retried=0,
        success=True,
    )
    return LLMResult(text=text, retried=False, success=True, raw=response, **usage)


# ---------------------------------------------------------------------------
# 自检：python -m app.llm_client [--live] [--image 路径]
# ---------------------------------------------------------------------------


def _ensure_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，输出 emoji 会抛 UnicodeEncodeError。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _self_check(live: bool, image_path: str | None = None) -> int:
    _ensure_utf8_stdout()
    print("=" * 56)
    print(f"模型服务配置检查（llm_client {__version__}）")
    print("=" * 56)
    print(f"  解释器         : {sys.executable}")

    if _dotenv_status == "missing":
        print("  python-dotenv  : ❌ 未安装")
        print("\n⚠️ 当前解释器里没有 python-dotenv —— 多半是没在虚拟环境里运行。")
        print("   先激活再跑：  .venv\\Scripts\\activate")
        print("   或直接指定：  .venv\\Scripts\\python.exe -m app.llm_client")
        return 1

    print("  python-dotenv  : ✅")

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
        print("     加 --image 图片路径 会额外测一次多模态识别。")
        return 0

    import time

    print("\n[1/2] 正在真实调用模型……")
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

    if image_path:
        print("\n[2/2] 正在测试图片输入……")
        try:
            with open(image_path, "rb") as handle:
                payload = handle.read()
        except OSError as exc:
            print(f"❌ 读不到图片：{exc}")
            return 2
        try:
            vision(
                "用一句中文描述这张图里有什么，不要分析数学内容。",
                [payload],
                purpose="self-check-vision",
            )
            print("✅ 图片输入可用（多模态支持）")
        except VisionUnsupported as exc:
            print(f"⚠️ 图片输入不可用：{exc}")
            print("   → intake.analyze_image 会自动降级成手输，不算故障。")
        except LLMError as exc:
            print(f"❌ 图片调用失败：{exc}")

    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK
    except Exception:
        pass
    argv = sys.argv[1:]
    image_arg = None
    if "--image" in argv:
        index = argv.index("--image")
        if index + 1 < len(argv):
            image_arg = argv[index + 1]
    raise SystemExit(_self_check("--live" in argv, image_arg))
