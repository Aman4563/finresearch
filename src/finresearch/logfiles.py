"""Size-capped, rotated log files for the long-running processes (#247 retention).

`finresearch serve --log-file data/logs/serve.log` writes the API's, the monitor's and uvicorn's logs through a
RotatingFileHandler; `finresearch logpipe data/logs/web.log` does the same for any process's output on stdin (the
web app: `pnpm start 2>&1 | uv run finresearch logpipe ../data/logs/web.log`). Each file is capped at
retention.LOG_MAX_BYTES with retention.LOG_BACKUPS older files (serve.log.1 ... .5), so logs never grow without
bound. A shell redirection (`> serve.log`) is not rotated: use these instead.
"""

from __future__ import annotations

import copy
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO, Any

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def rotating_handler(
    path: Path, max_bytes: int | None = None, backups: int | None = None
) -> RotatingFileHandler:
    from finresearch.monitor.retention import LOG_BACKUPS, LOG_MAX_BYTES

    path.parent.mkdir(parents=True, exist_ok=True)
    return RotatingFileHandler(path, maxBytes=max_bytes or LOG_MAX_BYTES, backupCount=backups or LOG_BACKUPS,
                               encoding="utf-8")  # fmt: skip


def uvicorn_log_config(
    path: Path, max_bytes: int | None = None, backups: int | None = None
) -> dict[str, Any]:
    """uvicorn's default logging config with every logger (uvicorn, access, and the app's root) sent to one rotated
    file instead of the terminal."""
    from uvicorn.config import LOGGING_CONFIG

    from finresearch.monitor.retention import LOG_BACKUPS, LOG_MAX_BYTES

    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg["formatters"]["plain"] = {"format": FORMAT}
    cfg["handlers"] = {"file": {"class": "logging.handlers.RotatingFileHandler", "filename": str(path),
                                "maxBytes": max_bytes or LOG_MAX_BYTES, "backupCount": backups or LOG_BACKUPS,
                                "encoding": "utf-8", "formatter": "plain"}}  # fmt: skip
    for name in cfg["loggers"]:
        cfg["loggers"][name]["handlers"] = ["file"]
    cfg["root"] = {"handlers": ["file"], "level": "INFO"}
    return cfg


def pipe(stream: IO[str], path: Path, max_bytes: int | None = None, backups: int | None = None) -> int:
    """Copy `stream` line by line into a rotated file (each line as written, no extra prefix). Returns lines."""
    log = logging.getLogger("finresearch.logpipe")
    log.propagate = False
    handler = rotating_handler(path, max_bytes, backups)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    n = 0
    try:
        for line in stream:
            log.info(line.rstrip("\n"))
            n += 1
    finally:
        log.removeHandler(handler)
        handler.close()
    return n


def stdin_pipe(path: Path) -> int:
    return pipe(sys.stdin, path)
