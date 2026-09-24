"""The `doctor` watcher: reports only newly-appeared defects since the last
run, and records that a run happened at all -- a watcher indistinguishable
from silence is the failure it exists to prevent.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from .checks import Finding
from .checks.structural import check_all
from .checks.staleness import check_staleness
from .checks.expect import check_expectations
from .ledger.store import LedgerStore
from .store import Store
from .clock import Clock

# Errors a corrupt, truncated, or hand-edited state file can raise while
# being parsed and turned back into Findings. Caught explicitly (never a
# bare `except`) so a broken state file degrades to "no previous state"
# instead of taking the watcher down on the next cron run.
_STATE_ERRORS = (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError,
                  TypeError, ValueError, AttributeError)


@dataclass
class DoctorResult:
    new: list[Finding] = field(default_factory=list)
    resolved: list[Finding] = field(default_factory=list)
    total: int = 0
    # The `last_run` recorded in state BEFORE this run overwrote it, so a
    # caller can report "checked, all clear" apart from "hasn't run since
    # Tuesday". None on a genuine first run (or after a reset).
    previous_run: str | None = None
    # Set (and human-readable) only when state_path existed but could not
    # be parsed as prior state. None on every normal run.
    state_reset: str | None = None


def _key(f: Finding) -> str:
    # `detail` can itself contain "|" (e.g. "HTTP 404 | https://x"). Joining
    # on "|" is still safe to decode because `_load_previous` splits with
    # maxsplit=2: the first two "|" delimit kind and file, and everything
    # after -- pipes included -- is kept intact as detail.
    return f"{f.kind}|{f.file}|{f.detail}"


def _load_previous(state_path: Path) -> tuple[dict[str, Finding], str | None, str | None]:
    """Load prior state. Returns (findings-by-key, last_run, reset_message).

    reset_message is None on a clean load (including "no state file yet").
    It is set, and the other two returned as empty/None, whenever the file
    exists but cannot be trusted -- this is the explicit handling for a
    corrupt/truncated/hand-edited state file: never a silent bare `except`.
    """
    if not state_path.exists():
        return {}, None, None
    try:
        data = json.loads(state_path.read_text())
        keys = data["findings"]
        # `Finding(*parts)` raises TypeError if a hand-edited key is missing
        # a "|" (too few parts) -- caught below like any other malformed-state error.
        previous = {k: Finding(*k.split("|", 2)) for k in keys}
        last_run = data.get("last_run")
        if last_run is not None and not isinstance(last_run, str):
            raise ValueError("last_run is not a string")
        return previous, last_run, None
    except _STATE_ERRORS as exc:
        return {}, None, (
            f"state file {state_path} was unreadable or malformed "
            f"({exc.__class__.__name__}: {exc}); treating as no previous state and resetting it"
        )


def run_doctor(cfg, client, state_path: Path, today: date,
               axes: tuple = (), scale_max: int = 5) -> DoctorResult:
    findings = check_all(cfg.store_path, cfg.accepted_absences, axes)
    findings += check_staleness(Store(cfg.store_path), client, Clock(today, cfg.stale_days))
    # Expectations: what a node says should be observably true, probed
    # here on the schedule `doctor` already runs on. Unmet ones are
    # findings like any other, tracked run over run, so the first time
    # an expectation fails it is NEW and says so.
    findings += check_expectations(LedgerStore(cfg.store_path), client, today,
                                   axes=axes, scale_max=scale_max)
    seen = {_key(f): f for f in findings}

    previous, last_run, reset_msg = _load_previous(state_path)

    res = DoctorResult(
        new=[f for k, f in seen.items() if k not in previous],
        resolved=[f for k, f in previous.items() if k not in seen],
        total=len(seen),
        previous_run=last_run,
        state_reset=reset_msg,
    )

    # Written on every run, including a clean one -- last_run must always
    # advance so a human (or the next run) can tell "checked, all clear"
    # apart from "hasn't run since Tuesday".
    state_path.write_text(json.dumps(
        {"last_run": today.isoformat(), "findings": sorted(seen)}, indent=2))
    return res
