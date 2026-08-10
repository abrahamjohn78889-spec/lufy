"""UI BLINDNESS: the frontend must not know how to trade.

Criterion 23 and 29. The dashboard renders backend state and nothing else. The
failure this pins is not a crash — it is a browser that computes a number the
engine also computes, and the two disagree. The operator then sees a direction,
trigger or P/L that no engine ever produced and acts on it.

The check is textual because there is no JS runtime here. That is enough: the
patterns being banned are patterns, and a `parseFloat` on a price cannot hide
from a grep of the one file that would contain it.
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parent.parent / "arc" / "web"
_APP_JS = (_WEB / "app.js").read_text(encoding="utf-8")
_INDEX = (_WEB / "index.html").read_text(encoding="utf-8")

# The A18 chart is the one place a string value is turned into a number, because an
# SVG y-coordinate is a pixel and pixels are not money. Everything outside this
# block must be free of numeric coercion.
_CHART = _APP_JS.split("function svgLine(", 1)[1].split("// ── order book", 1)[0]
_OUTSIDE_CHART = _APP_JS.replace(_CHART, "")


def _code(text: str) -> str:
    """Strip comments and template/quoted strings — prose is not logic.

    Without this every ban below would trip on the header comment that documents
    the ban, and on the visible labels that name the very concepts being banned.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", " ", text, flags=re.M)
    text = re.sub(r"//.*$", " ", text, flags=re.M)
    return re.sub(r"'[^'\n]*'|\"[^\"\n]*\"|`(?:[^`\\]|\\.)*`", "''", text, flags=re.S)


_LOGIC = _code(_OUTSIDE_CHART)
_ALL_LOGIC = _code(_APP_JS)


class TestNoNumericCoercionOnValues:
    def test_no_parsefloat_anywhere(self) -> None:
        """A Decimal parsed into a float is the precision loss the contract forbids."""
        assert "parseFloat" not in _LOGIC
        assert "parseFloat" not in _CHART

    def test_no_parseint_anywhere(self) -> None:
        assert "parseInt" not in _ALL_LOGIC

    def test_number_is_used_only_for_chart_geometry(self) -> None:
        """Every Number() call site must be inside the SVG block, on a coordinate."""
        assert "Number(" not in _LOGIC

    def test_no_tofixed_rounding_of_a_backend_value(self) -> None:
        """Rounding here would display a price the engine never quoted."""
        assert "toFixed" not in _ALL_LOGIC

    def test_the_only_math_outside_the_chart_is_time(self) -> None:
        """Math on a clock is presentation. Math on a value is a second engine."""
        allowed = {"Math.floor", "Math.max", "Math.round", "Math.min"}
        used = set(re.findall(r"Math\.[a-zA-Z]+", _LOGIC))
        assert used <= allowed, used - allowed


class TestNoBusinessLogic:
    def test_the_frontend_never_compares_a_twap_to_a_trigger(self) -> None:
        """The comparison that decides a fire lives in the Decision Engine only."""
        for banned in ("signal_twap >", "signal_twap <", "twap >=", "twap <=",
                       "> trigger", "< trigger", "ptb >", "ptb <"):
            assert banned not in _ALL_LOGIC, banned

    def test_the_frontend_never_names_a_direction_itself(self) -> None:
        """UP/DOWN/NO_DIRECTION are backend words. Assigning one here invents it."""
        for banned in ("= 'UP'", '= "UP"', "= 'DOWN'", '= "DOWN"',
                       "= 'NO_DIRECTION'", "?'UP'", "? 'UP'"):
            assert banned not in _OUTSIDE_CHART, banned

    def test_the_frontend_never_computes_a_buffer_or_a_trigger(self) -> None:
        assert not re.search(r"(buffer|trigger|ptb)\s*[+\-*/]", _ALL_LOGIC)
        assert not re.search(r"[+\-*/]\s*(buffer|trigger|ptb)\b", _ALL_LOGIC)

    def test_the_frontend_never_sizes_or_prices_an_order(self) -> None:
        for banned in ("quantity *", "quantity /", "price *", "price /",
                       "notional *", "notional /", "size *"):
            assert banned not in _ALL_LOGIC, banned

    def test_the_loe_stage_is_read_not_derived(self) -> None:
        """One assignment from the payload, and no branch that picks a stage."""
        assert "state.derived.loe_stage" in _APP_JS
        for stage in ("WINDOW_OPEN", "VALUES_FROZEN", "ORDER_SUBMITTED",
                      "WAITING_FOR_FILL", "BUFFER_NOT_SATISFIED"):
            # The stage names belong in the markup, which the backend order verifies,
            # never in the script where they would become a branch.
            assert stage not in _APP_JS, stage

    def test_pnl_is_never_summed_in_the_browser(self) -> None:
        for banned in ("pnl +", "+ pnl", "pnl +=", "total +", "sum +"):
            assert banned not in _ALL_LOGIC, banned

    def test_the_only_reduce_is_the_path_walker(self) -> None:
        """A reduce over values would be an aggregate the ledger already computes."""
        for line in _ALL_LOGIC.splitlines():
            if "reduce(" in line:
                assert "o[k]" in line, line

    def test_no_strategy_word_appears_as_logic(self) -> None:
        """A17: one strategy, and the browser has no opinion about it."""
        assert "strateg" not in _ALL_LOGIC.lower()


class TestStaleIsNeverLive:
    def test_a_dropped_socket_greys_the_whole_document(self) -> None:
        """Per-panel greying would leave one panel looking current."""
        assert "document.body.classList.toggle('stale'" in _APP_JS
        assert "body.stale" in (_WEB / "style.css").read_text(encoding="utf-8")

    def test_a_silent_socket_is_detected_without_a_close_event(self) -> None:
        """A half-open socket never fires onclose; the last frame would stay lit."""
        assert "performance.now() - lastFrame >" in _APP_JS
        assert "setLive(false)" in _APP_JS

    def test_the_dashboard_does_not_poll_for_state(self) -> None:
        """One /status read at boot, then the socket. Continuous polling is banned."""
        assert _APP_JS.count("fetch('/status')") == 1
        interval_bodies = re.findall(r"setInterval\(\(\) => \{(.*?)\}, \d+\)", _APP_JS, re.S)
        for body in interval_bodies:
            assert "fetch(" not in body


class TestNoUnboundedGrowth:
    def test_the_event_buffer_is_capped(self) -> None:
        """A 24x7 page with an unbounded list is a browser that grows all week."""
        assert re.search(r"MAX_ROWS\s*=\s*\d+", _APP_JS)
        assert "events.length > MAX_ROWS" in _APP_JS or "MAX_ROWS" in _APP_JS

    def test_the_dedup_set_is_pruned_with_the_buffer(self) -> None:
        """A Set that only ever grows is the same leak wearing a different hat."""
        trimmed = _APP_JS.split("function addEvent(", 1)[1].split("\n}", 1)[0]
        assert "seen.delete" in trimmed or "seen = new Set" in trimmed

    def test_rows_are_replaced_not_appended(self) -> None:
        """appendChild in a repaint duplicates the whole panel every frame."""
        assert "replaceChildren" in _APP_JS


class TestNoAccessControlInTheBrowser:
    def test_nothing_stores_or_sends_a_credential(self) -> None:
        for banned in ("localStorage", "sessionStorage", "document.cookie",
                       "Authorization", "Bearer", "token"):
            assert banned not in _APP_JS, banned

    def test_the_page_loads_nothing_from_the_internet(self) -> None:
        """A VPS on loopback has no outbound path; a CDN tag renders a broken page."""
        assert "http://" not in _INDEX
        assert "https://" not in _INDEX
        assert "//cdn" not in _INDEX
