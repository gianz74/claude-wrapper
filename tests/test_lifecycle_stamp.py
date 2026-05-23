"""Unit tests for the run-path stamp logic in lifecycle (T8).

The rest of T8 (instance CoW/start, last-used bump, exec claude, the auto-setup
trigger) is I/O against the incus daemon and is verified by a throwaway
integration run, like T3/T4/T5. These cover the pure stamp fingerprint + the
local read/write round-trip that drive the §15.2 "exactly one auto-setup".
"""

from claude_wrapper import lifecycle


def _write_config(tmp_path, body: str):
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


def test_config_stamp_stable_for_same_content(tmp_path):
    p = _write_config(tmp_path, "[setup]\npackages = ['jq']\n")
    assert lifecycle._config_stamp(p) == lifecycle._config_stamp(p)


def test_config_stamp_changes_with_content(tmp_path):
    a = tmp_path / "a.toml"
    a.write_text("packages = ['jq']\n")
    b = tmp_path / "b.toml"
    b.write_text("packages = ['jq', 'emacs']\n")
    assert lifecycle._config_stamp(a) != lifecycle._config_stamp(b)


def test_config_stamp_changes_with_schema_version(tmp_path, monkeypatch):
    p = _write_config(tmp_path, "packages = ['jq']\n")
    before = lifecycle._config_stamp(p)
    monkeypatch.setattr(lifecycle, "SCHEMA_VERSION", lifecycle.SCHEMA_VERSION + 1)
    assert lifecycle._config_stamp(p) != before


def test_stamp_read_write_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert lifecycle._read_stamp() is None  # absent → None, never raises
    lifecycle._write_stamp("deadbeef")
    assert lifecycle._read_stamp() == "deadbeef"


def test_stamp_drift_cycle(tmp_path, monkeypatch):
    """Edit-config → mismatch → (re)write → match: the §15.2 one-setup gate."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    cfg = _write_config(tmp_path, "packages = ['jq']\n")

    # First run: no stamp yet → mismatch (would trigger setup).
    assert lifecycle._read_stamp() != lifecycle._config_stamp(cfg)
    lifecycle._write_stamp(lifecycle._config_stamp(cfg))
    # Second run, config unchanged → match (fast path, no setup).
    assert lifecycle._read_stamp() == lifecycle._config_stamp(cfg)

    # Edit config → mismatch again.
    cfg.write_text("packages = ['jq', 'emacs']\n")
    assert lifecycle._read_stamp() != lifecycle._config_stamp(cfg)
    lifecycle._write_stamp(lifecycle._config_stamp(cfg))
    assert lifecycle._read_stamp() == lifecycle._config_stamp(cfg)
