from __future__ import annotations

import fnmatch
import logging
import os
import threading
from pathlib import Path

from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from arh_client.api import APIClient
from arh_client.log_buffer import LogBuffer

logger = logging.getLogger(__name__)

DEFAULT_EXCLUDE = [
    ".git",
    "node_modules",
    "__pycache__",
    "*.pyc",
    ".env",
]

EXTENSION_TYPE_MAP: dict[str, str] = {
    ".py": "code",
    ".js": "code",
    ".ts": "code",
    ".java": "code",
    ".go": "code",
    ".rs": "code",
    ".csv": "data",
    ".json": "data",
    ".parquet": "data",
    ".xlsx": "data",
    ".png": "figure",
    ".jpg": "figure",
    ".svg": "figure",
    ".pdf": "figure",
    ".pt": "model",
    ".onnx": "model",
    ".pkl": "model",
    ".h5": "model",
}


def _detect_artifact_type(file_path: str) -> str:
    """Return an artifact type string based on the file extension."""
    ext = Path(file_path).suffix.lower()
    return EXTENSION_TYPE_MAP.get(ext, "data")


def _matches_any(name: str, patterns: list[str]) -> bool:
    """Check whether *name* matches any of the glob *patterns*."""
    for pat in patterns:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def _path_contains_excluded(path: str, exclude: list[str]) -> bool:
    """Return True if any path component matches an exclude pattern."""
    parts = Path(path).parts
    for part in parts:
        if _matches_any(part, exclude):
            return True
    return False


class _EventHandler(FileSystemEventHandler):
    """Watchdog handler that debounces file events and delegates to FileObserver."""

    def __init__(self, owner: FileObserver):
        super().__init__()
        self._owner = owner

    def on_created(self, event):
        if not event.is_directory and isinstance(event, FileCreatedEvent):
            self._owner._schedule_upload(event.src_path)

    def on_modified(self, event):
        if not event.is_directory and isinstance(event, FileModifiedEvent):
            self._owner._schedule_upload(event.src_path)


class FileObserver:
    """Watch a directory and log file changes to the research timeline.

    Uses ``watchdog`` to monitor the filesystem in a background thread.
    File changes are debounced per path so that rapid successive writes
    result in a single log entry.

    Args:
        project_id: Research project to register artifacts to.
        client: An :class:`APIClient` instance.
        log_buffer: A :class:`LogBuffer` for recording file-change logs.
        watch_dir: Directory to watch (default ``"."``).
        repo_root: Root of the git repository. If ``None``, auto-detected
            from ``watch_dir``.
        include: Glob patterns to include (e.g. ``["*.py", "*.csv"]``).
            If ``None``, all files are included.
        exclude: Glob patterns to exclude.  Defaults to common noise
            directories/files (``.git``, ``node_modules``, etc.).
        debounce_sec: Seconds to wait before registering after the last
            change to a given file path.
    """

    def __init__(
        self,
        project_id: str,
        client: APIClient,
        log_buffer: LogBuffer,
        watch_dir: str = ".",
        repo_root: str | None = None,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        debounce_sec: float = 2.0,
    ):
        self._project_id = project_id
        self._client = client
        self._log_buffer = log_buffer
        self._watch_dir = os.path.abspath(watch_dir)
        self._include = include
        self._exclude = exclude if exclude is not None else list(DEFAULT_EXCLUDE)
        self._debounce_sec = debounce_sec

        if repo_root:
            self._repo_root = os.path.abspath(repo_root)
        else:
            self._repo_root = self._detect_repo_root()

        self._observer: Observer | None = None
        self._timers_lock = threading.Lock()
        self._timers: dict[str, threading.Timer] = {}

    def _detect_repo_root(self) -> str:
        """Walk up from watch_dir to find the git repo root."""
        import subprocess

        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, cwd=self._watch_dir,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except FileNotFoundError:
            pass
        return self._watch_dir

    def _get_current_branch(self) -> str:
        """Detect the current git branch."""
        import subprocess

        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, cwd=self._repo_root,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except FileNotFoundError:
            pass
        return ""

    def start(self) -> None:
        """Start the watchdog observer in a background thread."""
        if self._observer is not None:
            return

        handler = _EventHandler(self)
        self._observer = Observer()
        self._observer.schedule(handler, self._watch_dir, recursive=True)
        self._observer.daemon = True
        self._observer.start()
        logger.info("FileObserver started watching %s", self._watch_dir)

    def stop(self) -> None:
        """Stop the observer and flush any pending uploads."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
            self._observer = None

        with self._timers_lock:
            for timer in self._timers.values():
                timer.cancel()
            pending_paths = list(self._timers.keys())
            self._timers.clear()

        for path in pending_paths:
            self._do_upload(path)

        logger.info("FileObserver stopped")

    def _should_process(self, path: str) -> bool:
        """Determine whether a file event should be processed."""
        rel_path = os.path.relpath(path, self._watch_dir)
        name = os.path.basename(path)

        if _path_contains_excluded(rel_path, self._exclude):
            return False
        if _matches_any(name, self._exclude):
            return False

        if self._include is not None:
            return _matches_any(name, self._include)

        return True

    def _schedule_upload(self, path: str) -> None:
        """Debounce a file change: reset the timer for *path*."""
        if not self._should_process(path):
            return

        with self._timers_lock:
            existing = self._timers.pop(path, None)
            if existing is not None:
                existing.cancel()

            timer = threading.Timer(self._debounce_sec, self._do_upload, args=(path,))
            timer.daemon = True
            self._timers[path] = timer
            timer.start()

    def _do_upload(self, path: str) -> None:
        """Log the file change event.

        Note: file changes are logged for timeline visibility only.
        Artifact registration should be done explicitly by the agent at
        milestones via the ``upload_artifact`` MCP tool.
        """
        with self._timers_lock:
            self._timers.pop(path, None)

        if not os.path.isfile(path):
            return

        artifact_type = _detect_artifact_type(path)
        git_rel_path = os.path.relpath(path, self._repo_root)
        display_rel_path = os.path.relpath(path, self._watch_dir)

        self._log_buffer.add(
            {
                "function_name": f"file_changed: {display_rel_path}",
                "span_type": "file_change",
                "message": f"Auto-detected file change: {display_rel_path}",
                "meta_data": {
                    "file_path": git_rel_path,
                    "artifact_type": artifact_type,
                },
            }
        )
