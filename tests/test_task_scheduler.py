"""Tests for cortexclaw.task_scheduler — compute_next_run logic."""

from __future__ import annotations

from datetime import datetime, timezone

from cortexclaw.task_scheduler import compute_next_run
from cortexclaw.types import ScheduledTask


def _make_task(**overrides) -> ScheduledTask:
    defaults = dict(
        id="t1",
        group_folder="grp",
        chat_jid="jid1",
        prompt="do work",
        schedule_type="interval",
        schedule_value="60000",
        context_mode="isolated",
        next_run=None,
        status="active",
        created_at="2024-01-01T00:00:00",
    )
    defaults.update(overrides)
    return ScheduledTask(**defaults)


# ---------------------------------------------------------------------------
# Once tasks
# ---------------------------------------------------------------------------


class TestComputeNextRunOnce:
    def test_once_returns_none(self):
        task = _make_task(schedule_type="once", schedule_value="2024-01-15T12:00:00")
        assert compute_next_run(task) is None


# ---------------------------------------------------------------------------
# Interval tasks
# ---------------------------------------------------------------------------


class TestComputeNextRunInterval:
    def test_interval_without_anchor(self):
        task = _make_task(schedule_type="interval", schedule_value="60000")
        result = compute_next_run(task)
        assert result is not None
        # Should be ~60s from now
        next_dt = datetime.fromisoformat(result)
        now = datetime.now(timezone.utc)
        diff = (next_dt - now).total_seconds()
        assert 55 < diff < 65

    def test_interval_with_anchor(self):
        # Anchor is in the past — next_run should be bumped forward
        past = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc).isoformat()
        task = _make_task(
            schedule_type="interval",
            schedule_value="60000",  # 60s
            next_run=past,
        )
        result = compute_next_run(task)
        assert result is not None
        next_dt = datetime.fromisoformat(result)
        now = datetime.now(timezone.utc)
        # Should be in the future
        assert next_dt > now

    def test_interval_prevents_drift(self):
        """Anchoring to next_run instead of now prevents accumulated drift."""
        now = datetime.now(timezone.utc)
        # Set anchor to 1ms ago — next should be anchor + interval, not now + interval
        anchor_ts = now.timestamp() - 0.001
        anchor = datetime.fromtimestamp(anchor_ts, tz=timezone.utc).isoformat()
        task = _make_task(
            schedule_type="interval",
            schedule_value="60000",
            next_run=anchor,
        )
        result = compute_next_run(task)
        assert result is not None
        next_dt = datetime.fromisoformat(result)
        expected_approx = anchor_ts + 60.0
        actual = next_dt.timestamp()
        assert abs(actual - expected_approx) < 1.0

    def test_invalid_interval_fallback(self):
        task = _make_task(schedule_type="interval", schedule_value="0")
        result = compute_next_run(task)
        # Should fall back to ~60s
        assert result is not None


# ---------------------------------------------------------------------------
# Cron tasks
# ---------------------------------------------------------------------------


class TestComputeNextRunCron:
    def test_cron_returns_future(self):
        task = _make_task(
            schedule_type="cron",
            schedule_value="*/5 * * * *",  # every 5 minutes
        )
        result = compute_next_run(task)
        assert result is not None
        next_dt = datetime.fromisoformat(result)
        now = datetime.now(timezone.utc)
        assert next_dt > now

    def test_cron_daily(self):
        task = _make_task(
            schedule_type="cron",
            schedule_value="0 9 * * *",  # daily at 9am
        )
        result = compute_next_run(task)
        assert result is not None


# ---------------------------------------------------------------------------
# Unknown schedule type
# ---------------------------------------------------------------------------


class TestComputeNextRunUnknown:
    def test_unknown_returns_none(self):
        task = _make_task(schedule_type="bogus", schedule_value="???")
        assert compute_next_run(task) is None
