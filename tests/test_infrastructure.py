"""Structural gates over the source itself.

Every test here parses the source rather than calling it. That is deliberate: each
one guards a property that a passing unit test cannot detect, because the defect
is the *existence* of a code path rather than the behaviour of one.

`reset()` on TwapAccumulator is the clearest case. A test that calls reset() and
asserts the sum is zero passes — the method works. The defect is that the method
exists at all, because "TWAP resets per market" is meant to be satisfied by
throwing the object away (A11), and a reset path is one that some future call site
can forget while every existing test keeps passing.

The same reasoning covers module-level mutable state, a reintroduced `events`
table, a TESTNET mode, and a direct time.time() call: in each case the failure is
invisible until the day it matters, and by then it is in production.

The detectors are pure functions over parsed source, and TestGatesActuallyBite at
the bottom runs each one against a synthetic violation written to tmp_path. A gate
that cannot fail is worse than no gate: it reports the property is protected while
protecting nothing. The self-tests are how we know these can fail, without ever
modifying the real source to find out.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from arc.storage.schema import EXPECTED_TABLES, FORBIDDEN_TABLES

# Modules permitted to do the thing a gate otherwise forbids, and why.
_CLOCK_MODULE = "clock.py"
_STORAGE_PACKAGE = "storage"
_MARKET_PACKAGE = "market"
# The two packages permitted to open a socket. arc/notify/ is outbound-only: it sends
# and has no receive path, which is what makes the second exemption narrower than it
# reads. Any third entry here needs the same argument made explicitly.
_NETWORK_PACKAGES = (_MARKET_PACKAGE, "notify")

_NETWORK_MODULES = frozenset({"httpx", "websockets", "requests", "aiohttp", "urllib"})
_MONEY_MODULES = ("money.py", "models.py", "config.py")
_FORBIDDEN_METHODS = ("reset", "clear", "reuse", "reinit", "recycle")
_STATEFUL_CLASSES = ("MarketInstance", "TwapAccumulator", "ExecutionWindow")


# ── source access ────────────────────────────────────────────────────────────


def _python_files(source_root: Path) -> tuple[Path, ...]:
    return tuple(sorted((source_root / "arc").rglob("*.py")))


def _parsed(source_root: Path) -> tuple[tuple[Path, ast.Module], ...]:
    return tuple(
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in _python_files(source_root)
    )


def _parse_one(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# ── detectors ────────────────────────────────────────────────────────────────
# Each takes parsed source and returns a list of offender descriptions. Pure, so
# the self-tests at the bottom can drive them with synthetic input.


def _is_final_annotation(annotation: ast.expr) -> bool:
    if isinstance(annotation, ast.Name):
        return annotation.id == "Final"
    if isinstance(annotation, ast.Subscript):
        base = annotation.value
        if isinstance(base, ast.Name):
            return base.id == "Final"
        if isinstance(base, ast.Attribute):
            return base.attr == "Final"
    if isinstance(annotation, ast.Attribute):
        return annotation.attr == "Final"
    return False


def find_mutable_module_state(files: tuple[tuple[Path, ast.Module], ...]) -> list[str]:
    """Mutable containers assigned at module scope.

    Module scope is shared by every MarketInstance ever created, so a dict here
    undoes the per-market isolation A11 buys by discarding instances.
    """
    offenders: list[str] = []
    mutable = (ast.Dict, ast.List, ast.Set, ast.SetComp, ast.ListComp, ast.DictComp)

    for path, tree in files:
        for node in tree.body:  # module scope only, not nested scopes
            if not isinstance(node, ast.Assign | ast.AnnAssign):
                continue

            targets = [node.target] if isinstance(node, ast.AnnAssign) else list(node.targets)
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if not names:
                continue

            # __all__ is a dunder convention read at import and never mutated.
            # Its own gate below asserts it exists; flagging it here would make the
            # two contradict each other.
            if all(n.startswith("__") and n.endswith("__") for n in names):
                continue

            # A Final[...] annotation is the codebase's own marker for a deliberate
            # constant, and mypy already enforces it is not rebound.
            if isinstance(node, ast.AnnAssign) and _is_final_annotation(node.annotation):
                continue

            if node.value is not None and isinstance(node.value, mutable):
                offenders.append(f"{path.name}:{node.lineno} {', '.join(names)}")

    return offenders


def find_global_statements(files: tuple[tuple[Path, ast.Module], ...]) -> list[str]:
    """`global` is how module state gets mutated after import."""
    return [
        f"{path.name}:{node.lineno} global {', '.join(node.names)}"
        for path, tree in files
        for node in ast.walk(tree)
        if isinstance(node, ast.Global)
    ]


def find_reset_methods(tree: ast.Module, classes: tuple[str, ...]) -> list[str]:
    """reset/clear/reuse methods on classes whose state must be discarded."""
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name not in classes:
            continue
        for item in node.body:
            if (
                isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                and item.name.lstrip("_").lower() in _FORBIDDEN_METHODS
            ):
                offenders.append(f"{node.name}.{item.name} at line {item.lineno}")
    return offenders


def find_property_setters(tree: ast.Module, class_name: str, prop: str) -> list[str]:
    """@<prop>.setter on a class — a second write path to a write-once field."""
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef):
                continue
            for decorator in item.decorator_list:
                if (
                    isinstance(decorator, ast.Attribute)
                    and decorator.attr == "setter"
                    and isinstance(decorator.value, ast.Name)
                    and decorator.value.id == prop
                ):
                    offenders.append(f"{class_name}.{item.name} at line {item.lineno}")
    return offenders


def _docstring_ids(tree: ast.Module) -> set[int]:
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }


def find_reachable_token(
    files: tuple[tuple[Path, ast.Module], ...], token: str
) -> list[str]:
    """A token as reachable code: identifier, attribute, parameter, or literal.

    Prose is excluded on purpose. The comments in enums.py and config.py discuss
    TESTNET's absence at length, and that documentation is the point — it tells the
    next reader why adding the member back is not a small change. Only an
    identifier, attribute, or string literal can be reached at runtime.
    """
    needle = token.lower()
    offenders: list[str] = []

    for path, tree in files:
        docstrings = _docstring_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and needle in node.id.lower():
                offenders.append(f"{path.name}:{node.lineno} identifier {node.id}")
            elif isinstance(node, ast.Attribute) and needle in node.attr.lower():
                offenders.append(f"{path.name}:{node.lineno} attribute .{node.attr}")
            elif isinstance(node, ast.arg) and needle in node.arg.lower():
                offenders.append(f"{path.name}:{node.lineno} parameter {node.arg}")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and needle in node.value.lower()
                and id(node) not in docstrings
            ):
                offenders.append(f"{path.name}:{node.lineno} string literal")

    return offenders


def find_direct_clock_reads(
    files: tuple[tuple[Path, ast.Module], ...], allowed_module: str
) -> list[str]:
    """time.time() / time.monotonic() outside the Clock implementations.

    A direct clock read is untestable by construction: no test can place the
    process at close_ts - 3.0s to prove a window opens.
    """
    offenders: list[str] = []
    for path, tree in files:
        if path.name == allowed_module:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "time"
                and func.attr in ("time", "monotonic")
            ):
                offenders.append(f"{path.name}:{node.lineno} time.{func.attr}()")
    return offenders


def find_imports_of(
    files: tuple[tuple[Path, ast.Module], ...],
    modules: frozenset[str],
    *,
    exempt_package: str | None = None,
    exempt_packages: tuple[str, ...] = (),
) -> list[str]:
    """Imports of given top-level modules, exempting the named package(s)."""
    exempt = {*exempt_packages, *(() if exempt_package is None else (exempt_package,))}
    offenders: list[str] = []
    for path, tree in files:
        if exempt & set(path.parts):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module.split(".")[0]] if node.module else []
            else:
                continue
            offenders.extend(
                f"{path.name}:{node.lineno} {name}" for name in names if name in modules
            )
    return offenders


def find_float_calls(
    files: tuple[tuple[Path, ast.Module], ...], module_names: tuple[str, ...]
) -> list[str]:
    """float() in the money path.

    Reintroduces exactly the representation error the money module excludes, and
    silently: 0.85 becomes 0.84999999999999998 and crosses an entry cap.
    """
    offenders: list[str] = []
    for path, tree in files:
        if path.name not in module_names:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "float"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    return offenders


def parse_schema_columns(text: str) -> tuple[tuple[int, str, str], ...]:
    """Column declarations inside CREATE TABLE blocks: (line, name, declaration).

    Scoped to the table bodies deliberately. A scan over every line in schema.py
    also matches the module docstring — which discusses REAL and TEXT columns in
    prose — and a gate that trips on its own explanatory comment gets deleted
    rather than fixed.
    """
    columns: list[tuple[int, str, str]] = []
    depth = 0

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line.startswith("--") or not line:
            continue

        if "CREATE TABLE" in line:
            depth = line.count("(") - line.count(")")
            continue

        if depth <= 0:
            continue

        # Table-level constraints are not column declarations.
        if not line.upper().startswith(("PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK")):
            name = line.split()[0]
            if name.isidentifier():
                columns.append((lineno, name, line))

        depth += line.count("(") - line.count(")")

    return tuple(columns)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def parsed_source(source_root: Path) -> tuple[tuple[Path, ast.Module], ...]:
    return _parsed(source_root)


@pytest.fixture
def schema_text(source_root: Path) -> str:
    return (source_root / "arc" / "storage" / "schema.py").read_text(encoding="utf-8")


# ── gates ────────────────────────────────────────────────────────────────────


class TestNoModuleLevelMutableState:
    """The gate pyproject.toml defers to instead of enabling RUF012.

    RUF012 would additionally demand ClassVar on pydantic model config dicts where
    it is wrong, so the real enforcement lives here.
    """

    def test_no_mutable_module_level_assignments(
        self, parsed_source: tuple[tuple[Path, ast.Module], ...]
    ) -> None:
        offenders = find_mutable_module_state(parsed_source)
        assert not offenders, (
            "mutable state at module scope — it is shared by every market instance "
            "and defeats the per-market isolation of A11:\n  " + "\n  ".join(offenders)
        )

    def test_no_global_statements(
        self, parsed_source: tuple[tuple[Path, ast.Module], ...]
    ) -> None:
        offenders = find_global_statements(parsed_source)
        assert not offenders, "global mutation found:\n  " + "\n  ".join(offenders)


class TestNoResetPaths:
    """A11: per-market state is discarded, never cleared.

    The absence of these methods IS the guarantee. With a reset() available, the
    market rotation path can call it instead of constructing a new instance, and
    the resulting stale-state bug looks identical to correct behaviour until a
    field is added that reset() forgets to clear.
    """

    def test_stateful_classes_have_no_reset_method(self, source_root: Path) -> None:
        tree = _parse_one(source_root / "arc" / "domain" / "models.py")
        offenders = find_reset_methods(tree, _STATEFUL_CLASSES)
        assert not offenders, (
            "a reset/clear path exists on per-market state. A11 satisfies "
            "'TWAP resets per market' by discarding the instance; a reset method is "
            "one a future call site can forget to keep complete:\n  "
            + "\n  ".join(offenders)
        )

    def test_ptb_has_no_setter(self, source_root: Path) -> None:
        """PTB is frozen once. A property setter would be a second write path."""
        tree = _parse_one(source_root / "arc" / "domain" / "models.py")
        offenders = find_property_setters(tree, "MarketInstance", "ptb")
        assert not offenders, (
            "PTB has a property setter; it must only be writable through "
            "freeze_ptb(), which refuses a second call:\n  " + "\n  ".join(offenders)
        )

    def test_runtime_refuses_a_second_ptb_freeze(self) -> None:
        """The structural gate above, confirmed behaviourally."""
        from arc.domain.models import MarketInstance

        market = MarketInstance.create(window_ts=1754400000, offsets=(3, 5))
        market.freeze_ptb("120000.00")
        with pytest.raises(ValueError, match="already frozen"):
            market.freeze_ptb("120000.00")  # identical value still refused


class TestNoTestnetAnywhere:
    """A3: TESTNET does not exist as a value, a branch, or a string.

    Not "is rejected" — does not exist. A guarded TESTNET branch is a branch that
    can be reached by a config typo or a future refactor, and the failure mode is
    testnet prices driving a real-money order.
    """

    def test_testnet_is_not_reachable_code(
        self, parsed_source: tuple[tuple[Path, ast.Module], ...]
    ) -> None:
        offenders = find_reachable_token(parsed_source, "testnet")
        assert not offenders, (
            "TESTNET exists as reachable code (A3). It must not be a value, a "
            "branch, or a literal — only prose explaining its absence:\n  "
            + "\n  ".join(offenders)
        )

    def test_the_absence_stays_documented(self, source_root: Path) -> None:
        """The prose must stay. Without it, someone re-adds the member as a favour."""
        enums_text = (source_root / "arc" / "domain" / "enums.py").read_text(encoding="utf-8")
        assert "TESTNET" in enums_text, (
            "the comment explaining why TESTNET does not exist was removed; it is "
            "what stops the member being helpfully re-added"
        )

    def test_mode_enum_has_exactly_two_members(self) -> None:
        from arc.domain.enums import Mode

        assert [m.value for m in Mode] == ["V1", "V2"]

    def test_mode_rejects_unknown_values_as_typos(self) -> None:
        """TESTNET fails as an unknown value, like any other typo — not as a case."""
        from arc.config import ArcSettings

        with pytest.raises(Exception, match="MODE must be one of"):
            ArcSettings(mode="TESTNET")  # type: ignore[arg-type]


class TestClockIsInjected:
    """Nothing reads the wall clock directly except the Clock implementations."""

    def test_no_direct_time_calls_outside_clock_module(
        self, parsed_source: tuple[tuple[Path, ast.Module], ...]
    ) -> None:
        offenders = find_direct_clock_reads(parsed_source, _CLOCK_MODULE)
        assert not offenders, (
            "direct clock read outside clock.py — inject a Clock instead, or the "
            "timing this guards can never be tested:\n  " + "\n  ".join(offenders)
        )

    def test_logging_setup_only_formats_existing_timestamps(self, source_root: Path) -> None:
        """time.localtime/strftime are formatting, not clock reads. Allowed."""
        text = (source_root / "arc" / "logging_setup.py").read_text(encoding="utf-8")
        assert "record.created" in text, (
            "the log formatter must render the record's own timestamp rather than "
            "reading the clock again, or a log line's time drifts from its event"
        )
        assert "time.time()" not in text


class TestStorageIsTheOnlySqlBoundary:
    """The Store is the only component that touches SQLite.

    Guarantees like "PTB is written once" are enforced by a single UPDATE with a
    WHERE clause. A second module holding a connection can bypass it, and the
    guarantee silently becomes a convention.
    """

    def test_sqlite3_imported_only_inside_storage_package(
        self, parsed_source: tuple[tuple[Path, ast.Module], ...]
    ) -> None:
        offenders = find_imports_of(
            parsed_source, frozenset({"sqlite3"}), exempt_package=_STORAGE_PACKAGE
        )
        assert not offenders, (
            "sqlite3 imported outside arc/storage/ — the single write path is what "
            "makes the write-once guarantees enforceable:\n  " + "\n  ".join(offenders)
        )

    def test_no_sql_statements_outside_storage_package(self, source_root: Path) -> None:
        """SQL text outside the Store means a second query path exists."""
        offenders: list[str] = []
        statements = ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE")

        for path in _python_files(source_root):
            if _STORAGE_PACKAGE in path.parts:
                continue
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for statement in statements:
                    if statement in line:
                        offenders.append(f"{path.name}:{lineno}  {stripped[:60]}")

        assert not offenders, "SQL outside the Store:\n  " + "\n  ".join(offenders)


class TestNoEventSourcing:
    """A4: event sourcing is removed as an architecture.

    What survives is write-before-act. These tables reappearing would mean the
    replay/projection machinery had come back, and with it the failure mode where
    restart recovery *recomputes* a frozen trigger instead of reloading it.
    """

    def test_forbidden_tables_are_not_in_the_schema(self, schema_text: str) -> None:
        declared = {name for _, name, _ in parse_schema_columns(schema_text)}
        for table in FORBIDDEN_TABLES:
            assert f"CREATE TABLE IF NOT EXISTS {table}" not in schema_text, (
                f"table {table!r} was reintroduced; event sourcing is removed (A4)"
            )
        # Nothing named like an event log slipped in under another table either.
        assert not {c for c in declared if c.startswith(("event_", "projection_"))}

    def test_expected_and_forbidden_table_sets_are_disjoint(self) -> None:
        assert not set(EXPECTED_TABLES) & set(FORBIDDEN_TABLES)

    def test_no_replay_or_projection_functions(
        self, parsed_source: tuple[tuple[Path, ast.Module], ...]
    ) -> None:
        offenders: list[str] = []
        forbidden = ("replay_events", "rebuild_projection", "apply_event", "project_state")
        for path, tree in parsed_source:
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                    and node.name in forbidden
                ):
                    offenders.append(f"{path.name}:{node.lineno} {node.name}")
        assert not offenders, "event-sourcing machinery:\n  " + "\n  ".join(offenders)


class TestMoneyNeverTouchesFloat:
    """Money is Decimal end to end, including across the storage boundary."""

    def test_no_float_construction_in_money_modules(
        self, parsed_source: tuple[tuple[Path, ast.Module], ...]
    ) -> None:
        offenders = find_float_calls(parsed_source, _MONEY_MODULES)
        assert not offenders, (
            "float() in the money path — the binary error this admits is what "
            "moves a price across a tick boundary:\n  " + "\n  ".join(offenders)
        )

    _MONEY_COLUMNS = (
        "ptb",
        "running_sum",
        "settlement_twap",
        "opening_twap",
        "buffer",
        "locked_trigger",
        "price",
        "size",
        "filled_size",
        "pnl",
    )

    def test_money_columns_are_declared_text(self, schema_text: str) -> None:
        """A REAL money column loses the guarantee at the storage boundary."""
        seen: set[str] = set()
        for lineno, column, declaration in parse_schema_columns(schema_text):
            if column in self._MONEY_COLUMNS:
                seen.add(column)
                assert "TEXT" in declaration, (
                    f"money column {column!r} at line {lineno} is not TEXT: "
                    f"{declaration!r}. A REAL column stores 0.85 as 0.84999999999999998"
                )
        missing = set(self._MONEY_COLUMNS) - seen
        assert not missing, (
            f"money columns never found in the schema scan: {sorted(missing)}. The "
            "gate would otherwise pass vacuously"
        )

    def test_no_real_columns_except_timestamps(self, schema_text: str) -> None:
        """REAL is permitted only for timestamps, never for a quantity."""
        for lineno, column, declaration in parse_schema_columns(schema_text):
            if "REAL" not in declaration:
                continue
            assert column.endswith(("_at", "_ts")) or column == "ts", (
                f"schema.py line {lineno} declares REAL column {column!r}; REAL is "
                "for timestamps only, never a quantity"
            )

    def test_decimal_survives_the_text_round_trip(self) -> None:
        """The storage guarantee, confirmed behaviourally rather than structurally."""
        from decimal import Decimal

        from arc.domain.money import dec_str, to_decimal

        for value in ("0.85", "120000.00", "0.7449", "1E-7", "0.000000001"):
            assert to_decimal(dec_str(value)) == Decimal(value)


class TestNoFakeRuntime:
    """A1 Rule 2: nothing is stubbed to make an unbuilt path look functional."""

    def test_there_is_no_third_runtime_mode(self) -> None:
        """Q4: V1 and V2 only. A legacy observe path would be an unused runtime."""
        from arc.cli import main
        from arc.domain.enums import Mode

        assert [m.value for m in Mode] == ["V1", "V2"]
        for argv in (["observe"], ["run", "--mode=observe"], ["run"]):
            with pytest.raises(SystemExit):
                main(argv)

    def test_network_imports_are_confined_to_the_market_and_notify_packages(
        self, parsed_source: tuple[tuple[Path, ast.Module], ...]
    ) -> None:
        """Only arc/market/ and arc/notify/ may open a socket.

        Phase 2 introduced the feed and discovery, so "no network anywhere" is no
        longer the invariant — the invariant is that the domain, storage and config
        layers still cannot reach the network. That is what makes their behaviour
        reproducible from a test with no venue, and what stops a "just fetch it here"
        call appearing inside the money path where it would make a price depend on
        whether a request happened to succeed.

        arc/notify/ is exempt deliberately, not incidentally. Notifications are
        outbound by definition, and the alternative — a Telegram sender hidden inside
        arc/market/ to satisfy this list — would put a chat client in the package that
        produces prices, which is a worse arrangement than naming the exemption. What
        keeps it safe is that arc/notify/ sends and never receives: it is only listed
        here, not granted any inbound path.
        """
        offenders = find_imports_of(
            parsed_source, _NETWORK_MODULES, exempt_packages=_NETWORK_PACKAGES
        )
        assert not offenders, (
            "network import outside arc/market/ and arc/notify/ — the layers below "
            "them must stay testable without a venue:\n  " + "\n  ".join(offenders)
        )

    def test_no_placeholder_markers(self, source_root: Path) -> None:
        """A TODO or a NotImplementedError is an unbuilt path that ships."""
        offenders: list[str] = []
        for path in _python_files(source_root):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                stripped = line.strip()
                for marker in ("TODO", "FIXME", "XXX", "HACK", "NotImplementedError"):
                    if marker in stripped and not stripped.startswith("#"):
                        offenders.append(f"{path.name}:{lineno} {marker}")
        assert not offenders, "placeholder in shipped code:\n  " + "\n  ".join(offenders)


class TestErrorTaxonomyIsEnforced:
    """Fatal and operational errors must not blur.

    A config error that is merely logged boots a bot trading a configuration nobody
    authorised; a network error that is fatal takes down a process that should have
    kept its dashboard and its recorded data (A8).
    """

    def test_config_errors_are_fatal(self) -> None:
        from arc.errors import (
            ArcError,
            ArcFatalError,
            BindAddressError,
            ConfigInvariantError,
            SchemaMigrationError,
        )

        for error in (ConfigInvariantError, BindAddressError, SchemaMigrationError):
            assert issubclass(error, ArcFatalError)
            assert not issubclass(error, ArcError)

    def test_operational_errors_are_not_fatal(self) -> None:
        from arc.errors import (
            ArcError,
            ArcFatalError,
            CancelAckTimeoutError,
            ConnectionLostError,
            FeedError,
            ObservationRejectedError,
            PriceToBeatUnavailableError,
            StorageError,
            TransientLatencyRejectError,
            WindowFreezeError,
        )

        operational = (
            StorageError,
            FeedError,
            ConnectionLostError,
            TransientLatencyRejectError,
            CancelAckTimeoutError,
            PriceToBeatUnavailableError,
            ObservationRejectedError,
            WindowFreezeError,
        )
        for error in operational:
            assert issubclass(error, ArcError)
            assert not issubclass(error, ArcFatalError), (
                f"{error.__name__} is fatal; a market-level failure must not kill a "
                "process that should keep recording data (A8)"
            )


class TestDenialReasonsHaveNoLeadTimeGate:
    """A10/D1: the lead-time gate is repealed, not merely unused.

    The only execution boundary is the CANCELLING phase. A timing-based denial
    reason would be the re-entry point for a clock comparison deciding whether an
    order is "too late".
    """

    def test_no_timing_based_denial_reason(self) -> None:
        from arc.domain.enums import DenialReason

        forbidden = ("LEAD_TIME", "TOO_LATE", "TOO_CLOSE", "INSUFFICIENT_TIME", "DEADLINE")
        for reason in DenialReason:
            for pattern in forbidden:
                assert pattern not in reason.value, (
                    f"{reason.value} is a timing gate; the only execution boundary is "
                    "the CANCELLING phase (A10/D1)"
                )

    def test_market_cancelling_reason_exists(self) -> None:
        from arc.domain.enums import DenialReason

        assert DenialReason.MARKET_CANCELLING.value == "MARKET_CANCELLING"


class TestPublicApiIsDeclared:
    """Every module declares __all__, so its surface is deliberate."""

    def test_every_module_declares_all(
        self, parsed_source: tuple[tuple[Path, ast.Module], ...]
    ) -> None:
        missing = [
            path.name
            for path, tree in parsed_source
            if path.name != "__init__.py"
            and not any(
                isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
                for node in tree.body
            )
        ]
        assert not missing, f"modules without __all__: {missing}"


# ── the gates' own tests ─────────────────────────────────────────────────────


def _synthetic(tmp_path: Path, filename: str, source: str) -> tuple[tuple[Path, ast.Module], ...]:
    """Write a throwaway module to tmp_path and parse it.

    Violations are only ever constructed here, in a temporary directory. The real
    source under arc/ is never modified to test a gate — a gate proven by breaking
    production code is one that leaves production code broken when the run dies
    partway through.
    """
    path = tmp_path / filename
    path.write_text(source, encoding="utf-8")
    return ((path, ast.parse(source, filename=str(path))),)


class TestGatesActuallyBite:
    """Each detector, driven against a synthetic violation.

    A structural gate is a claim that something cannot happen. Without these, a
    detector with an inverted condition or a typo'd node type reports every
    property protected while inspecting nothing — and it would keep reporting that
    on the day the property was actually broken.
    """

    def test_mutable_module_state_is_detected(self, tmp_path: Path) -> None:
        files = _synthetic(tmp_path, "bad.py", "CACHE = {}\nSEEN: set[str] = set()\n")
        assert find_mutable_module_state(files)

    def test_final_constants_are_not_flagged(self, tmp_path: Path) -> None:
        files = _synthetic(
            tmp_path,
            "ok.py",
            "from typing import Final\n_MAP: Final[dict[int, str]] = {1: 'a'}\n",
        )
        assert not find_mutable_module_state(files)

    def test_dunder_all_is_not_flagged(self, tmp_path: Path) -> None:
        files = _synthetic(tmp_path, "ok.py", "__all__ = ['a', 'b']\n")
        assert not find_mutable_module_state(files)

    def test_immutable_module_constants_are_not_flagged(self, tmp_path: Path) -> None:
        files = _synthetic(tmp_path, "ok.py", "OFFSETS = (3, 5, 7)\nNAME = 'arc'\n")
        assert not find_mutable_module_state(files)

    def test_global_statement_is_detected(self, tmp_path: Path) -> None:
        files = _synthetic(tmp_path, "bad.py", "x = 1\ndef f() -> None:\n    global x\n    x = 2\n")
        assert find_global_statements(files)

    def test_reset_method_is_detected(self, tmp_path: Path) -> None:
        source = (
            "class TwapAccumulator:\n"
            "    def reset(self) -> None:\n"
            "        self.running_sum = 0\n"
        )
        tree = ast.parse(source)
        assert find_reset_methods(tree, _STATEFUL_CLASSES)

    def test_clear_and_private_reset_are_detected(self, tmp_path: Path) -> None:
        for name in ("clear", "_reset", "reuse", "recycle"):
            tree = ast.parse(f"class MarketInstance:\n    def {name}(self) -> None:\n        pass\n")
            assert find_reset_methods(tree, _STATEFUL_CLASSES), f"{name} not caught"

    def test_reset_on_an_unrelated_class_is_not_flagged(self) -> None:
        """The gate is scoped to per-market state, not every class in the file."""
        tree = ast.parse("class RateLimiter:\n    def reset(self) -> None:\n        pass\n")
        assert not find_reset_methods(tree, _STATEFUL_CLASSES)

    def test_property_setter_is_detected(self) -> None:
        source = (
            "class MarketInstance:\n"
            "    @property\n"
            "    def ptb(self): ...\n"
            "    @ptb.setter\n"
            "    def ptb(self, v): ...\n"
        )
        assert find_property_setters(ast.parse(source), "MarketInstance", "ptb")

    def test_getter_alone_is_not_flagged(self) -> None:
        source = "class MarketInstance:\n    @property\n    def ptb(self): ...\n"
        assert not find_property_setters(ast.parse(source), "MarketInstance", "ptb")

    def test_testnet_enum_member_is_detected(self, tmp_path: Path) -> None:
        files = _synthetic(
            tmp_path, "bad.py", "class Mode:\n    TESTNET = 'TESTNET'\n    V1 = 'V1'\n"
        )
        assert find_reachable_token(files, "testnet")

    def test_testnet_branch_is_detected(self, tmp_path: Path) -> None:
        files = _synthetic(
            tmp_path, "bad.py", "def f(mode):\n    if mode == 'testnet':\n        return 1\n"
        )
        assert find_reachable_token(files, "testnet")

    def test_testnet_in_prose_is_not_flagged(self, tmp_path: Path) -> None:
        """The real enums.py relies on exactly this exemption."""
        files = _synthetic(
            tmp_path,
            "ok.py",
            '"""There is deliberately no TESTNET member."""\n'
            "# TESTNET does not exist here either\n"
            "V1 = 'V1'\n",
        )
        assert not find_reachable_token(files, "testnet")

    def test_direct_clock_read_is_detected(self, tmp_path: Path) -> None:
        files = _synthetic(tmp_path, "engine.py", "import time\ndef f():\n    return time.time()\n")
        assert find_direct_clock_reads(files, _CLOCK_MODULE)
        assert find_direct_clock_reads(
            _synthetic(tmp_path, "e2.py", "import time\ndef f():\n    return time.monotonic()\n"),
            _CLOCK_MODULE,
        )

    def test_clock_module_itself_is_exempt(self, tmp_path: Path) -> None:
        files = _synthetic(tmp_path, _CLOCK_MODULE, "import time\ndef f():\n    return time.time()\n")
        assert not find_direct_clock_reads(files, _CLOCK_MODULE)

    def test_strftime_is_not_a_clock_read(self, tmp_path: Path) -> None:
        files = _synthetic(
            tmp_path, "fmt.py", "import time\ndef f(c):\n    return time.strftime('%H', time.localtime(c))\n"
        )
        assert not find_direct_clock_reads(files, _CLOCK_MODULE)

    def test_sqlite_import_outside_storage_is_detected(self, tmp_path: Path) -> None:
        files = _synthetic(tmp_path, "engine.py", "import sqlite3\n")
        assert find_imports_of(files, frozenset({"sqlite3"}), exempt_package=_STORAGE_PACKAGE)
        assert find_imports_of(
            _synthetic(tmp_path, "e2.py", "from sqlite3 import Row\n"),
            frozenset({"sqlite3"}),
            exempt_package=_STORAGE_PACKAGE,
        )

    def test_network_import_is_detected(self, tmp_path: Path) -> None:
        for source in ("import httpx\n", "import websockets\n", "from httpx import AsyncClient\n"):
            files = _synthetic(tmp_path, "feed.py", source)
            assert find_imports_of(files, _NETWORK_MODULES), source

    def test_float_call_in_money_module_is_detected(self, tmp_path: Path) -> None:
        files = _synthetic(tmp_path, "money.py", "def f(v):\n    return float(v)\n")
        assert find_float_calls(files, ("money.py",))

    def test_float_annotation_is_not_a_call(self, tmp_path: Path) -> None:
        """Taking a float timestamp is fine; constructing one from money is not."""
        files = _synthetic(tmp_path, "money.py", "def f(ts: float) -> float:\n    return ts\n")
        assert not find_float_calls(files, ("money.py",))

    def test_real_money_column_is_detected(self) -> None:
        schema = (
            "CREATE TABLE IF NOT EXISTS settlements (\n"
            "    market_slug  TEXT PRIMARY KEY,\n"
            "    pnl          REAL NOT NULL DEFAULT 0,\n"
            "    settled_at   REAL NOT NULL\n"
            ");\n"
        )
        columns = parse_schema_columns(schema)
        by_name = {name: decl for _, name, decl in columns}
        assert "REAL" in by_name["pnl"], "the scanner must see pnl's declaration"
        assert "TEXT" not in by_name["pnl"]
        # And the timestamp exemption must not swallow it.
        assert not "pnl".endswith(("_at", "_ts"))

    def test_schema_scanner_ignores_prose_and_constraints(self) -> None:
        schema = (
            '"""Every money column is TEXT. A REAL column would store 0.85 badly."""\n'
            "CREATE TABLE IF NOT EXISTS intents (\n"
            "    intent_id   TEXT PRIMARY KEY,\n"
            "    -- a comment mentioning REAL\n"
            "    created_at  REAL NOT NULL,\n"
            "    UNIQUE (market_slug, offset_seconds)\n"
            ");\n"
        )
        names = [name for _, name, _ in parse_schema_columns(schema)]
        assert names == ["intent_id", "created_at"], names
