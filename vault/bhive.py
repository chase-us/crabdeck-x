"""bHive heartbeat protocol — minute-slot scheduling + 20s watchdog.

Slot id is floor(unix_seconds / 60). Agents include `bhive_slot` on every
HEARTBEAT. Missing more than one slot, or going silent for WATCHDOG_SECONDS,
is a fault. Gateway watchdog (20s) and bHive slot lag are independent nets.
"""

from __future__ import annotations

from dataclasses import dataclass

SLOT_SECONDS = 60
WATCHDOG_SECONDS = 20.0
ALLOWED_AGENTS = frozenset({"hermes", "openclaw", "orchestrator", "crabdeck", "vault"})


def minute_slot(ts_seconds: float) -> int:
    if not isinstance(ts_seconds, (int, float)):
        raise TypeError("ts_seconds must be a number")
    if ts_seconds < 0:
        raise ValueError("ts_seconds must be >= 0")
    return int(ts_seconds // SLOT_SECONDS)


def slot_lag(last_slot: int, now_slot: int) -> int:
    if not isinstance(last_slot, int) or not isinstance(now_slot, int):
        raise TypeError("slots must be int")
    return now_slot - last_slot


def missed_slot(last_slot: int, now_slot: int) -> bool:
    """True when the agent skipped at least one full minute slot."""
    return slot_lag(last_slot, now_slot) > 1


def missed_watchdog(last_seen_seconds: float, now_seconds: float) -> bool:
    if now_seconds < last_seen_seconds:
        return False
    return (now_seconds - last_seen_seconds) > WATCHDOG_SECONDS


def validate_agent(agent: object) -> str:
    if not isinstance(agent, str) or not agent.strip():
        raise ValueError("agent must be a non-empty string")
    name = agent.strip().lower()
    if name not in ALLOWED_AGENTS:
        raise ValueError(f"unknown agent: {agent!r}")
    return name


@dataclass(frozen=True)
class BhiveStatus:
    agent: str
    last_seen: float
    last_slot: int
    now_slot: int
    watchdog_miss: bool
    slot_miss: bool
    status: str

    def as_dict(self) -> dict[str, object]:
        return {
            "agent": self.agent,
            "last_seen": self.last_seen,
            "last_slot": self.last_slot,
            "now_slot": self.now_slot,
            "watchdog_miss": self.watchdog_miss,
            "slot_miss": self.slot_miss,
            "status": self.status,
        }


def evaluate_agent(
    agent: str,
    last_seen: float,
    last_slot: int,
    now_seconds: float,
) -> BhiveStatus:
    name = validate_agent(agent)
    now_slot = minute_slot(now_seconds)
    wd = missed_watchdog(last_seen, now_seconds)
    sl = missed_slot(last_slot, now_slot)
    if wd:
        status = "missed_heartbeat"
    elif sl:
        status = "slot_lag"
    else:
        status = "running"
    return BhiveStatus(
        agent=name,
        last_seen=last_seen,
        last_slot=last_slot,
        now_slot=now_slot,
        watchdog_miss=wd,
        slot_miss=sl,
        status=status,
    )
