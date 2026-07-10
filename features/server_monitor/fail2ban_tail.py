from __future__ import annotations

import logging
import os
import time
import threading
from pathlib import Path
from typing import Iterator


class RealtimeLogFollower:
    """
    Realtime follower for a single text log file.

    - starts at EOF on first open
    - detects inode changes
    - detects truncation / copytruncate
    - retries forever until stop_event is set
    """

    def __init__(
        self,
        path: str,
        stop_event: threading.Event,
        logger: logging.Logger,
        *,
        name: str = "log",
        poll_interval: float = 0.5,
        retry_min_delay: float = 1.0,
        retry_max_delay: float = 10.0,
        seek_to_end_on_start: bool = True,
        encoding: str = "utf-8",
        errors: str = "replace",
    ) -> None:
        self.path = str(path or "").strip()
        self.stop_event = stop_event
        self.logger = logger
        self.name = name
        self.poll_interval = max(0.1, float(poll_interval))
        self.retry_min_delay = max(0.2, float(retry_min_delay))
        self.retry_max_delay = max(self.retry_min_delay, float(retry_max_delay))
        self.seek_to_end_on_start = bool(seek_to_end_on_start)
        self.encoding = encoding
        self.errors = errors
        self._first_open = True
        self._fp = None
        self._inode = None
        self._position = 0

    def close(self) -> None:
        fp = self._fp
        self._fp = None
        self._inode = None
        self._position = 0
        if fp is not None:
            try:
                fp.close()
            except Exception:
                pass

    def _should_reopen(self, path_obj: Path) -> bool:
        if self._fp is None:
            return True
        try:
            stat_now = path_obj.stat()
        except FileNotFoundError:
            return True
        if self._inode is None:
            return True
        if stat_now.st_ino != self._inode:
            return True
        if stat_now.st_size < self._position:
            return True
        return False

    def _open(self, path_obj: Path) -> None:
        self.close()
        fp = path_obj.open("r", encoding=self.encoding, errors=self.errors)
        try:
            stat_now = path_obj.stat()
            self._inode = stat_now.st_ino
        except Exception:
            self._inode = None
        if self._first_open and self.seek_to_end_on_start:
            fp.seek(0, os.SEEK_END)
        else:
            fp.seek(0)
        self._position = fp.tell()
        self._first_open = False
        self._fp = fp

    def follow(self) -> Iterator[str]:
        delay = self.retry_min_delay
        while not self.stop_event.is_set():
            try:
                if not self.path:
                    self.logger.warning("%s path kosong", self.name)
                    time.sleep(delay)
                    delay = min(delay * 1.5, self.retry_max_delay)
                    continue

                path_obj = Path(self.path).expanduser()
                if not path_obj.exists():
                    time.sleep(delay)
                    delay = min(delay * 1.5, self.retry_max_delay)
                    continue

                if self._should_reopen(path_obj):
                    self._open(path_obj)
                    delay = self.retry_min_delay

                assert self._fp is not None
                line = self._fp.readline()
                if line:
                    self._position = self._fp.tell()
                    delay = self.retry_min_delay
                    yield line.rstrip("\n")
                    continue

                if self._should_reopen(path_obj):
                    self._open(path_obj)
                    delay = self.retry_min_delay
                    continue

                time.sleep(self.poll_interval)
            except Exception as exc:
                self.logger.exception("RealtimeLogFollower %s error: %s", self.name, exc)
                self.close()
                time.sleep(delay)
                delay = min(delay * 1.5, self.retry_max_delay)

        self.close()
