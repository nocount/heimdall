from datetime import timedelta

import pytest

from heimdall.db import iso
from heimdall.watchdog import Proc, Watchdog, scan_processes

from .conftest import EVENING, FOCUS

T0 = FOCUS


def firefox(pid=100, started=T0 - timedelta(hours=1)):
    return Proc(pid, "firefox", "firefox", started)


class Rig:
    def __init__(self, config, procs=(), up=True):
        self.procs = list(procs)
        self.up = up
        self.last_heartbeat = None
        self.killed: list[int] = []
        self.dog = Watchdog(config, processes=lambda: self.procs, status=self.status,
                            kill=self.killed.extend)
        # The watchdog has watched the daemon be up for a while.
        self.dog.tick(T0 - timedelta(minutes=10))
        self.killed.clear()

    def status(self):
        return {"last_heartbeat": self.last_heartbeat} if self.up else None

    def heartbeat(self, at):
        self.last_heartbeat = iso(at)


@pytest.fixture
def rig(config):
    return lambda **kw: Rig(config, **kw)


def test_fresh_heartbeat_leaves_firefox_alone(rig):
    r = rig(procs=[firefox()])
    r.heartbeat(T0 - timedelta(seconds=90))  # one missed minute is fine
    assert r.dog.tick(T0) == []
    assert r.killed == []


def test_silent_extension_kills_firefox(rig):
    r = rig(procs=[firefox(100), Proc(101, "Web Content", "firefox", T0 - timedelta(hours=1))])
    r.heartbeat(T0 - timedelta(seconds=151))
    actions = r.dog.tick(T0)
    assert r.killed == [100]  # the main process only; children go with it
    assert "heimdall extension silent" in actions[0]


def test_never_any_heartbeat_kills_firefox(rig):
    r = rig(procs=[firefox()])
    r.dog.tick(T0)
    assert r.killed == [100]


def test_startup_grace(rig):
    r = rig(procs=[firefox(started=T0 - timedelta(seconds=30))])
    r.dog.tick(T0)
    assert r.killed == []
    r.dog.tick(T0 + timedelta(seconds=61))
    assert r.killed == [100]


def test_newest_firefox_gets_the_grace(rig):
    # A second instance (another profile) just started: wait for it too.
    r = rig(procs=[firefox(100), firefox(200, started=T0 - timedelta(seconds=10))])
    r.dog.tick(T0)
    assert r.killed == []


def test_daemon_down_spares_firefox(rig):
    r = rig(procs=[firefox()], up=False)
    r.dog.tick(T0)
    assert r.killed == []


def test_daemon_just_back_waits_for_a_heartbeat_cycle(rig):
    r = rig(procs=[firefox()])
    r.up = False
    r.dog.tick(T0)
    r.up = True
    r.dog.tick(T0 + timedelta(seconds=15))  # back up; heartbeats stale from the outage
    r.dog.tick(T0 + timedelta(seconds=150))
    assert r.killed == []
    r.dog.tick(T0 + timedelta(seconds=166))  # a full timeout since it came back, still silent
    assert r.killed == [100]


def test_other_browsers_killed_by_exact_name(rig):
    r = rig(procs=[
        Proc(300, "chrome", "chrome", T0), Proc(301, "brave", "brave", T0),
        Proc(302, "Isolated Web Co", "vivaldi-bin", T0),  # matched by executable
        Proc(303, "code", "code", T0), Proc(304, "chrome_crashpad", "chrome_crashpad_handler", T0),
        Proc(305, "claude-desktop", "electron", T0),
    ])
    r.dog.tick(T0)
    assert sorted(r.killed) == [300, 301, 302]


def test_nothing_outside_focus_hours(rig):
    r = rig(procs=[firefox(), Proc(300, "chrome", "chrome", EVENING)])
    r.dog.tick(EVENING)
    assert r.killed == []


def test_scan_processes_sees_this_process():
    import os
    from datetime import datetime, timezone

    me = [p for p in scan_processes() if p.pid == os.getpid()]
    assert me and me[0].exe.startswith("python")
    assert timedelta(0) <= datetime.now(timezone.utc) - me[0].started < timedelta(minutes=5)
