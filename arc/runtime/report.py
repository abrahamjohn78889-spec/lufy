"""The Production Validation Report, rendered as plain text.

WHY TEXT AND NOT A DOCUMENT FORMAT. This is read over SSH on the machine that ran
the validation, at the moment the operator is deciding whether to enable live
trading. A PDF or an HTML file is a file to transfer first; a text block is
readable where it is produced and pastes into a commit message or a chat.

WHY IT REFUSES TO CONCLUDE. The report prints its verdict from
`ValidationReport.ready_for_live`, which is False while any criterion is
UNVERIFIED. It cannot be talked into a green summary by a run that simply did not
exercise the hard parts, because the unverified list is printed in full, with what
each one needs, immediately under the verdict rather than in an appendix.
"""

from __future__ import annotations

from typing import Any

from arc.runtime.validation import (
    FAIL,
    PASS,
    UNVERIFIED,
    ValidationReport,
)

__all__ = ["render_report"]

_RULE = "─" * 78

# The metric rows, in the order the addendum lists them. A tuple rather than a walk
# over the dict so the report's layout is fixed: an operator comparing two runs
# side by side must find the same figure on the same line.
_METRIC_ROWS: tuple[tuple[str, str], ...] = (
    ("runtime uptime (s)", "runtime_uptime_seconds"),
    ("runtime restarts", "runtime_restarts"),
    ("runtime reconnects", "runtime_reconnects"),
    ("avg websocket latency ms", "avg_websocket_latency_ms"),
    ("avg CLOB latency ms", "avg_clob_latency_ms"),
    ("avg RTDS latency ms", "avg_rtds_latency_ms"),
    ("avg Chainlink latency ms", "avg_chainlink_latency_ms"),
    ("avg order latency ms", "avg_order_latency_ms"),
    ("recorder markets", "recorder_markets"),
    ("recorder observations", "recorder_observations"),
    ("database bytes", "database_bytes"),
    ("database bytes / market", "database_bytes_per_market"),
    ("database bytes / day", "database_bytes_per_day_projected"),
    ("validation duration (s)", "validation_duration_seconds"),
)


def _section(title: str) -> str:
    return f"\n{title}\n{_RULE}\n"


def _row(label: str, value: Any, width: int = 30) -> str:
    return f"  {label:<{width}} {value}\n"


def render_report(report: ValidationReport, *, mode: str, provider: str) -> str:
    """One text block. Verdict first, then the evidence."""
    lines = [
        _RULE,
        "\nARC — PRODUCTION VALIDATION REPORT\n",
        _RULE,
        "\n",
        _row("runtime mode", mode),
        _row("provider", provider),
        _row("criteria passed", sum(1 for c in report.criteria if c.result == PASS)),
        _row("criteria failed", len(report.failed)),
        _row("criteria unverified", len(report.unverified)),
        _row("verdict", report.verdict),
    ]

    if report.failed:
        lines.append(_section("FAILED"))
        for c in report.failed:
            lines.append(f"  [{c.number}] {c.name}\n        {c.detail}\n")

    if report.unverified:
        lines.append(_section("UNVERIFIED — requires the operator, not the test suite"))
        for c in report.unverified:
            lines.append(f"  [{c.number}] {c.name}\n        {c.detail}\n")
            if c.evidence:
                lines.append(f"        how: {c.evidence}\n")

    lines.append(_section("ALL CRITERIA"))
    for c in report.criteria:
        mark = {PASS: "PASS", FAIL: "FAIL", UNVERIFIED: "----"}[c.result]
        lines.append(f"  {mark}  [{c.number}] {c.name}: {c.detail}\n")

    recorder = report.recorder
    if recorder is not None:
        lines.append(_section("RECORDER"))
        lines.append(_row("markets audited", len(recorder.markets)))
        lines.append(_row("complete", "yes" if recorder.complete else "no"))
        lines.append(_row("incomplete markets", len(recorder.incomplete)))
        lines.append(_row("market gaps", len(recorder.gaps) or "none"))
        for market in recorder.incomplete[:10]:
            lines.append(f"      {market.slug}: {', '.join(market.missing)}\n")
        for gap in recorder.gaps[:10]:
            lines.append(f"      gap {gap}\n")

    stats = report.stats
    if stats is not None:
        lines.append(_section("FILL STATISTICS BY WINDOW"))
        header = (
            f"  {'window':>7} {'fired':>6} {'subs':>6} {'ack':>6} {'fill':>6} "
            f"{'part':>6} {'canc':>6} {'rej':>5} {'indet':>6} "
            f"{'rate':>7} {'fill ms':>9} {'p95 ms':>9}\n"
        )
        lines.append(header)
        for key in sorted(stats.by_offset):
            row = stats.by_offset[key].as_json()
            rate = "—" if row["fill_rate"] is None else f"{row['fill_rate']:.2%}"
            mean = "—" if row["mean_fill_latency_ms"] is None else (
                f"{row['mean_fill_latency_ms']:.0f}"
            )
            p95 = "—" if row["p95_fill_latency_ms"] is None else (
                f"{row['p95_fill_latency_ms']:.0f}"
            )
            lines.append(
                f"  {row['window']:>7} {row['fired']:>6} {row['submissions']:>6} "
                f"{row['acknowledged']:>6} {row['filled']:>6} {row['partial']:>6} "
                f"{row['cancelled']:>6} {row['rejected']:>5} {row['indeterminate']:>6} "
                f"{rate:>7} {mean:>9} {p95:>9}\n"
            )
        totals = stats.as_json()
        lines.append(
            f"\n  totals: {totals['submissions']} submissions, "
            f"{totals['filled']} filled across {totals['markets']} markets\n"
        )

    metrics = report.metrics
    if metrics is not None:
        lines.append(_section("RUNTIME METRICS"))
        values = metrics.as_json()
        for label, field in _METRIC_ROWS:
            lines.append(_row(label, values[field]))

    lines.append(_section("NOT MEASURED BY ARC"))
    lines.append(
        "  CPU, memory, disk and network are the host's to report. ARC does not\n"
        "  sample them, and a number invented here would be read as measured.\n"
        "  Read them with `top`, `free -m`, `df -h` and `ss -s` on the VPS.\n"
    )
    lines.append(_RULE + "\n")
    return "".join(lines)
