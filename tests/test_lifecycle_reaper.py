"""Unit tests for the pure reaper decision logic in lifecycle (T10).

The execution side of the reaper (the liveness guard, ``incus stop/delete``, the
detached background spawn, ``gc``/``delete`` subcommands) is I/O against the
incus daemon and is verified by a throwaway integration run, like T3/T4/T5/T8.
These cover the daemon-free meat: ``plan_reap`` (the stop/delete/LRU decision),
``_last_used_epoch``, the amortized-reap stamp gate, and the result summary.
"""

from claude_wrapper import lifecycle
from claude_wrapper.config import ReaperConfig

NOW = 1_700_000_000  # a realistic epoch, so an epoch-0 (missing) tag reads as ancient


def _inst(name, *, last_used=None, status="Running"):
    config = {} if last_used is None else {lifecycle.LAST_USED_KEY: str(last_used)}
    return {"name": name, "status": status, "config": config}


def _reaper(*, stop=30 * 60, delete=14 * 86400, max_instances=0):
    return ReaperConfig(
        stop_idle_after=stop, delete_unused_after=delete, max_instances=max_instances
    )


# --- _last_used_epoch --------------------------------------------------------


def test_last_used_epoch_reads_tag():
    assert lifecycle._last_used_epoch(_inst("a", last_used=123)) == 123


def test_last_used_epoch_missing_is_zero():
    assert lifecycle._last_used_epoch(_inst("a")) == 0


def test_last_used_epoch_garbage_is_zero():
    assert lifecycle._last_used_epoch({"config": {lifecycle.LAST_USED_KEY: "nope"}}) == 0


# --- plan_reap: nothing to do ------------------------------------------------


def test_plan_all_fresh_is_empty():
    fresh = [_inst("a", last_used=NOW), _inst("b", last_used=NOW)]
    plan = lifecycle.plan_reap(fresh, _reaper(), NOW)
    assert plan.stop == () and plan.delete == ()


# --- plan_reap: stop idle ----------------------------------------------------


def test_plan_stops_running_idle_past_threshold():
    insts = [_inst("a", last_used=NOW - 31 * 60)]  # idle 31m > 30m
    plan = lifecycle.plan_reap(insts, _reaper(), NOW)
    assert plan.stop == ("a",) and plan.delete == ()


def test_plan_does_not_stop_stopped_instance():
    insts = [_inst("a", last_used=NOW - 31 * 60, status="Stopped")]
    plan = lifecycle.plan_reap(insts, _reaper(), NOW)
    assert plan.stop == () and plan.delete == ()


def test_plan_stop_threshold_zero_disables_stop():
    insts = [_inst("a", last_used=NOW - 10 * 86400)]
    plan = lifecycle.plan_reap(insts, _reaper(stop=0), NOW)
    assert plan.stop == ()


# --- plan_reap: delete unused ------------------------------------------------


def test_plan_deletes_unused_past_threshold_not_stopped():
    insts = [_inst("a", last_used=NOW - 15 * 86400)]  # unused 15d > 14d
    plan = lifecycle.plan_reap(insts, _reaper(), NOW)
    assert plan.delete == ("a",) and plan.stop == ()


def test_plan_delete_threshold_zero_disables_delete():
    insts = [_inst("a", last_used=NOW - 100 * 86400)]
    plan = lifecycle.plan_reap(insts, _reaper(delete=0), NOW)
    assert plan.delete == ()
    # ...but it is still old enough to be stopped (running, idle).
    assert plan.stop == ("a",)


def test_plan_missing_last_used_is_deleted_as_orphan():
    insts = [_inst("a")]  # no last-used -> epoch 0 -> ancient
    plan = lifecycle.plan_reap(insts, _reaper(), NOW)
    assert plan.delete == ("a",)


# --- plan_reap: LRU trim -----------------------------------------------------


def test_plan_trims_oldest_beyond_max_instances():
    insts = [
        _inst("new", last_used=NOW),
        _inst("mid", last_used=NOW - 100),
        _inst("old", last_used=NOW - 1000),
    ]
    plan = lifecycle.plan_reap(insts, _reaper(max_instances=2), NOW)
    assert plan.delete == ("old",)  # only the single oldest, down to the cap
    assert plan.stop == ()


def test_plan_max_instances_zero_is_unlimited():
    insts = [_inst(f"i{n}", last_used=NOW - n) for n in range(5)]
    plan = lifecycle.plan_reap(insts, _reaper(max_instances=0), NOW)
    assert plan.delete == ()


def test_plan_trim_deletes_multiple_to_reach_cap():
    insts = [_inst(f"i{n}", last_used=NOW - n) for n in range(4)]  # i3 oldest
    plan = lifecycle.plan_reap(insts, _reaper(max_instances=1), NOW)
    # keep only the newest (i0); delete the three oldest, oldest-first.
    assert set(plan.delete) == {"i1", "i2", "i3"}


# --- plan_reap: delete wins over stop ----------------------------------------


def test_plan_delete_wins_when_both_eligible():
    # Both running + idle past stop_idle_after (stop candidates); max=1 trims the
    # older one, which must move to delete and drop out of stop.
    insts = [
        _inst("newer", last_used=NOW - 31 * 60),
        _inst("older", last_used=NOW - 60 * 60),
    ]
    plan = lifecycle.plan_reap(insts, _reaper(max_instances=1), NOW)
    assert plan.delete == ("older",)
    assert plan.stop == ("newer",)


# --- reap-due stamp gate -----------------------------------------------------


def test_reap_due_when_no_stamp(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert lifecycle._reap_due(NOW) is True


def test_reap_not_due_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    lifecycle._write_reap_stamp(NOW - 10)
    assert lifecycle._reap_due(NOW) is False


def test_reap_due_when_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    lifecycle._write_reap_stamp(NOW - lifecycle.REAP_INTERVAL_S - 1)
    assert lifecycle._reap_due(NOW) is True


def test_reap_stamp_roundtrip_and_garbage(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert lifecycle._read_reap_stamp() is None
    lifecycle._write_reap_stamp(12345)
    assert lifecycle._read_reap_stamp() == 12345
    lifecycle._reap_stamp_path().write_text("garbage\n")
    assert lifecycle._read_reap_stamp() is None


# --- result summary ----------------------------------------------------------


def test_summary_suffix_empty():
    assert lifecycle.ReapResult().summary_suffix() == ""


def test_summary_suffix_reports_counts():
    r = lifecycle.ReapResult(stopped=("a", "b"), deleted=("c",))
    assert r.summary_suffix() == "; reaper stopped 2, deleted 1"
