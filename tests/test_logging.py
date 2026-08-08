"""Plain-line logging: format, redaction, no-duplicate-handlers on re-setup."""

from __future__ import annotations

import logging
from pathlib import Path

from arc.logging_setup import (
    LOGGER_NAME,
    ArcLineFormatter,
    RedactionFilter,
    SessionFilter,
    attach_session_id,
    log_event,
    setup_logging,
)


def _record(
    level: int = logging.INFO, msg: str = "Event", detail: str = "", created: float = 0.0
) -> logging.LogRecord:
    record = logging.LogRecord(LOGGER_NAME, level, __file__, 1, msg, (), None)
    record.arc_detail = detail
    record.created = created
    return record


class TestArcLineFormatterLayout:
    def test_info_line_has_no_marker(self) -> None:
        line = ArcLineFormatter().format(_record(logging.INFO, "Feed Connected"))
        assert "⚠" not in line and "⛔" not in line
        assert "Feed Connected" in line

    def test_warning_gets_the_warn_glyph(self) -> None:
        line = ArcLineFormatter().format(_record(logging.WARNING, "Rejected"))
        assert "⚠" in line

    def test_error_and_critical_get_the_stop_glyph(self) -> None:
        for level in (logging.ERROR, logging.CRITICAL):
            line = ArcLineFormatter().format(_record(level, "PTB Unavailable"))
            assert "⛔" in line

    def test_detail_is_aligned_to_the_event_column(self) -> None:
        short = ArcLineFormatter().format(_record(logging.INFO, "PTB Frozen", "120,000.00"))
        long = ArcLineFormatter().format(_record(logging.INFO, "Trigger Locked", "120,010.00"))
        short_col = short.index("120,000.00")
        long_col = long.index("120,010.00")
        assert short_col == long_col

    def test_no_detail_means_no_trailing_whitespace(self) -> None:
        line = ArcLineFormatter().format(_record(logging.INFO, "Feed Connected", ""))
        assert line == line.rstrip()

    def test_timestamp_is_hh_mm_ss(self) -> None:
        line = ArcLineFormatter().format(_record(logging.INFO, "x", created=0.0))
        stamp = line.split(" ", 1)[0]
        assert len(stamp) == 8 and stamp.count(":") == 2

    def test_exception_info_is_appended(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = _record(logging.ERROR, "Crashed")
            record.exc_info = sys.exc_info()
        line = ArcLineFormatter().format(record)
        assert "ValueError: boom" in line


class TestRedactionFilter:
    def test_short_values_are_never_redacted(self) -> None:
        """Below the floor a "secret" would match ordinary log text."""
        redaction = RedactionFilter(("ab", "abcde"))
        assert redaction.secret_count == 0

    def test_values_at_the_floor_are_redacted(self) -> None:
        redaction = RedactionFilter(("abcdef",))
        assert redaction.secret_count == 1
        assert redaction.redact("key=abcdef end") == "key=[REDACTED] end"

    def test_msg_is_redacted_on_the_record(self) -> None:
        redaction = RedactionFilter(("supersecret123",))
        record = _record(msg="token supersecret123 leaked")
        redaction.filter(record)
        assert "supersecret123" not in record.msg
        assert "[REDACTED]" in record.msg

    def test_arc_detail_is_redacted(self) -> None:
        redaction = RedactionFilter(("supersecret123",))
        record = _record(detail="key=supersecret123")
        redaction.filter(record)
        assert "supersecret123" not in record.arc_detail  # type: ignore[attr-defined]

    def test_tuple_args_are_redacted(self) -> None:
        redaction = RedactionFilter(("supersecret123",))
        record = _record(msg="value=%s")
        record.args = ("supersecret123",)
        redaction.filter(record)
        assert record.args == ("[REDACTED]",)

    def test_dict_args_are_redacted(self) -> None:
        redaction = RedactionFilter(("supersecret123",))
        record = _record(msg="value=%(v)s")
        record.args = {"v": "supersecret123"}
        redaction.filter(record)
        assert record.args == {"v": "[REDACTED]"}

    def test_a_secret_containing_another_secret_is_replaced_whole(self) -> None:
        """Sorted longest-first: partial masking would leave a recognisable fragment."""
        redaction = RedactionFilter(("abcdef", "abcdefghij"))
        assert redaction.redact("abcdefghij") == "[REDACTED]"

    def test_no_secrets_means_filter_is_a_no_op(self) -> None:
        redaction = RedactionFilter(())
        record = _record(msg="plain text")
        assert redaction.filter(record) is True
        assert record.msg == "plain text"

    def test_filter_always_returns_true(self) -> None:
        """A filter returning False would drop the record instead of redacting it."""
        redaction = RedactionFilter(("supersecret123",))
        assert redaction.filter(_record(msg="supersecret123")) is True


class TestSetupLoggingIsSafeToCallTwice:
    def test_returns_the_named_logger(self, tmp_path: Path) -> None:
        logger = setup_logging(tmp_path)
        assert logger.name == LOGGER_NAME

    def test_propagate_is_disabled(self, tmp_path: Path) -> None:
        """Root may be configured by uvicorn with a different formatter."""
        logger = setup_logging(tmp_path)
        assert logger.propagate is False

    def test_second_call_does_not_duplicate_handlers(self, tmp_path: Path) -> None:
        setup_logging(tmp_path, console=True)
        first_count = len(logging.getLogger(LOGGER_NAME).handlers)
        setup_logging(tmp_path, console=True)
        second_count = len(logging.getLogger(LOGGER_NAME).handlers)
        assert second_count == first_count

    def test_console_false_omits_the_stream_handler(self, tmp_path: Path) -> None:
        """FileHandler is itself a StreamHandler subclass, so exclude it explicitly."""
        logger = setup_logging(tmp_path, console=False)
        non_file_stream_handlers = [
            h
            for h in logger.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        ]
        assert non_file_stream_handlers == []

    def test_log_dir_is_created(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "logs"
        setup_logging(target)
        assert target.is_dir()

    def test_get_logger_never_creates_handlers(self, tmp_path: Path) -> None:
        logger = logging.getLogger("arc-test-fresh-name")
        assert logger.handlers == []


class TestLogEventEmitsOnePlainLine:
    def test_log_event_writes_to_the_configured_logger(self, tmp_path: Path) -> None:
        logger = setup_logging(tmp_path, console=False)
        log_event(logging.INFO, "Feed Connected", logger=logger)
        log_path = tmp_path / "arc.log"
        assert "Feed Connected" in log_path.read_text(encoding="utf-8")

    def test_detail_appears_in_the_written_line(self, tmp_path: Path) -> None:
        logger = setup_logging(tmp_path, console=False)
        log_event(logging.INFO, "PTB Frozen", "120,000.00", logger=logger)
        text = (tmp_path / "arc.log").read_text(encoding="utf-8")
        assert "120,000.00" in text

    def test_log_event_defaults_to_get_logger(self, tmp_path: Path) -> None:
        setup_logging(tmp_path, console=False)
        log_event(logging.INFO, "Default Logger Path")
        text = (tmp_path / "arc.log").read_text(encoding="utf-8")
        assert "Default Logger Path" in text


class TestSessionStampingNeverEatsTheDetail:
    """Regression: a LoggerAdapter here silently blanked every reason string.

    The first implementation of session stamping wrapped the runtime logger in a
    logging.LoggerAdapter. An adapter's process() REPLACES the caller's `extra`
    dict with its own, so `arc_detail` — the reason text on every denial, every
    rejection and every PTB failure — was dropped from every line in the codebase.
    The lines still looked complete; they just carried no reason. These tests fail
    if anything reintroduces that shape.
    """

    def test_detail_survives_session_stamping(self, tmp_path: Path) -> None:
        logger = setup_logging(tmp_path, console=False)
        attach_session_id(logger, "0123456789abcdef")
        log_event(logging.ERROR, "PTB Unavailable", "no trading this market", logger=logger)
        text = (tmp_path / "arc.log").read_text(encoding="utf-8")
        assert "no trading this market" in text

    def test_the_record_still_carries_arc_detail(self, tmp_path: Path) -> None:
        """Asserted on the record, not the rendered line: Signal Tank reads the field."""
        logger = setup_logging(tmp_path, console=False)
        attach_session_id(logger, "0123456789abcdef")
        seen: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                seen.append(record)

        handler = _Capture()
        logger.addHandler(handler)
        try:
            log_event(logging.WARNING, "Rejected", "ENTRY_PRICE_LIMIT (0.87 > 0.85)", logger=logger)
        finally:
            logger.removeHandler(handler)

        assert len(seen) == 1
        assert getattr(seen[0], "arc_detail", "") == "ENTRY_PRICE_LIMIT (0.87 > 0.85)"
        assert getattr(seen[0], "arc_session_id", "") == "0123456789abcdef"

    def test_the_session_id_is_not_printed_into_the_line(self, tmp_path: Path) -> None:
        """The plain-line format is frozen. The id travels on the record only."""
        logger = setup_logging(tmp_path, console=False)
        attach_session_id(logger, "0123456789abcdef")
        log_event(logging.INFO, "PTB Frozen", "120,000.00", logger=logger)
        text = (tmp_path / "arc.log").read_text(encoding="utf-8")
        assert "0123456789abcdef" not in text
        assert "120,000.00" in text

    def test_the_column_alignment_is_unchanged_by_stamping(self, tmp_path: Path) -> None:
        logger = setup_logging(tmp_path, console=False)
        attach_session_id(logger, "0123456789abcdef")
        log_event(logging.INFO, "PTB Frozen", "120,000.00", logger=logger)
        log_event(logging.INFO, "Trigger Locked", "120,010.00", logger=logger)
        lines = (tmp_path / "arc.log").read_text(encoding="utf-8").splitlines()
        assert lines[0].index("120,000.00") == lines[1].index("120,010.00")

    def test_reattaching_the_same_session_does_not_stack_filters(self, tmp_path: Path) -> None:
        logger = setup_logging(tmp_path, console=False)
        first = attach_session_id(logger, "aaaa")
        again = attach_session_id(logger, "aaaa")
        assert first is again
        assert sum(isinstance(f, SessionFilter) for f in logger.filters) == 1

    def test_a_new_session_replaces_the_old_id(self, tmp_path: Path) -> None:
        """A restart must not keep attributing lines to the runtime that ended."""
        logger = setup_logging(tmp_path, console=False)
        attach_session_id(logger, "old-session")
        attach_session_id(logger, "new-session")
        assert sum(isinstance(f, SessionFilter) for f in logger.filters) == 1

        seen: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                seen.append(record)

        handler = _Capture()
        logger.addHandler(handler)
        try:
            log_event(logging.INFO, "Runtime Ready", logger=logger)
        finally:
            logger.removeHandler(handler)

        assert getattr(seen[0], "arc_session_id", "") == "new-session"

    def test_redaction_still_reaches_the_detail_after_stamping(self, tmp_path: Path) -> None:
        """Both filters must coexist: stamping must not displace redaction."""
        logger = setup_logging(tmp_path, console=False, secrets=("supersecretvalue",))
        attach_session_id(logger, "0123456789abcdef")
        log_event(logging.INFO, "Wallet Connected", "key supersecretvalue", logger=logger)
        text = (tmp_path / "arc.log").read_text(encoding="utf-8")
        assert "supersecretvalue" not in text
        assert "[REDACTED]" in text
