from __future__ import annotations

import logging
import threading

from arh_client.api import APIClient

logger = logging.getLogger(__name__)


class LogBuffer:
    """Thread-safe buffer that batches research logs for efficient sending.

    Collects log entries and flushes them in batches, either when the buffer
    reaches ``max_batch_size`` or every ``flush_interval`` seconds.
    """

    def __init__(
        self,
        project_id: str,
        client: APIClient,
        flush_interval: float = 5.0,
        max_batch_size: int = 50,
    ):
        self._project_id = project_id
        self._client = client
        self._buffer: list[dict] = []
        self._lock = threading.Lock()
        self._flush_interval = flush_interval
        self._max_batch_size = max_batch_size
        self._timer: threading.Timer | None = None
        self._running = False
        self.flush_count = 0
        self.total_sent = 0

    def add(self, log_data: dict) -> None:
        """Add a log entry to the buffer (thread-safe).

        Automatically flushes when the buffer reaches *max_batch_size*.
        """
        should_flush = False
        with self._lock:
            self._buffer.append(log_data)
            if len(self._buffer) >= self._max_batch_size:
                should_flush = True

        if should_flush:
            self.flush()

    def flush(self) -> None:
        """Send all buffered logs via the batch API."""
        with self._lock:
            if not self._buffer:
                return
            batch = self._buffer[:]
            self._buffer.clear()

        try:
            self._client.add_logs_batch(self._project_id, batch)
            self.flush_count += 1
            self.total_sent += len(batch)
        except Exception:
            logger.warning(
                "Failed to send %d log(s) for project %s; saving locally",
                len(batch),
                self._project_id,
                exc_info=True,
            )
            self._save_local_fallback(batch)

    def start(self) -> None:
        """Start the background periodic flush timer."""
        if self._running:
            return
        self._running = True
        self._schedule_flush()

    def stop(self) -> None:
        """Flush remaining logs and stop the background timer."""
        self._running = False
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self.flush()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _schedule_flush(self) -> None:
        """Schedule the next periodic flush if still running."""
        if not self._running:
            return
        self._timer = threading.Timer(self._flush_interval, self._periodic_flush)
        self._timer.daemon = True
        self._timer.start()

    def _periodic_flush(self) -> None:
        """Callback executed by the timer thread."""
        self.flush()
        self._schedule_flush()

    def _save_local_fallback(self, logs: list[dict]) -> None:
        """Persist logs locally when the API is unreachable."""
        try:
            from arh_client.tracker import _save_local_log

            for entry in logs:
                _save_local_log(
                    project_id=self._project_id,
                    function_name=entry.get("function_name", "unknown"),
                    input_data=entry.get("input_data", {}),
                    output_data=entry.get("output_data", {}),
                    tag=entry.get("tag", ""),
                    execution_time=entry.get("execution_time", 0.0),
                    level=entry.get("level", "info"),
                )
        except Exception:
            logger.error(
                "Local log fallback also failed for project %s",
                self._project_id,
                exc_info=True,
            )
