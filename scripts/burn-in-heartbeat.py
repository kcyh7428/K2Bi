#!/usr/bin/env python3
"""Send the daily K2Bi burn-in heartbeat to Telegram.

The heartbeat is read-only: it queries IB Gateway state, reads the decision
journal, formats one mobile-sized message, prints it to stdout for cron audit,
then pipes it through scripts/send-telegram.sh.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import io
import json
import os
import queue
import subprocess
import sys
import threading
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple


if __name__ == "__main__" and __package__ is None:
    _project_root = Path(__file__).resolve().parent.parent
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))

try:
    from ib_async import IB
except ImportError:  # pragma: no cover - exercised on hosts without ib_async
    IB = None  # type: ignore[assignment]

try:
    from execution.journal.schema import EVENT_TYPES
except Exception:  # pragma: no cover - fail-open for message generation only
    EVENT_TYPES = frozenset()


DEFAULT_VAULT_ROOT = Path.home() / "Projects" / "K2Bi-Vault"
JOURNAL_DIR = Path("raw") / "journal"
BURN_IN_STATE_REL = Path("System") / "burn-in-start.json"
ENGINE_SERVICE = "k2bi-engine.service"
IB_HOST = "127.0.0.1"
IB_PORT = 4002
# Spec-mandated read-only monitoring client ID. Do not change without an
# explicit operator/client-ID allocation ruling.
IB_CLIENT_ID = 99
BROKER_QUERY_TIMEOUT_SECONDS = 20
HKT = timezone(timedelta(hours=8), "HKT")

ROUTINE_ALWAYS = frozenset(
    {
        "cycle_evaluated_skip_position_held",
        "recovery_self_healed_pending_order",
    }
)
ROUTINE_ONCE_PER_BOOT = frozenset({"engine_started", "engine_recovered"})
ANOMALY_EVENTS = frozenset(
    {
        "order_proposed",
        "order_submitted",
        "order_filled",
        "order_terminal",
        "recovery_state_mismatch",
        "kill_switch_active",
        "circuit_breaker_tripped_rapid_fire",
        "JournalDurabilityError",
        "engine_stopped",
        "position_visibility_lost",
    }
)


class EngineState(NamedTuple):
    status: str
    uptime: str


class PositionLine(NamedTuple):
    symbol: str
    qty: Any
    avg_cost: Any
    stop_price: Any
    stop_status: str


class BrokerSnapshot(NamedTuple):
    positions: list[PositionLine]
    error: str | None


class JournalWindow(NamedTuple):
    events: list[dict[str, Any]]
    error: str | None


class Anomaly(NamedTuple):
    ts: str
    event_type: str
    detail: str


class HeartbeatResult(NamedTuple):
    exit_code: int
    message: str


class BrokerQueryTimeout(TimeoutError):
    """Raised when the read-only IBKR heartbeat query exceeds its deadline."""


def _parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _strip_env_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_project_env(project_root: Path) -> None:
    """Load KEY=value pairs from project .env without overriding the environment."""
    path = project_root / ".env"
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"[warn] could not read {path}: {exc}", file=sys.stderr)
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key.startswith("export "):
            key = key.removeprefix("export ").strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _strip_env_quotes(value.strip())


def _systemctl(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["systemctl", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if stderr:
            print(f"[warn] systemctl {' '.join(args)} failed: {stderr}", file=sys.stderr)
        return None
    return result.stdout.strip()


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _active_uptime() -> str:
    active_us_raw = _systemctl(
        ["show", ENGINE_SERVICE, "--property=ActiveEnterTimestampMonotonic", "--value"]
    )
    if not active_us_raw:
        return "uptime unknown"
    try:
        active_seconds = int(active_us_raw) / 1_000_000
        boot_seconds = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return "uptime unknown"
    if active_seconds > boot_seconds:
        return "uptime unknown"
    return _format_duration(boot_seconds - active_seconds)


def get_engine_state() -> EngineState:
    """Return systemd activity for the engine service."""
    active = _systemctl(["is-active", ENGINE_SERVICE])
    status = "active" if active == "active" else "inactive"
    uptime = _active_uptime() if status == "active" else "uptime n/a"
    return EngineState(status=status, uptime=uptime)


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _format_qty(value: Any) -> str:
    decimal = _as_decimal(value)
    if decimal is None:
        return "?"
    if decimal == decimal.to_integral_value():
        return str(int(decimal))
    return format(decimal.normalize(), "f")


def _format_avg(value: Any) -> str:
    decimal = _as_decimal(value)
    if decimal is None:
        return "?"
    return format(decimal.quantize(Decimal("0.01")), "f")


def _format_stop(value: Any) -> str:
    decimal = _as_decimal(value)
    if decimal is None:
        return "?"
    rounded = decimal.quantize(Decimal("0.01"))
    text = format(rounded, "f")
    return text.rstrip("0").rstrip(".")


def _position_rows(ib: Any) -> dict[str, tuple[Any, Any]]:
    rows: dict[str, tuple[Any, Any]] = {}
    for position in ib.positions():
        contract = getattr(position, "contract", None)
        symbol = getattr(contract, "symbol", "")
        if symbol in {"G", "SPY"}:
            rows[symbol] = (
                getattr(position, "position", None),
                getattr(position, "avgCost", None),
            )
    return rows


def _open_order_rows(ib: Any) -> dict[str, tuple[Any, str]]:
    trades = ib.reqAllOpenOrders()
    if hasattr(ib, "sleep"):
        ib.sleep(2)
    if trades is None and hasattr(ib, "trades"):
        trades = ib.trades()
    stops: dict[str, tuple[Any, str]] = {}
    for trade in trades or []:
        contract = getattr(trade, "contract", None)
        order = getattr(trade, "order", None)
        status = getattr(trade, "orderStatus", None)
        symbol = getattr(contract, "symbol", "")
        if symbol not in {"G", "SPY"}:
            continue
        if getattr(order, "action", "") != "SELL":
            continue
        if getattr(order, "orderType", "") != "STP":
            continue
        stops[symbol] = (
            getattr(order, "auxPrice", None),
            getattr(status, "status", "status unknown"),
        )
    return stops


def query_broker() -> BrokerSnapshot:
    """Read positions and protective stops through IBKR clientId 99."""
    if IB is None:
        return BrokerSnapshot(
            positions=_empty_position_lines("broker-unreachable"),
            error="ib_async unavailable",
        )
    results: queue.Queue[BrokerSnapshot | BaseException] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            results.put(_query_broker_inner())
        except BaseException as exc:  # noqa: BLE001 - ferry to caller
            results.put(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        result = results.get(timeout=BROKER_QUERY_TIMEOUT_SECONDS)
    except queue.Empty:
        result = BrokerQueryTimeout(
            f"broker query timed out after {BROKER_QUERY_TIMEOUT_SECONDS}s"
        )

    if isinstance(result, BrokerSnapshot):
        return result
    if isinstance(result, BrokerQueryTimeout):
        return BrokerSnapshot(
            positions=_empty_position_lines("broker-unreachable"),
            error=f"BrokerQueryTimeout: {result}",
        )
    return BrokerSnapshot(
        positions=_empty_position_lines("broker-unreachable"),
        error=f"{type(result).__name__}: {result}",
    )


def _query_broker_inner() -> BrokerSnapshot:
    """Perform the IBKR read under query_broker's outer deadline."""

    ib = IB()
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=10)
        position_rows = _position_rows(ib)
        stop_rows = _open_order_rows(ib)
    except Exception as exc:  # noqa: BLE001 - live broker heartbeat must report
        error = f"{type(exc).__name__}: {exc}"
        return BrokerSnapshot(
            positions=_empty_position_lines("broker-unreachable"),
            error=error,
        )
    finally:
        try:
            ib.disconnect()
        except Exception as exc:  # noqa: BLE001 - preserve heartbeat result
            print(
                f"[warn] broker disconnect failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    lines = []
    for symbol in ("G", "SPY"):
        qty, avg_cost = position_rows.get(symbol, (None, None))
        stop_price, stop_status = stop_rows.get(symbol, (None, "STP missing"))
        lines.append(
            PositionLine(
                symbol=symbol,
                qty=qty,
                avg_cost=avg_cost,
                stop_price=stop_price,
                stop_status=stop_status,
            )
        )
    return BrokerSnapshot(positions=lines, error=None)


def _empty_position_lines(status: str) -> list[PositionLine]:
    return [
        PositionLine("G", None, None, None, status),
        PositionLine("SPY", None, None, None, status),
    ]


def _event_ts(event: dict[str, Any]) -> tuple[datetime | None, str | None]:
    raw = event.get("ts")
    if not isinstance(raw, str):
        return None, "missing or non-string ts"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, f"invalid ts {raw!r}"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc), None


def _journal_dates_for_window(now: datetime) -> list[str]:
    today = now.date()
    yesterday = today - timedelta(days=1)
    return [yesterday.isoformat(), today.isoformat()]


def _lock_path_for(path: Path) -> Path:
    # Must match execution.journal.writer.JournalWriter._lock_path_for.
    return path.with_suffix(path.suffix + ".lock")


def _acquire_shared_lock(path: Path) -> int:
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_fd = os.open(str(_lock_path_for(path)), flags, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
    except Exception:
        os.close(lock_fd)
        raise
    return lock_fd


def _release_lock(lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def read_journal_window(vault_root: Path, now: datetime) -> JournalWindow:
    """Read journal events in the past 24h.

    The cron runs at 09:00 HKT, which is 01:00 UTC. Reading yesterday plus
    today keeps the actual 24h window intact across the UTC date boundary.
    """
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    existing_files = 0
    cutoff = now - timedelta(hours=24)
    for date_str in _journal_dates_for_window(now):
        path = vault_root / JOURNAL_DIR / f"{date_str}.jsonl"
        if not path.exists():
            continue
        existing_files += 1
        lock_fd: int | None = None
        try:
            lock_fd = _acquire_shared_lock(path)
            with open(path, "r", encoding="utf-8") as handle:
                for line_number, raw in enumerate(handle, start=1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        errors.append(
                            f"journal read failed for {path.name} "
                            f"line {line_number}: {exc.msg}"
                        )
                        continue
                    if not isinstance(event, dict):
                        continue
                    event_time, ts_error = _event_ts(event)
                    if ts_error is not None:
                        errors.append(
                            f"journal read failed for {path.name} "
                            f"line {line_number}: {ts_error}"
                        )
                    if event_time is None or event_time >= cutoff:
                        events.append(event)
        except OSError as exc:
            errors.append(f"journal read failed for {path.name}: {exc}")
        finally:
            if lock_fd is not None:
                _release_lock(lock_fd)

    if existing_files == 0:
        current_date = now.date().isoformat()
        return JournalWindow(events=[], error=f"no journal for date {current_date}")
    if errors:
        suffix = "" if len(errors) == 1 else f"; {len(errors) - 1} more read errors"
        return JournalWindow(events=events, error=f"{errors[0]}{suffix}")
    return JournalWindow(events=events, error=None)


def _payload_detail(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    candidates = (
        "symbol",
        "ticker",
        "strategy_id",
        "terminal_status",
        "broker_order_id",
        "broker_perm_id",
        "qty",
        "reason",
        "source",
        "error_class",
        "error",
        "pid",
    )
    for key in candidates:
        if key in payload and payload[key] not in (None, ""):
            return f"{key}={_shorten(payload[key])}"
        if key in event and event[key] not in (None, ""):
            return f"{key}={_shorten(event[key])}"
    entry_id = event.get("journal_entry_id")
    if entry_id:
        return f"journal_entry_id={entry_id}"
    return "payload={}"


def _shorten(value: Any, max_chars: int = 160) -> str:
    text = str(value).replace("\n", " ")
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def _is_journal_durability_error(event: dict[str, Any]) -> bool:
    if event.get("event_type") == "JournalDurabilityError":
        return True
    payload = event.get("payload")
    if isinstance(payload, dict) and payload.get("error_class") == "JournalDurabilityError":
        return True
    return event.get("error") == "JournalDurabilityError"


def classify_journal_events(events: list[dict[str, Any]]) -> tuple[int, list[Anomaly]]:
    event_counts = Counter(
        event.get("event_type")
        for event in events
        if isinstance(event.get("event_type"), str)
    )
    cycle_skips = 0
    anomalies: list[Anomaly] = []

    for event in events:
        event_type = event.get("event_type")
        if not isinstance(event_type, str) or not event_type:
            event_type = "unknown event_type"

        if event_type == "cycle_evaluated_skip_position_held":
            cycle_skips += 1
            continue
        if event_type in ROUTINE_ALWAYS:
            continue
        if (
            event_type in ROUTINE_ONCE_PER_BOOT
            and event_counts.get(event_type, 0) <= 1
        ):
            continue

        is_unknown = bool(EVENT_TYPES) and event_type not in EVENT_TYPES
        is_boot_bounce = (
            event_type in ROUTINE_ONCE_PER_BOOT
            and event_counts.get(event_type, 0) > 1
        )
        is_anomaly = (
            event_type in ANOMALY_EVENTS
            or is_unknown
            or is_boot_bounce
            or _is_journal_durability_error(event)
        )
        if is_anomaly:
            anomalies.append(
                Anomaly(
                    ts=str(event.get("ts", "?")),
                    event_type=event_type,
                    detail=_payload_detail(event),
                )
            )

    return cycle_skips, anomalies


def burn_in_line(vault_root: Path, now: datetime) -> str:
    path = vault_root / BURN_IN_STATE_REL
    if not path.exists():
        return "Burn-in: day ? (state file missing)"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state, dict):
            raise ValueError("state is not an object")
        day1_raw = (
            state.get("day1_date")
            or state.get("start_date")
            or state.get("burn_in_start_date")
        )
        if not isinstance(day1_raw, str):
            raise ValueError("missing day1_date")
        day1 = date.fromisoformat(day1_raw)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return f"Burn-in: day ? (state file invalid: {exc})"

    today_hkt = now.astimezone(HKT).date()
    day = max(1, (today_hkt - day1).days + 1)
    day = min(day, 90)
    status = str(state.get("status", "active")).strip().lower()

    if day > 5 and status in {"closed", "complete", "completed"}:
        return f"Burn-in: day {day} (CLOSED - see retro)"
    if day > 5:
        return f"Burn-in: day {day} of 5 (EXTENDED per architect ruling)"
    return f"Burn-in: day {day} of 5"


def _format_position(line: PositionLine) -> str:
    return (
        f"  {line.symbol}: {_format_qty(line.qty)} @ avg ${_format_avg(line.avg_cost)}, "
        f"STP ${_format_stop(line.stop_price)} {line.stop_status}"
    )


def build_message(
    now: datetime,
    engine_state: EngineState,
    broker: BrokerSnapshot,
    journal: JournalWindow,
    vault_root: Path,
) -> str:
    cycle_skips, anomalies = classify_journal_events(journal.events)
    if broker.error is not None:
        anomalies.append(
            Anomaly(
                ts=now.isoformat(),
                event_type="broker-unreachable",
                detail=_shorten(broker.error),
            )
        )
    if journal.error is not None:
        anomalies.append(
            Anomaly(
                ts=now.isoformat(),
                event_type="journal-read-failed",
                detail=_shorten(journal.error),
            )
        )

    day_str = now.astimezone(HKT).date().isoformat()
    lines = [
        f"🤖 K2Bi heartbeat {day_str}",
        "",
        f"Engine: {engine_state.status} {engine_state.uptime}",
        "Positions:",
    ]
    lines.extend(_format_position(position) for position in broker.positions)
    lines.extend(
        [
            "",
            f"Last 24h: {cycle_skips} cycle skips, {len(anomalies)} anomalies",
        ]
    )
    if anomalies:
        lines.append("Anomalies:")
        for anomaly in anomalies[:10]:
            lines.append(f"  {anomaly.ts} {anomaly.event_type} {anomaly.detail}")
        overflow = len(anomalies) - 10
        if overflow > 0:
            lines.append(f"  ({overflow} more, see journal)")

    lines.extend(["", burn_in_line(vault_root, now)])
    return "\n".join(lines)


def send_telegram(message: str, script_dir: Path | None = None) -> None:
    if script_dir is None:
        script_dir = Path(__file__).resolve().parent
    sender = script_dir / "send-telegram.sh"
    subprocess.run([str(sender)], input=message, text=True, check=True, timeout=60)


def run_heartbeat(vault_root: Path, now: datetime) -> HeartbeatResult:
    engine_state = get_engine_state()
    broker = query_broker()
    journal = read_journal_window(vault_root, now)
    message = build_message(now, engine_state, broker, journal, vault_root)
    if broker.error is not None:
        return HeartbeatResult(exit_code=1, message=message)
    if journal.error is not None:
        return HeartbeatResult(exit_code=2, message=message)
    return HeartbeatResult(exit_code=0, message=message)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send K2Bi burn-in heartbeat")
    parser.add_argument(
        "--vault-root",
        default=os.environ.get("K2BI_VAULT_ROOT", str(DEFAULT_VAULT_ROOT)),
        help="K2Bi vault root; default: K2BI_VAULT_ROOT or ~/Projects/K2Bi-Vault",
    )
    parser.add_argument(
        "--now-utc",
        default=None,
        help="Override current UTC time for tests, ISO 8601",
    )
    parser.add_argument(
        "--no-send",
        action="store_true",
        help="Print the heartbeat without sending Telegram",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    project_root = Path(__file__).resolve().parent.parent
    load_project_env(project_root)
    now = _parse_now(args.now_utc)
    vault_root = Path(args.vault_root).expanduser()
    result = run_heartbeat(vault_root=vault_root, now=now)
    print(result.message)
    if not args.no_send:
        try:
            send_telegram(result.message)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            print(
                f"telegram-send-failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            if result.exit_code == 0:
                return 1
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
