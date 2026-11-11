"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="completed", run_id=1, error=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Each rule — positive + negative + clearing
# ---------------------------------------------------------------------------
















def test_running_with_open_parents_fires_only_while_running():
    """A running card whose parent is not terminal is flagged; the same graph
    on a ready/todo card (the gate is holding it) and a done parent are not."""
    graph = {"parents": [{"id": "t_parent", "title": "p", "status": "todo"}], "children": []}
    diags = kd.compute_task_diagnostics(_task(status="running", started_at=100), [], [], graph=graph)
    assert [d.kind for d in diags] == ["running_with_open_parents"]
    assert diags[0].data["open_parents"] == [{"id": "t_parent", "status": "todo"}]
    assert "hermes kanban unlink t_parent t_demo00" in diags[0].actions[0].payload["command"]
    assert kd.compute_task_diagnostics(_task(status="todo"), [], [], graph=graph) == []
    done_graph = {"parents": [{"id": "t_parent", "title": "p", "status": "done"}], "children": []}
    assert kd.compute_task_diagnostics(_task(status="running"), [], [], graph=done_graph) == []


def test_stuck_in_blocked_fires_past_threshold():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48, reason="needs approval"),
    ]
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
    )
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "stuck_in_blocked"
    assert d.severity == "warning"
    assert d.data["age_hours"] >= 48


def test_stuck_in_blocked_rearms_after_a_stale_comment():
    """A comment re-arms the nag window; it does not discharge the block forever.

    Blocked 30 h ago with its newest comment 29 h ago: the engagement itself is
    older than ``blocked_stale_hours`` (24), so the card is stale again and must
    be reported. Pre-fix any comment ever silenced the rule permanently.
    """
    now = 1_800_000_000
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 30, reason="waiting on a human"),
        _event("commented", ts=now - 3600 * 29, body="noted, still waiting"),
    ]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d.kind for d in diags] == ["stuck_in_blocked"]
    d = diags[0]
    assert d.data["age_hours"] == pytest.approx(29, abs=0.1)  # aged from the comment
    assert d.data["comment_at"] == now - 3600 * 29
    assert "did not resolve" in d.detail


def test_stuck_in_blocked_silent_after_a_recent_comment():
    """No nag right after a human engaged: a comment 2 h old keeps the card quiet."""
    now = 1_800_000_000
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 30, reason="waiting on a human"),
        _event("commented", ts=now - 3600 * 2, body="on it"),
    ]
    assert kd.compute_task_diagnostics(task, events, [], now=now) == []


def test_stuck_in_blocked_silent_after_an_unblock():
    """``unblocked`` is a real discharge, not an engagement beat: once it lands
    after the block it suppresses the rule regardless of its age."""
    now = 1_800_000_000
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 30, reason="waiting on a human"),
        _event("unblocked", ts=now - 3600 * 29),
    ]
    assert kd.compute_task_diagnostics(task, events, [], now=now) == []


def test_stuck_in_blocked_reports_a_parks_with_only_an_old_comment(kanban_home):
    """Board-shaped repro: a calendar wait parked ``blocked`` with one early
    evidence note stayed invisible to ``hermes kanban diagnostics --task <id>``
    for 8 days.

    Real history: created ``initial_status=blocked`` at 2026-11-02T12:00:30Z
    (``create_task`` writes the ``blocked`` event), one comment 5m53s later
    saying "wait for 2026-11-09", status still ``blocked`` 8 days on. The
    ``--task`` path is ``compute_task_diagnostics(get_task, list_events,
    list_runs, graph)``; before this fix it returned no diagnostics for it.
    """
    blocked_at = 1_793_642_430     # 2026-11-02T12:00:30Z
    commented_at = 1_793_642_783   # +5m53s
    now = blocked_at + 8 * 24 * 3600

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="calendar wait", assignee="default", created_by="default",
            initial_status="blocked",
        )
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "commented", {"author": "default", "len": 1432})
            conn.execute(
                "UPDATE task_events SET created_at = ? WHERE task_id = ? "
                "AND kind IN ('created', 'blocked')", (blocked_at, tid),
            )
            conn.execute(
                "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'commented'",
                (commented_at, tid),
            )
        # The CLI's --task path: task row + events + runs + graph context.
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        runs = kb.list_runs(conn, tid)
        graph = kb.task_graph_context(conn, tid)

    diags = kd.compute_task_diagnostics(task, events, runs, graph=graph, now=now)
    assert [d.kind for d in diags] == ["stuck_in_blocked"]
    d = diags[0]
    assert d.data["blocked_at"] == blocked_at
    assert d.data["comment_at"] == commented_at
    assert d.data["blocked_hours"] == pytest.approx(192, abs=0.1)








# ---------------------------------------------------------------------------
# Severity sorting
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Integration — runs through real kanban_db so sqlite.Row fields work
# ---------------------------------------------------------------------------


def test_engine_works_on_sqlite_row_objects(kanban_home):
    """Regression: the rule functions must handle sqlite3.Row (which
    supports mapping access but not attribute access and isn't a dict)
    as well as dataclass Task / plain dict. The API layer passes Row
    objects directly.
    """
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        real = kb.create_task(conn, title="r", assignee="x", created_by="w")
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="with phantom", created_cards=[real, "t_deadbeef1"],
            )
        # Pull Row objects the way the API helper does.
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        diags = kd.compute_task_diagnostics(row, events, runs)
        assert len(diags) == 1
        assert diags[0].kind == "hallucinated_cards"
        assert "t_deadbeef1" in diags[0].data["phantom_ids"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-tolerance: a broken rule shouldn't 500 the whole compute call
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# stranded_in_ready
#
# Surfaces ready tasks that nobody has claimed within the threshold.
# Identity-agnostic by design: catches typo'd assignees, deleted profiles,
# down external worker pools, and misconfigured dispatchers in one rule.
# ---------------------------------------------------------------------------


def test_stranded_in_ready_fires_when_age_exceeds_threshold():
    """Default threshold = 30 min. A ready task promoted 45 min ago
    with no claim should fire as a warning."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    # 45 min = 2700s, threshold = 1800s.
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"
    assert stranded[0].data["age_seconds"] == 45 * 60
    assert stranded[0].data["assignee"] == "demo"




# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")








def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True
