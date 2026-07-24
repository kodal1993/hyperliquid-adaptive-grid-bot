from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


class HeartbeatWatchdog:
    """Detects a silently hung tick loop and force-restarts the process.

    The tick loop can block forever inside a blocking SDK call with no
    exception raised (e.g. a half-open TCP socket with no timeout), so a
    try/except around the loop body never fires and the process looks
    "active" under systemd while doing nothing. This watchdog runs on its
    own thread and force-exits the process when too much time passes
    without a heartbeat, relying on systemd/supervisor to restart it.
    """

    def __init__(
        self,
        alert_after_seconds: float = 300,
        restart_after_seconds: float = 600,
        check_interval_seconds: float = 15,
        alert_callback: Callable[[str], None] | None = None,
        enabled: bool = True,
    ) -> None:
        self.alert_after_seconds = alert_after_seconds
        self.restart_after_seconds = restart_after_seconds
        self.check_interval_seconds = check_interval_seconds
        self.alert_callback = alert_callback
        self.enabled = enabled
        self._lock = threading.Lock()
        self._last_beat = time.time()
        self._alert_sent = False

    def beat(self) -> None:
        with self._lock:
            self._last_beat = time.time()
            self._alert_sent = False

    def start(self) -> None:
        if not self.enabled:
            return
        thread = threading.Thread(target=self._run, name="heartbeat-watchdog", daemon=True)
        thread.start()

    def _notify(self, message: str) -> None:
        if self.alert_callback is None:
            return
        try:
            self.alert_callback(message)
        except Exception:
            logger.exception("watchdog_alert_callback_failed")

    def _run(self) -> None:
        while True:
            time.sleep(self.check_interval_seconds)
            with self._lock:
                since_last_beat = time.time() - self._last_beat
                alert_sent = self._alert_sent

            if since_last_beat >= self.restart_after_seconds:
                logger.critical(
                    "watchdog_restart since_last_beat_seconds=%.1f restart_after_seconds=%s",
                    since_last_beat,
                    self.restart_after_seconds,
                )
                self._notify(
                    f"\U0001f480 Watchdog: no heartbeat for {since_last_beat:.0f}s, "
                    "force-restarting the bot process"
                )
                for handler in logging.getLogger().handlers:
                    try:
                        handler.flush()
                    except Exception:
                        pass
                os._exit(1)

            if since_last_beat >= self.alert_after_seconds and not alert_sent:
                logger.error(
                    "watchdog_alert since_last_beat_seconds=%.1f alert_after_seconds=%s",
                    since_last_beat,
                    self.alert_after_seconds,
                )
                self._notify(
                    f"⚠️ Watchdog: no heartbeat for {since_last_beat:.0f}s"
                )
                with self._lock:
                    self._alert_sent = True
