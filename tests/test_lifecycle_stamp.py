"""Unit tests for the run-path config stamp (T8, re-keyed in T17).

The rest of the run path (instance CoW/start, last-used bump, exec claude, the
auto-setup trigger) is I/O against the incus daemon and is verified by a
throwaway integration run, like T3/T4/T5. These cover the pure stamp fingerprint
+ the local read/write round-trip that drive the §15.2 "exactly one auto-setup".

T17 re-keyed ``_config_stamp`` from ``hash(SCHEMA_VERSION + config.toml bytes)``
to a hash of the config's *build identity* (``_base_build_id`` + each context's
``_template_build_id``, the same functions T12 uses for instance recreation).
So the stamp is now **stable** across runtime-only edits (``[env]``/``[reaper]``)
and **drifts** on build-relevant edits incl. provision-script *content* changes
(DESIGN §7.3/§10/§15.13).
"""

from claude_wrapper import lifecycle
from claude_wrapper.config import (
    Config,
    Context,
    MountSpec,
    ReaperConfig,
    SetupConfig,
)


def _cfg(*, packages=("jq",), provision=None, mounts=(), contexts=(),
         env=None, forward=(), reaper=None):
    return Config(
        setup=SetupConfig(packages=tuple(packages), provision_script=provision),
        reaper=reaper or ReaperConfig(),
        mounts=tuple(mounts),
        contexts=tuple(contexts),
        env=env or {},
        forward=tuple(forward),
    )


# --- determinism + schema coverage -------------------------------------------


def test_config_stamp_stable_for_same_config():
    cfg = _cfg(packages=("jq", "emacs"))
    assert lifecycle._config_stamp(cfg) == lifecycle._config_stamp(cfg)


def test_config_stamp_changes_with_schema_version(monkeypatch):
    # SCHEMA_VERSION is folded in via _base_build_id, so a bump still drifts.
    cfg = _cfg()
    before = lifecycle._config_stamp(cfg)
    monkeypatch.setattr(lifecycle, "SCHEMA_VERSION", lifecycle.SCHEMA_VERSION + 1)
    assert lifecycle._config_stamp(cfg) != before


# --- T17: STABLE across runtime-only edits ([env] / [reaper]) ----------------


def test_config_stamp_stable_across_env_edit():
    a = _cfg(env={"FOO": "1"}, forward=("BAR",))
    b = _cfg(env={"FOO": "2", "BAZ": "x"}, forward=())
    # Only env/forward differ — neither is in any build-id → same stamp.
    assert lifecycle._config_stamp(a) == lifecycle._config_stamp(b)


def test_config_stamp_stable_across_reaper_edit():
    a = _cfg(reaper=ReaperConfig(stop_idle_after=1800))
    b = _cfg(reaper=ReaperConfig(stop_idle_after=60, max_instances=5))
    assert lifecycle._config_stamp(a) == lifecycle._config_stamp(b)


def test_config_stamp_stable_across_context_env_edit():
    ctx_a = Context(name="api", when=("/x",), env={"DEPLOY": "a"})
    ctx_b = Context(name="api", when=("/x",), env={"DEPLOY": "b"}, forward=("TOKEN",))
    assert lifecycle._config_stamp(_cfg(contexts=(ctx_a,))) == lifecycle._config_stamp(
        _cfg(contexts=(ctx_b,))
    )


# --- T17: DRIFTS on build-relevant edits -------------------------------------


def test_config_stamp_drifts_on_packages():
    a = lifecycle._config_stamp(_cfg(packages=("jq",)))
    b = lifecycle._config_stamp(_cfg(packages=("jq", "git")))
    assert a != b


def test_config_stamp_drifts_on_global_mount_field():
    a = lifecycle._config_stamp(_cfg(mounts=(MountSpec(path="/a", mode="rw"),)))
    b = lifecycle._config_stamp(_cfg(mounts=(MountSpec(path="/a", mode="ro"),)))
    assert a != b


def test_config_stamp_drifts_on_context_add_remove():
    ctx = Context(name="api", when=("/x",), mounts=(MountSpec(path="/p"),))
    a = lifecycle._config_stamp(_cfg(contexts=()))
    b = lifecycle._config_stamp(_cfg(contexts=(ctx,)))
    assert a != b


def test_config_stamp_drifts_on_context_mount_change():
    a = lifecycle._config_stamp(
        _cfg(contexts=(Context(name="api", when=("/x",), mounts=(MountSpec(path="/p"),)),))
    )
    b = lifecycle._config_stamp(
        _cfg(contexts=(Context(
            name="api", when=("/x",),
            mounts=(MountSpec(path="/p"), MountSpec(path="/q")),
        ),))
    )
    assert a != b


# --- T17: DRIFTS on provision-script *content* change (config shape same) ----


def test_config_stamp_drifts_on_global_provision_content(tmp_path):
    p = tmp_path / "provision.sh"
    p.write_text("echo one\n")
    before = lifecycle._config_stamp(_cfg(provision=str(p)))
    p.write_text("echo two\n")  # same path/shape, different bytes
    after = lifecycle._config_stamp(_cfg(provision=str(p)))
    assert before != after


def test_config_stamp_drifts_on_context_provision_content(tmp_path):
    p = tmp_path / "provision-api.sh"
    p.write_text("setup-a\n")
    ctx = Context(name="api", when=("/x",), provision_script=str(p))
    before = lifecycle._config_stamp(_cfg(contexts=(ctx,)))
    p.write_text("setup-b\n")
    after = lifecycle._config_stamp(_cfg(contexts=(ctx,)))
    assert before != after


# --- local stamp read/write round-trip + the drift gate ----------------------


def test_stamp_read_write_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert lifecycle._read_stamp() is None  # absent → None, never raises
    lifecycle._write_stamp("deadbeef")
    assert lifecycle._read_stamp() == "deadbeef"


def test_stamp_drift_cycle(tmp_path, monkeypatch):
    """Edit-config → mismatch → (re)write → match: the §15.2 one-setup gate."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    cfg = _cfg(packages=("jq",))

    # First run: no stamp yet → mismatch (would trigger setup).
    assert lifecycle._read_stamp() != lifecycle._config_stamp(cfg)
    lifecycle._write_stamp(lifecycle._config_stamp(cfg))
    # Second run, config unchanged → match (fast path, no setup).
    assert lifecycle._read_stamp() == lifecycle._config_stamp(cfg)

    # Build-relevant edit (a package) → mismatch again.
    cfg = _cfg(packages=("jq", "emacs"))
    assert lifecycle._read_stamp() != lifecycle._config_stamp(cfg)
    lifecycle._write_stamp(lifecycle._config_stamp(cfg))
    assert lifecycle._read_stamp() == lifecycle._config_stamp(cfg)

    # Runtime-only edit ([env]) → still a match (no spurious setup).
    cfg = _cfg(packages=("jq", "emacs"), env={"FOO": "bar"})
    assert lifecycle._read_stamp() == lifecycle._config_stamp(cfg)
