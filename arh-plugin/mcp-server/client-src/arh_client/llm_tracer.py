"""Auto-instrumentation for Anthropic and OpenAI LLM SDKs.

Monkey-patches SDK methods to automatically log LLM calls as research spans.
Both SDKs are optional -- if not installed, instrumentation is silently skipped.
"""

from __future__ import annotations

import functools
import time
import threading
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from arh_client.log_buffer import LogBuffer

_original_methods: dict[str, Any] = {}
_lock = threading.Lock()

MAX_CONTENT_LENGTH = 500


def _truncate(text: str, max_length: int = MAX_CONTENT_LENGTH) -> str:
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= max_length:
        return text
    return text[:max_length] + "...[truncated]"


def _truncate_messages(messages: list[dict], max_length: int = MAX_CONTENT_LENGTH) -> list[dict]:
    truncated = []
    for msg in messages:
        entry = {"role": msg.get("role", "")}
        content = msg.get("content", "")
        if isinstance(content, str):
            entry["content"] = _truncate(content, max_length)
        elif isinstance(content, list):
            entry["content"] = f"[{len(content)} content blocks]"
        else:
            entry["content"] = _truncate(str(content), max_length)
        truncated.append(entry)
    return truncated


def _send_span(
    log_buffer: LogBuffer | None,
    function_name: str,
    input_data: dict,
    output_data: dict,
    tag: str,
    execution_time: float,
    meta_data: dict,
    level: str = "info",
) -> None:
    entry = {
        "span_type": "llm_call",
        "function_name": function_name,
        "input_data": input_data,
        "output_data": output_data,
        "tag": tag,
        "execution_time": execution_time,
        "meta_data": meta_data,
        "level": level,
    }

    if log_buffer is not None:
        log_buffer.add(entry)
        return

    from arh_client.tracker import _current_project_id, _send_log

    pid = _current_project_id
    if pid:
        _send_log(
            pid,
            function_name,
            input_data,
            output_data,
            tag,
            execution_time,
            level,
        )


# ---------------------------------------------------------------------------
# Anthropic instrumentation
# ---------------------------------------------------------------------------


def instrument_anthropic(
    client: Any = None,
    log_buffer: LogBuffer | None = None,
) -> None:
    """Wrap Anthropic SDK's messages.create to auto-log LLM calls.

    Args:
        client: An ``anthropic.Anthropic`` or ``anthropic.AsyncAnthropic`` instance.
                If *None*, the class-level method is patched so all future
                instances are instrumented.
        log_buffer: Optional LogBuffer for batched sending.
    """
    try:
        import anthropic  # noqa: F811
    except ImportError:
        return

    # --- sync ---
    if client is not None:
        _patch_anthropic_sync_instance(client, log_buffer)
    else:
        _patch_anthropic_sync_class(anthropic, log_buffer)

    # --- async ---
    if client is not None and hasattr(client, "messages") and hasattr(client.messages, "acreate"):
        _patch_anthropic_async_instance(client, log_buffer)


def _patch_anthropic_sync_class(anthropic_module: Any, log_buffer: LogBuffer | None) -> None:
    messages_cls = anthropic_module.resources.Messages
    original = messages_cls.create

    with _lock:
        if getattr(original, "__wrapped__", False):
            return
        _original_methods["anthropic.Messages.create"] = original

    @functools.wraps(original)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        return _run_anthropic_sync(original, self, log_buffer, *args, **kwargs)

    wrapper.__wrapped__ = True  # type: ignore[attr-defined]
    messages_cls.create = wrapper

    # async variant on AsyncMessages
    if hasattr(anthropic_module.resources, "AsyncMessages"):
        async_messages_cls = anthropic_module.resources.AsyncMessages
        async_original = async_messages_cls.create
        if not getattr(async_original, "__wrapped__", False):
            _original_methods["anthropic.AsyncMessages.create"] = async_original

            @functools.wraps(async_original)
            async def async_wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
                return await _run_anthropic_async(async_original, self, log_buffer, *args, **kwargs)

            async_wrapper.__wrapped__ = True  # type: ignore[attr-defined]
            async_messages_cls.create = async_wrapper


def _patch_anthropic_sync_instance(client: Any, log_buffer: LogBuffer | None) -> None:
    messages = client.messages
    original = messages.create

    with _lock:
        if getattr(original, "__wrapped__", False):
            return
        key = f"anthropic.instance.{id(client)}.messages.create"
        _original_methods[key] = original

    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return _run_anthropic_sync(original, None, log_buffer, *args, **kwargs)

    wrapper.__wrapped__ = True  # type: ignore[attr-defined]
    client.messages.create = wrapper


def _patch_anthropic_async_instance(client: Any, log_buffer: LogBuffer | None) -> None:
    messages = client.messages
    original = messages.acreate

    with _lock:
        if getattr(original, "__wrapped__", False):
            return
        key = f"anthropic.instance.{id(client)}.messages.acreate"
        _original_methods[key] = original

    @functools.wraps(original)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await _run_anthropic_async(original, None, log_buffer, *args, **kwargs)

    wrapper.__wrapped__ = True  # type: ignore[attr-defined]
    client.messages.acreate = wrapper


def _run_anthropic_sync(
    original: Any,
    self_arg: Any,
    log_buffer: LogBuffer | None,
    *args: Any,
    **kwargs: Any,
) -> Any:
    start = time.time()
    try:
        if self_arg is not None:
            result = original(self_arg, *args, **kwargs)
        else:
            result = original(*args, **kwargs)
        execution_time = time.time() - start
        _log_anthropic_result(result, kwargs, execution_time, log_buffer)
        return result
    except Exception as e:
        execution_time = time.time() - start
        _log_anthropic_error(e, kwargs, execution_time, log_buffer)
        raise


async def _run_anthropic_async(
    original: Any,
    self_arg: Any,
    log_buffer: LogBuffer | None,
    *args: Any,
    **kwargs: Any,
) -> Any:
    start = time.time()
    try:
        if self_arg is not None:
            result = await original(self_arg, *args, **kwargs)
        else:
            result = await original(*args, **kwargs)
        execution_time = time.time() - start
        _log_anthropic_result(result, kwargs, execution_time, log_buffer)
        return result
    except Exception as e:
        execution_time = time.time() - start
        _log_anthropic_error(e, kwargs, execution_time, log_buffer)
        raise


def _log_anthropic_result(
    result: Any,
    kwargs: dict,
    execution_time: float,
    log_buffer: LogBuffer | None,
) -> None:
    model = kwargs.get("model", "unknown")
    messages = kwargs.get("messages", [])
    max_tokens = kwargs.get("max_tokens", None)
    temperature = kwargs.get("temperature", None)

    input_data = {
        "model": model,
        "messages": _truncate_messages(messages) if isinstance(messages, list) else str(messages),
        "max_tokens": max_tokens,
    }

    # Extract response content
    content_text = ""
    if hasattr(result, "content") and result.content:
        first_block = result.content[0]
        if hasattr(first_block, "text"):
            content_text = first_block.text

    usage_dict = {}
    if hasattr(result, "usage"):
        usage_dict = {
            "input_tokens": getattr(result.usage, "input_tokens", 0),
            "output_tokens": getattr(result.usage, "output_tokens", 0),
        }

    stop_reason = getattr(result, "stop_reason", None)

    output_data = {
        "content": _truncate(content_text),
        "usage": usage_dict,
        "stop_reason": stop_reason,
    }

    meta_data = {
        "provider": "anthropic",
        "model": model,
        "temperature": temperature,
        "tokens": usage_dict,
    }

    _send_span(
        log_buffer,
        function_name="anthropic.messages.create",
        input_data=input_data,
        output_data=output_data,
        tag=model,
        execution_time=execution_time,
        meta_data=meta_data,
        level="info",
    )


def _log_anthropic_error(
    error: Exception,
    kwargs: dict,
    execution_time: float,
    log_buffer: LogBuffer | None,
) -> None:
    model = kwargs.get("model", "unknown")
    messages = kwargs.get("messages", [])

    input_data = {
        "model": model,
        "messages": _truncate_messages(messages) if isinstance(messages, list) else str(messages),
        "max_tokens": kwargs.get("max_tokens", None),
    }

    _send_span(
        log_buffer,
        function_name="anthropic.messages.create",
        input_data=input_data,
        output_data={"error": str(error)},
        tag=model,
        execution_time=execution_time,
        meta_data={"provider": "anthropic", "model": model},
        level="error",
    )


# ---------------------------------------------------------------------------
# OpenAI instrumentation
# ---------------------------------------------------------------------------


def instrument_openai(
    client: Any = None,
    log_buffer: LogBuffer | None = None,
) -> None:
    """Wrap OpenAI SDK's chat.completions.create to auto-log LLM calls.

    Args:
        client: An ``openai.OpenAI`` or ``openai.AsyncOpenAI`` instance.
                If *None*, the class-level method is patched so all future
                instances are instrumented.
        log_buffer: Optional LogBuffer for batched sending.
    """
    try:
        import openai  # noqa: F811
    except ImportError:
        return

    if client is not None:
        _patch_openai_sync_instance(client, log_buffer)
    else:
        _patch_openai_sync_class(openai, log_buffer)


def _patch_openai_sync_class(openai_module: Any, log_buffer: LogBuffer | None) -> None:
    completions_cls = openai_module.resources.chat.completions.Completions
    original = completions_cls.create

    with _lock:
        if getattr(original, "__wrapped__", False):
            return
        _original_methods["openai.Completions.create"] = original

    @functools.wraps(original)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        return _run_openai_sync(original, self, log_buffer, *args, **kwargs)

    wrapper.__wrapped__ = True  # type: ignore[attr-defined]
    completions_cls.create = wrapper

    # async variant
    if hasattr(openai_module.resources.chat.completions, "AsyncCompletions"):
        async_cls = openai_module.resources.chat.completions.AsyncCompletions
        async_original = async_cls.create
        if not getattr(async_original, "__wrapped__", False):
            _original_methods["openai.AsyncCompletions.create"] = async_original

            @functools.wraps(async_original)
            async def async_wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
                return await _run_openai_async(async_original, self, log_buffer, *args, **kwargs)

            async_wrapper.__wrapped__ = True  # type: ignore[attr-defined]
            async_cls.create = async_wrapper


def _patch_openai_sync_instance(client: Any, log_buffer: LogBuffer | None) -> None:
    completions = client.chat.completions
    original = completions.create

    with _lock:
        if getattr(original, "__wrapped__", False):
            return
        key = f"openai.instance.{id(client)}.chat.completions.create"
        _original_methods[key] = original

    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return _run_openai_sync(original, None, log_buffer, *args, **kwargs)

    wrapper.__wrapped__ = True  # type: ignore[attr-defined]
    client.chat.completions.create = wrapper


def _run_openai_sync(
    original: Any,
    self_arg: Any,
    log_buffer: LogBuffer | None,
    *args: Any,
    **kwargs: Any,
) -> Any:
    start = time.time()
    try:
        if self_arg is not None:
            result = original(self_arg, *args, **kwargs)
        else:
            result = original(*args, **kwargs)
        execution_time = time.time() - start
        _log_openai_result(result, kwargs, execution_time, log_buffer)
        return result
    except Exception as e:
        execution_time = time.time() - start
        _log_openai_error(e, kwargs, execution_time, log_buffer)
        raise


async def _run_openai_async(
    original: Any,
    self_arg: Any,
    log_buffer: LogBuffer | None,
    *args: Any,
    **kwargs: Any,
) -> Any:
    start = time.time()
    try:
        if self_arg is not None:
            result = await original(self_arg, *args, **kwargs)
        else:
            result = await original(*args, **kwargs)
        execution_time = time.time() - start
        _log_openai_result(result, kwargs, execution_time, log_buffer)
        return result
    except Exception as e:
        execution_time = time.time() - start
        _log_openai_error(e, kwargs, execution_time, log_buffer)
        raise


def _log_openai_result(
    result: Any,
    kwargs: dict,
    execution_time: float,
    log_buffer: LogBuffer | None,
) -> None:
    model = kwargs.get("model", "unknown")
    messages = kwargs.get("messages", [])
    temperature = kwargs.get("temperature", None)
    max_tokens = kwargs.get("max_tokens", None)

    input_data = {
        "model": model,
        "messages": _truncate_messages(messages) if isinstance(messages, list) else str(messages),
        "max_tokens": max_tokens,
    }

    # Extract response content
    content_text = ""
    if hasattr(result, "choices") and result.choices:
        first_choice = result.choices[0]
        if hasattr(first_choice, "message") and hasattr(first_choice.message, "content"):
            content_text = first_choice.message.content or ""

    usage_dict = {}
    if hasattr(result, "usage") and result.usage:
        usage_dict = {
            "input_tokens": getattr(result.usage, "prompt_tokens", 0),
            "output_tokens": getattr(result.usage, "completion_tokens", 0),
        }

    finish_reason = None
    if hasattr(result, "choices") and result.choices:
        finish_reason = getattr(result.choices[0], "finish_reason", None)

    output_data = {
        "content": _truncate(content_text),
        "usage": usage_dict,
        "stop_reason": finish_reason,
    }

    meta_data = {
        "provider": "openai",
        "model": model,
        "temperature": temperature,
        "tokens": usage_dict,
    }

    _send_span(
        log_buffer,
        function_name="openai.chat.completions.create",
        input_data=input_data,
        output_data=output_data,
        tag=model,
        execution_time=execution_time,
        meta_data=meta_data,
        level="info",
    )


def _log_openai_error(
    error: Exception,
    kwargs: dict,
    execution_time: float,
    log_buffer: LogBuffer | None,
) -> None:
    model = kwargs.get("model", "unknown")
    messages = kwargs.get("messages", [])

    input_data = {
        "model": model,
        "messages": _truncate_messages(messages) if isinstance(messages, list) else str(messages),
        "max_tokens": kwargs.get("max_tokens", None),
    }

    _send_span(
        log_buffer,
        function_name="openai.chat.completions.create",
        input_data=input_data,
        output_data={"error": str(error)},
        tag=model,
        execution_time=execution_time,
        meta_data={"provider": "openai", "model": model},
        level="error",
    )


# ---------------------------------------------------------------------------
# Uninstrument
# ---------------------------------------------------------------------------


def uninstrument() -> None:
    """Restore all original SDK methods that were monkey-patched."""
    with _lock:
        for key, original in _original_methods.items():
            try:
                if key == "anthropic.Messages.create":
                    import anthropic
                    anthropic.resources.Messages.create = original
                elif key == "anthropic.AsyncMessages.create":
                    import anthropic
                    anthropic.resources.AsyncMessages.create = original
                elif key == "openai.Completions.create":
                    import openai
                    openai.resources.chat.completions.Completions.create = original
                elif key == "openai.AsyncCompletions.create":
                    import openai
                    openai.resources.chat.completions.AsyncCompletions.create = original
                # Instance-level patches cannot be generically restored
                # since the instance reference is not stored.
            except (ImportError, AttributeError):
                pass
        _original_methods.clear()
