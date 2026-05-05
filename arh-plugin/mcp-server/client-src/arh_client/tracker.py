import asyncio
import datetime
import functools
import inspect
import json
import os
import time

from arh_client.config import get_config

_current_project_id: str | None = None


def _set_current_project(project_id: str | None):
    global _current_project_id
    _current_project_id = project_id


def _safe_serialize(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def research_tracker(tag: str = "", project_id: str | None = None):
    """Decorator that automatically logs function calls to a research project.

    Args:
        tag: Tag for categorizing the log entry
        project_id: Explicit project ID. If not provided, uses the current
                     project from ResearchManager context.

    Usage:
        @research_tracker(tag="data_processing")
        def process_data(input_file: str) -> dict:
            ...
    """

    def decorator(func):
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            pid = project_id or _current_project_id
            if not pid or get_config().disable_tracking:
                return func(*args, **kwargs)

            sig = inspect.signature(func)
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            input_data = {k: _safe_serialize(v) for k, v in bound.arguments.items()}

            start = time.time()
            try:
                result = func(*args, **kwargs)
                execution_time = time.time() - start
                output_data = _safe_serialize(result)
                _send_log(
                    pid, func.__name__, input_data, output_data, tag, execution_time
                )
                return result
            except Exception as e:
                execution_time = time.time() - start
                _send_log(
                    pid,
                    func.__name__,
                    input_data,
                    {"error": str(e)},
                    tag,
                    execution_time,
                    level="error",
                )
                raise

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            pid = project_id or _current_project_id
            if not pid or get_config().disable_tracking:
                return await func(*args, **kwargs)

            sig = inspect.signature(func)
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            input_data = {k: _safe_serialize(v) for k, v in bound.arguments.items()}

            start = time.time()
            try:
                result = await func(*args, **kwargs)
                execution_time = time.time() - start
                output_data = _safe_serialize(result)
                _send_log(
                    pid, func.__name__, input_data, output_data, tag, execution_time
                )
                return result
            except Exception as e:
                execution_time = time.time() - start
                _send_log(
                    pid,
                    func.__name__,
                    input_data,
                    {"error": str(e)},
                    tag,
                    execution_time,
                    level="error",
                )
                raise

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


def _send_log(
    project_id: str,
    function_name: str,
    input_data: dict,
    output_data,
    tag: str,
    execution_time: float,
    level: str = "info",
):
    try:
        from arh_client.api import APIClient

        client = APIClient()
        client.add_log(
            project_id,
            {
                "function_name": function_name,
                "input_data": input_data,
                "output_data": output_data
                if isinstance(output_data, dict)
                else {"result": output_data},
                "tag": tag,
                "execution_time": execution_time,
                "level": level,
            },
        )
    except Exception:
        _save_local_log(
            project_id, function_name, input_data, output_data, tag, execution_time, level
        )


def _save_local_log(
    project_id: str,
    function_name: str,
    input_data: dict,
    output_data,
    tag: str,
    execution_time: float,
    level: str,
):
    config = get_config()
    if not config.log_locally:
        return
    os.makedirs(config.local_log_dir, exist_ok=True)
    log_file = os.path.join(config.local_log_dir, f"{project_id}.jsonl")
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "function_name": function_name,
        "input_data": input_data,
        "output_data": output_data,
        "tag": tag,
        "execution_time": execution_time,
        "level": level,
    }
    with open(log_file, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
