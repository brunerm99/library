from __future__ import annotations

import os
import sys
from collections import deque
from typing import Deque, List

_memory_log: Deque[str] = deque(maxlen=int(os.getenv("LIBINDEX_LOG_BUFFER", "1000")))


def get_recent_logs(n: int = 200) -> List[str]:
    n = max(1, min(n, len(_memory_log)))
    return list(_memory_log)[-n:]


def configure_logger():
    try:
        from loguru import logger as _logger  # type: ignore

        fmt = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {message}"
        _logger.remove()
        _logger.add(
            sys.stdout,
            format=fmt,
            colorize=False,
            level=os.getenv("LIBINDEX_LOG_LEVEL", "INFO"),
            backtrace=False,
            diagnose=False,
        )

        # Optional file sink
        log_file = os.getenv("LIBINDEX_LOG_FILE", "/data/library.log")
        try:
            _logger.add(
                log_file,
                format=fmt,
                rotation=os.getenv("LIBINDEX_LOG_ROTATION", "10 MB"),
                retention=os.getenv("LIBINDEX_LOG_RETENTION", "7 days"),
                level=os.getenv("LIBINDEX_LOG_LEVEL", "INFO"),
            )
        except Exception:
            # Ignore file sink errors if path isn't writable
            pass

        # Memory sink for UI/API retrieval
        def _memory_sink(message):
            try:
                _memory_log.append(message.rstrip("\n"))
            except Exception:
                pass

        _logger.add(_memory_sink, level=os.getenv("LIBINDEX_MEMORY_LEVEL", "INFO"))

        return _logger
    except Exception:  # pragma: no cover
        import logging

        logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
        _fallback = logging.getLogger("libindex")

        class _Shim:
            def info(self, msg, *args, **kwargs):
                _fallback.info(msg if not args else msg.format(*args))

            def warning(self, msg, *args, **kwargs):
                _fallback.warning(msg if not args else msg.format(*args))

            def error(self, msg, *args, **kwargs):
                _fallback.error(msg if not args else msg.format(*args))

            def exception(self, msg, *args, **kwargs):
                _fallback.exception(msg if not args else msg.format(*args))

        return _Shim()


# Initialize logger on import
logger = configure_logger()
