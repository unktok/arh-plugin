"""Unified session manager that starts/stops all auto-tracing features."""

from __future__ import annotations

import logging
import os
from typing import Any

from arh_client.api import APIClient
from arh_client.git_tracker import (
    detect_git_info,
    install_post_commit_hook,
    uninstall_post_commit_hook,
)
from arh_client.log_buffer import LogBuffer
from arh_client.observer import FileObserver
from arh_client.llm_tracer import (
    instrument_anthropic as _instrument_anthropic,
    instrument_openai as _instrument_openai,
    uninstrument as _uninstrument,
)
from arh_client.tracker import _set_current_project

logger = logging.getLogger(__name__)


class AgentSession:
    """One-liner to start full auto-tracing.

    Wraps project creation, log buffering, file observation, and LLM SDK
    instrumentation into a single context manager.

    Usage::

        with AgentSession(
            "My experiment",
            watch_dir="./outputs",
            instrument_anthropic=True,
        ) as session:
            # All LLM calls and file changes are auto-logged
            result = do_research()
            session.log("custom_step", input_data={"key": "value"})
    """

    def __init__(
        self,
        title: str,
        description: str = "",
        tags: list[str] | None = None,
        watch_dir: str | None = None,
        watch_include: list[str] | None = None,
        watch_exclude: list[str] | None = None,
        instrument_anthropic: bool = False,
        instrument_openai: bool = False,
        git_auto_detect: bool = True,
        flush_interval: float = 5.0,
        max_batch_size: int = 50,
    ):
        self._title = title
        self._description = description
        self._tags = tags
        self._watch_dir = watch_dir
        self._watch_include = watch_include
        self._watch_exclude = watch_exclude
        self._instrument_anthropic = instrument_anthropic
        self._instrument_openai = instrument_openai
        self._git_auto_detect = git_auto_detect
        self._flush_interval = flush_interval
        self._max_batch_size = max_batch_size

        self._client: APIClient | None = None
        self._buffer: LogBuffer | None = None
        self._observer: FileObserver | None = None
        self._project: dict | None = None
        self._instrumented_anthropic = False
        self._instrumented_openai = False
        self._git_hook_installed = False
        self._git_repo_dir: str | None = None

    @property
    def project_id(self) -> str:
        """Return the current project ID (raises if session not entered)."""
        if self._project is None:
            raise RuntimeError("AgentSession has not been entered yet")
        return self._project["id"]

    @property
    def project(self) -> dict:
        """Return the project dict returned by the API."""
        if self._project is None:
            raise RuntimeError("AgentSession has not been entered yet")
        return self._project

    def log(self, function_name: str, **kwargs: Any) -> None:
        """Manually log a span via the buffer.

        Accepts arbitrary keyword arguments which are added to the log entry.
        Common keys: ``step_type``, ``title``, ``content``, ``metadata``,
        ``input_data``, ``output_data``, ``tag``, ``execution_time``.
        """
        if self._buffer is None:
            raise RuntimeError("AgentSession has not been entered yet")
        entry: dict[str, Any] = {"function_name": function_name}
        entry.update(kwargs)
        self._buffer.add(entry)

    # ------------------------------------------------------------------
    # Sync context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> AgentSession:
        # 1. Create research project
        self._client = APIClient()
        self._project = self._client.create_project(
            {
                "title": self._title,
                "description": self._description,
                "tags": self._tags or [],
            }
        )
        pid = self._project["id"]

        # 2. Auto-detect and link git repository
        if self._git_auto_detect:
            git_info = detect_git_info(os.getcwd())
            if git_info:
                remote_url, branch = git_info
                if remote_url:
                    try:
                        self._client.link_repository(pid, remote_url, branch)
                    except Exception:
                        logger.warning("Failed to link git repository", exc_info=True)
                    try:
                        hook_path = install_post_commit_hook(
                            pid,
                            self._client._base_url,
                            self._client._api_key,
                        )
                        if hook_path:
                            self._git_hook_installed = True
                            self._git_repo_dir = os.getcwd()
                    except Exception:
                        logger.warning("Failed to install post-commit hook", exc_info=True)

        # 3. Create and start LogBuffer
        self._buffer = LogBuffer(
            project_id=pid,
            client=self._client,
            flush_interval=self._flush_interval,
            max_batch_size=self._max_batch_size,
        )
        self._buffer.start()

        # 4. Start FileObserver if watch_dir specified
        if self._watch_dir is not None:
            self._observer = FileObserver(
                project_id=pid,
                client=self._client,
                log_buffer=self._buffer,
                watch_dir=self._watch_dir,
                include=self._watch_include,
                exclude=self._watch_exclude,
            )
            self._observer.start()

        # 5. Instrument Anthropic SDK
        if self._instrument_anthropic:
            _instrument_anthropic(log_buffer=self._buffer)
            self._instrumented_anthropic = True

        # 6. Instrument OpenAI SDK
        if self._instrument_openai:
            _instrument_openai(log_buffer=self._buffer)
            self._instrumented_openai = True

        # 7. Set global project ID for @research_tracker
        _set_current_project(pid)

        logger.info("AgentSession started: project %s (%s)", pid, self._title)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        # 1. Uninstall git post-commit hook
        if self._git_hook_installed:
            try:
                uninstall_post_commit_hook(self._git_repo_dir)
            except Exception:
                logger.warning("Error uninstalling post-commit hook", exc_info=True)
            self._git_hook_installed = False

        # 2. Stop FileObserver
        if self._observer is not None:
            try:
                self._observer.stop()
            except Exception:
                logger.warning("Error stopping FileObserver", exc_info=True)
            self._observer = None

        # 3. Uninstrument LLM SDKs
        if self._instrumented_anthropic or self._instrumented_openai:
            try:
                _uninstrument()
            except Exception:
                logger.warning("Error uninstrumenting LLM SDKs", exc_info=True)
            self._instrumented_anthropic = False
            self._instrumented_openai = False

        # 4. Stop and flush LogBuffer
        if self._buffer is not None:
            try:
                self._buffer.stop()
            except Exception:
                logger.warning("Error stopping LogBuffer", exc_info=True)
            self._buffer = None

        # 5. Clear global project ID (project stays "active" — agent sets "completed" explicitly)
        _set_current_project(None)

        pid = self._project["id"] if self._project else "unknown"
        logger.info("AgentSession ended: project %s", pid)

        # Do not suppress exceptions
        return False
