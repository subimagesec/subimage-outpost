"""Tee stdin to stdout and to a size-rotated log file.

start.sh pipes all container output through this so the /_internal/logs endpoint
can read it back, while keeping it bounded on disk (and therefore bounded in the
memory the endpoint uses to read it). Rotation is by size via the stdlib
RotatingFileHandler: at most maxBytes * (backupCount + 1) is kept on disk.
"""

import logging
import logging.handlers
import os
import sys

DEFAULT_MAX_BYTES = 5000000
DEFAULT_BACKUP_COUNT = 3


def _positive_int_env(name: str, default: int) -> int:
    """Read a strictly positive int from the env, falling back to `default`.

    A non-positive maxBytes/backupCount would silently disable rotation and break
    the bounded-log guarantee, so reject those (and unparseable values).
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def main() -> None:
    log_path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get("OUTPOST_LOG_FILE", "/tmp/outpost.log")
    )
    max_bytes = _positive_int_env("OUTPOST_LOG_MAX_BYTES", DEFAULT_MAX_BYTES)
    backup_count = _positive_int_env("OUTPOST_LOG_BACKUP_COUNT", DEFAULT_BACKUP_COUNT)

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    file_logger = logging.getLogger("outpost-logtee")
    file_logger.setLevel(logging.INFO)
    file_logger.addHandler(handler)

    # readline (not `for line in sys.stdin`) avoids the iterator read-ahead buffer
    # so lines reach the console promptly.
    for line in iter(sys.stdin.readline, ""):
        line = line.rstrip("\n")
        print(line, flush=True)
        file_logger.info(line)


if __name__ == "__main__":
    main()
