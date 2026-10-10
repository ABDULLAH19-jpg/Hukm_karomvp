import json
import logging
import sys

# LogRecord attributes that are not ours. Anything else passed via `extra=` is printed.
_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}

logger = logging.getLogger("hukm")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
                 "event": record.getMessage()}
        entry.update({k: v for k, v in vars(record).items() if k not in _STANDARD})
        if record.exc_info:
            entry["traceback"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """One JSON line per event on stdout. Safe to call more than once."""
    if any(getattr(h, "_hukm", False) for h in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._hukm = True
    logger.addHandler(handler)
    logger.setLevel(level)
    # HTTP clients log full request URLs, and the geocoding key travels in the URL.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
