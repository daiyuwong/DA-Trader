"""
Centralized logging — daily rotating files + console + dashboard feed.
"""

import logging
import sys
import queue
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
import config


# Shared queue that the dashboard reads from for live logs
log_queue: queue.Queue = queue.Queue(maxsize=500)


class QueueHandler(logging.Handler):
    """Push formatted log records into a bounded queue for the dashboard."""

    def emit(self, record):
        try:
            msg = self.format(record)
            # Drop oldest if full
            if log_queue.full():
                try:
                    log_queue.get_nowait()
                except queue.Empty:
                    pass
            log_queue.put_nowait(msg)
        except Exception:
            self.handleError(record)


def setup_logger(name: str = "bot") -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger  # already configured

    logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # Daily rotating file
    log_file = config.LOGS_DIR / f"{name}.log"
    file_handler = TimedRotatingFileHandler(
        str(log_file),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # Queue handler for dashboard
    q_handler = QueueHandler()
    q_handler.setFormatter(fmt)
    logger.addHandler(q_handler)

    return logger


def get_recent_logs(count: int = 50) -> list[str]:
    """Return the most recent log lines from the queue."""
    items = []
    while not log_queue.empty() and len(items) < count:
        try:
            items.append(log_queue.get_nowait())
        except queue.Empty:
            break
    # Put them back so other consumers can see them
    for item in items:
        try:
            log_queue.put_nowait(item)
        except queue.Full:
            break
    return items


def drain_logs() -> list[str]:
    """Drain all logs from the queue (for SSE streaming)."""
    items = []
    while not log_queue.empty():
        try:
            items.append(log_queue.get_nowait())
        except queue.Empty:
            break
    return items
