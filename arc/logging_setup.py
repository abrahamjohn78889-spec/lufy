"""Plain-line logging.

One format, everywhere, exactly as specified (A4):

    12:09:45  Feed Connected
    12:09:50  PTB Frozen              120,000.00
    12:09:51  Trigger Locked          120,010.00  UP
    12:09:54 ⚠ Rejected   ENTRY_PRICE_LIMIT (0.87 > 0.85)
    12:10:01 ⛔ PTB Unavailable — no trading this market

No JSON, no structured fields, no correlation IDs. The operator reads these lines
directly; a machine never parses them.

Failures are logged as loudly as successes. A fill shows up in six places on the
dashboard, but a rejection that is not logged leaves no trace anywhere at all —
which is why every denial carries a reason code into this stream.
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

__all__ = [
    "LOGGER_NAME",
    "ArcLineFormatter",
    "RedactionFilter",
    "SessionFilter",
    "attach_session_id",
    "ensure_utf8_streams",
    "get_logger",
    "log_event",
    "setup_logging",
]

LOGGER_NAME: Final[str] = "arc"

# Column at which the value field starts, so successive lines align in a terminal
# and the operator can scan a column of prices rather than reading each line.
_EVENT_COLUMN_WIDTH: Final[int] = 22

# Severity markers. WARNING and above get a glyph so a failure is impossible to
# miss in a scrolling log; INFO lines stay unmarked so the normal case is quiet.
_LEVEL_MARKERS: Final[dict[int, str]] = {
    logging.DEBUG: " ",
    logging.INFO: " ",
    logging.WARNING: "⚠",
    logging.ERROR: "⛔",
    logging.CRITICAL: "⛔",
}

_REDACTED: Final[str] = "[REDACTED]"

# Below this length a "secret" is not distinctive enough to search for safely: an
# empty or 2-character value would match substrings all over ordinary log text and
# redact things that are not secrets. Short values are handled by never putting
# them in a log line in the first place, not by search-and-replace.
_MIN_REDACTABLE_LENGTH: Final[int] = 6


def ensure_utf8_streams() -> None:
    """Force stdout/stderr to UTF-8.

    The log markers are ⚠ and ⛔ and the doctor report draws box rules. A Windows
    console defaults to cp1252, which cannot encode any of them, and the whole
    process would die with a UnicodeEncodeError while writing a warning line —
    losing the report precisely when something needed reporting. errors="replace"
    so an unencodable character degrades to a substitution instead of an
    exception even where reconfigure is unavailable.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # A redirected or already-detached stream raises here. Not fatal: the
            # file handler is explicitly UTF-8 regardless, so the log is intact.
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


class RedactionFilter(logging.Filter):
    """Removes known secret values from every record before it is formatted.

    This is the last line of defence, not the first. Secrets are SecretStr and are
    never interpolated into messages to begin with; this filter exists because a
    single f-string in a future exception handler would otherwise write an API
    secret into a log file that gets pasted into a support chat.
    """

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        # Sorted longest-first so that a secret which contains another secret as a
        # substring is replaced whole, instead of being partially masked and
        # leaving a recognisable fragment behind.
        self._secrets: tuple[str, ...] = tuple(
            sorted(
                {s for s in secrets if len(s) >= _MIN_REDACTABLE_LENGTH},
                key=len,
                reverse=True,
            )
        )

    @property
    def secret_count(self) -> int:
        return len(self._secrets)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, _REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        if isinstance(record.msg, str):
            record.msg = self.redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self.redact(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    self.redact(a) if isinstance(a, str) else a for a in record.args
                )
        detail = getattr(record, "arc_detail", None)
        if isinstance(detail, str):
            record.arc_detail = self.redact(detail)
        return True


class SessionFilter(logging.Filter):
    """Stamps every record with the runtime session id that produced it.

    A filter rather than a LoggerAdapter. An adapter's `process` REPLACES the
    caller's `extra` dict with its own, which would silently erase `arc_detail`
    from every line in the codebase — the reason text on every denial, every
    rejection and every PTB failure — leaving lines that look complete and carry
    no reason.
    """

    def __init__(self, runtime_session_id: str) -> None:
        super().__init__()
        self._session_id = runtime_session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    def filter(self, record: logging.LogRecord) -> bool:
        # Never overwrite an id already on the record: a nested runtime's own
        # stamp is more specific than ours.
        if not getattr(record, "arc_session_id", ""):
            record.arc_session_id = self._session_id
        return True


def attach_session_id(logger: logging.Logger, runtime_session_id: str) -> SessionFilter:
    """Stamp this logger's records with a session id. Idempotent per session.

    Idempotent because a second runtime construction against the same logger — the
    normal case across a supervisor restart, and across tests sharing a logger —
    would otherwise stack filters and leave the FIRST session's id winning, so
    every line after a restart would be attributed to the runtime that ended.
    """
    for existing in logger.filters:
        if isinstance(existing, SessionFilter):
            if existing.session_id == runtime_session_id:
                return existing
            logger.removeFilter(existing)
    added = SessionFilter(runtime_session_id)
    logger.addFilter(added)
    return added


class ArcLineFormatter(logging.Formatter):
    """Formats a record as one plain line: HH:MM:SS, marker, event, detail."""

    def __init__(self, timezone: str = "") -> None:
        super().__init__()
        # Blank means the host's own zone. A name pins it, which is what lets a VPS
        # in one region write timestamps an operator in another can line up against
        # the Polymarket page without doing arithmetic during an incident.
        self._zone = ZoneInfo(timezone) if timezone else None

    # Local time, not UTC. The operator reads these beside the Polymarket page and
    # a UTC log next to a local-time countdown makes every incident harder to
    # reconstruct than it needs to be.
    # formatTime is the stdlib logging API name, hence the camelCase.
    def formatTime(
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:
        if self._zone is None:
            return time.strftime("%H:%M:%S", time.localtime(record.created))
        return datetime.fromtimestamp(record.created, self._zone).strftime("%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        marker = _LEVEL_MARKERS.get(record.levelno, " ")
        event = record.getMessage()
        detail = getattr(record, "arc_detail", "")
        stamp = self.formatTime(record)

        if detail:
            line = f"{stamp} {marker} {event.ljust(_EVENT_COLUMN_WIDTH)}{detail}"
        else:
            line = f"{stamp} {marker} {event}"
        line = line.rstrip()

        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def setup_logging(
    log_dir: Path | str,
    *,
    secrets: tuple[str, ...] = (),
    level: int = logging.INFO,
    timezone: str = "",
    console: bool = True,
    retention_days: int = 30,
) -> logging.Logger:
    """Configure the single ARC logger. Safe to call more than once.

    Existing handlers are closed and dropped rather than added to. Calling this
    twice without clearing would duplicate every line in the file and in the
    dashboard log panel, which reads as duplicated trading activity.
    """
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    # Root propagation off: the root logger may be configured by uvicorn, and its
    # formatter would re-render ARC lines in a different format.
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = ArcLineFormatter(timezone)
    redaction = RedactionFilter(secrets)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        directory / "arc.log",
        when="midnight",
        backupCount=retention_days,
        encoding="utf-8",
        utc=False,
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redaction)
    logger.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.addFilter(redaction)
        logger.addHandler(stream)

    return logger


def get_logger() -> logging.Logger:
    """Return the ARC logger. Never creates handlers."""
    return logging.getLogger(LOGGER_NAME)


def log_event(
    level: int,
    event: str,
    detail: str = "",
    *,
    logger: logging.Logger | None = None,
    exc_info: Any = None,
) -> None:
    """Emit one plain line.

    `event` is the short left-hand label ("Order Submitted"); `detail` is the
    aligned right-hand column ("UP  0.74  135 sh").
    """
    target = logger if logger is not None else get_logger()
    target.log(level, event, extra={"arc_detail": detail}, exc_info=exc_info)
