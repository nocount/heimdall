"""The root watchdog (PLAN.md 5.3): during focus hours, Firefox must be
running heimdall, and other browsers aren't allowed.

Every `poll_s`:
- Kill processes whose name is on `kill_browsers` (exact match on the
  process name or executable basename).
- If Firefox is running, has been up longer than `startup_grace_s`, the
  daemon is up and has been for at least `heartbeat_timeout_s`, and the
  extension's last heartbeat is older than `heartbeat_timeout_s`: kill
  Firefox. That covers safe mode, a profile without the extension, and a
  disabled or crashed extension. Session restore brings the tabs back.

If the daemon is down, Firefox is left alone: the extension already blocks
pages when it can't reach the daemon, and systemd restarts the daemon.

Heartbeats are read from the daemon's /v1/status, never from its database:
a root process opening the SQLite file could leave root-owned WAL files the
daemon (user heimdall) can't write.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

import httpx

from .config import Config
from .schedule import Schedule

log = logging.getLogger("heimdall.watchdog")

PROC = Path("/proc")
TERM_GRACE_S = 5  # SIGTERM, then SIGKILL whatever is left


@dataclass(frozen=True)
class Proc:
    pid: int
    name: str  # /proc/<pid>/comm (truncated to 15 chars by the kernel)
    exe: str  # basename of /proc/<pid>/exe, "" if unreadable
    started: datetime


def _boot_time() -> float:
    for line in (PROC / "stat").read_text().splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    raise RuntimeError("no btime in /proc/stat")


def scan_processes() -> list[Proc]:
    boot = _boot_time()
    hz = os.sysconf("SC_CLK_TCK")
    procs = []
    for d in PROC.iterdir():
        if not d.name.isdigit():
            continue
        try:
            name = (d / "comm").read_text().strip()
            # Field 22 (starttime) comes after the parenthesised comm, which may contain spaces.
            stat = (d / "stat").read_text()
            starttime = int(stat[stat.rindex(")") + 2:].split()[19])
            try:
                exe = os.path.basename(os.readlink(d / "exe"))
            except OSError:
                exe = ""
        except (OSError, ValueError, IndexError):
            continue  # exited mid-scan, or a kernel thread
        started = datetime.fromtimestamp(boot + starttime / hz, timezone.utc)
        procs.append(Proc(int(d.name), name, exe, started))
    return procs


def daemon_status(port: int = 7878) -> dict | None:
    """The daemon's /v1/status, or None if it's down."""
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/v1/status", timeout=2)
        return resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def terminate(pids: Iterable[int]) -> None:
    pids = list(pids)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + TERM_GRACE_S
    while pids and time.monotonic() < deadline:
        time.sleep(0.25)
        pids = [p for p in pids if PROC.joinpath(str(p)).exists()]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class Watchdog:
    def __init__(self, config: Config, *,
                 processes: Callable[[], list[Proc]] = scan_processes,
                 status: Callable[[], dict | None] = daemon_status,
                 kill: Callable[[Iterable[int]], None] = terminate):
        self.cfg = config.watchdog
        self.schedule = Schedule(config)
        self.processes = processes
        self.status = status
        self.kill = kill
        self.daemon_up_since: datetime | None = None

    def tick(self, now: datetime) -> list[str]:
        """One pass. Returns what it did, for logs and tests."""
        status = self.status()
        up = status is not None
        if not up:
            self.daemon_up_since = None
        elif self.daemon_up_since is None:
            self.daemon_up_since = now

        if not self.schedule.is_focus(now):
            return []

        actions = []
        procs = self.processes()
        banned = set(self.cfg.kill_browsers)
        others = [p for p in procs if p.name in banned or p.exe in banned]
        if others:
            self.kill(p.pid for p in others)
            actions.append("killed " + ", ".join(sorted({f"{p.name}[{p.pid}]" for p in others})))

        firefox = [p for p in procs if p.name == "firefox"]
        if not firefox:
            return actions
        newest = max(p.started for p in firefox)
        timeout = timedelta(seconds=self.cfg.heartbeat_timeout_s)
        if now - newest < timedelta(seconds=self.cfg.startup_grace_s):
            return actions  # still starting; give the extension time to boot
        if not up or now - self.daemon_up_since < timeout:
            return actions  # can't tell a dead extension from a daemon that was down
        last = status.get("last_heartbeat")
        last_seen = datetime.fromisoformat(last) if last else None
        if last_seen is None or now - last_seen > timeout:
            self.kill(p.pid for p in firefox)
            seen = f"last heartbeat {last}" if last else "no heartbeat ever"
            actions.append(f"killed firefox {[p.pid for p in firefox]}: heimdall extension silent ({seen})")
        return actions

    def run(self) -> None:
        log.info("watchdog running: poll %ss, heartbeat timeout %ss, startup grace %ss, banned %s",
                 self.cfg.poll_s, self.cfg.heartbeat_timeout_s, self.cfg.startup_grace_s, self.cfg.kill_browsers)
        while True:
            try:
                for action in self.tick(datetime.now(timezone.utc)):
                    log.warning("%s", action)
            except Exception:
                log.exception("watchdog tick failed")
            time.sleep(self.cfg.poll_s)
