from __future__ import annotations

import json
import os
import tempfile
from threading import Lock
from typing import Iterator


class WriteAheadLog:
    """
    Append-only JSONL log of mutating operations. Each line is a JSON object with
    'op' field. Recovered on startup by replaying entries through a callable.

    Invariants:
    - append() is atomic per-line; partial writes are detected and skipped on replay.
    - rotate() snapshots the current log to a `.archived` file with epoch-suffixed name
      and starts a fresh empty log; safe to call concurrently with appends (locked).
    - replay() yields parsed dicts in append order; lines that fail to parse are skipped
      (corruption tolerance).
    """

    OP_INGEST = "ingest"
    OP_UPSERT_ITEM = "upsert_item"

    def __init__(self, path: str, fsync_every: int = 1) -> None:
        self._path = path
        self._fsync_every = max(1, int(fsync_every))
        self._counter = 0
        self._lock = Lock()
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, exist_ok=True)
        # open file in append+binary so each writeline is independent
        self._fp = open(path, "ab", buffering=0)

    def append(self, op: str, payload: dict) -> None:
        line = json.dumps({"op": op, **payload}, separators=(",", ":"), ensure_ascii=False)
        encoded = (line + "\n").encode("utf-8")
        with self._lock:
            self._fp.write(encoded)
            self._counter += 1
            if self._counter % self._fsync_every == 0:
                self._fp.flush()
                os.fsync(self._fp.fileno())

    def replay(self) -> Iterator[dict]:
        if not os.path.exists(self._path):
            return iter(())

        def gen() -> Iterator[dict]:
            with open(self._path, "rb") as f:
                for raw in f:
                    if not raw.endswith(b"\n"):
                        # torn write — skip the partial trailing line
                        break
                    try:
                        yield json.loads(raw.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue

        return gen()

    def rotate(self, archive_suffix: str) -> str | None:
        """Move current log to <path>.<archive_suffix> and start a fresh empty log."""
        with self._lock:
            self._fp.flush()
            os.fsync(self._fp.fileno())
            self._fp.close()
            archive = f"{self._path}.{archive_suffix}"
            if os.path.exists(self._path):
                os.replace(self._path, archive)
            else:
                archive = None
            self._fp = open(self._path, "ab", buffering=0)
            self._counter = 0
            return archive

    def close(self) -> None:
        with self._lock:
            self._fp.flush()
            os.fsync(self._fp.fileno())
            self._fp.close()

    @staticmethod
    def write_atomic_snapshot(path: str, payload: dict) -> None:
        """Write a snapshot atomically via tempfile + rename."""
        directory = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp = tempfile.mkstemp(prefix=".relgraph-wal-snap-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
