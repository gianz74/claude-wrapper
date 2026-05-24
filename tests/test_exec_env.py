"""Unit tests for user-declared env merge + build-id insensitivity (T16, §7.3).

The merge happens in :func:`lifecycle._exec_env`, which reads ``os.environ`` for
the built-in forwarded baseline and for user ``forward`` names. These tests
``monkeypatch`` specific host vars and assert the documented precedence
(broadest→narrowest, later wins): identity → built-in baseline → user ``forward``
→ user literals (global, then context) → identity re-asserted. They also pin that
env is *never* part of the §4/§10 build identity, so an env edit cannot recreate
an instance.
"""

from claude_wrapper import lifecycle
from claude_wrapper.config import Config, Context, SetupConfig


def _ctx(name="c", **kw):
    return Context(name=name, when=("/x",), **kw)


# --- identity is always set + re-asserted ------------------------------------


def test_identity_always_set(monkeypatch):
    monkeypatch.setenv("HOME", "/wrong")
    monkeypatch.setenv("USER", "wrong")
    env = lifecycle._exec_env(Config(), None, "alice", "/home/alice")
    assert env["HOME"] == "/home/alice"
    assert env["USER"] == "alice"
    assert env["PATH"].startswith(lifecycle.LAUNCHER_DIR + ":")


# --- the two mechanisms reach the sandbox ------------------------------------


def test_global_literal_and_forward(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret")
    cfg = Config(env={"EDITOR": "vim"}, forward=("GH_TOKEN",))
    env = lifecycle._exec_env(cfg, None, "u", "/home/u")
    assert env["EDITOR"] == "vim"
    assert env["GH_TOKEN"] == "secret"


def test_builtin_baseline_forwarded(monkeypatch):
    # TERM is in the always-forwarded universal baseline (§12) — present with no
    # config. This baseline must stay narrow (terminal/locale + IDE hints).
    monkeypatch.setenv("TERM", "xterm-256color")
    env = lifecycle._exec_env(Config(), None, "u", "/home/u")
    assert env["TERM"] == "xterm-256color"


# --- deployment knobs are relocated out of the baseline (T19, §7.3) ----------


def test_relocated_var_not_forwarded_by_default(monkeypatch):
    # Proxy/cloud/cert knobs were removed from the hardcoded baseline — present
    # on the host but NOT forwarded unless the config names them in [env].forward.
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8080")
    monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/creds.json")
    env = lifecycle._exec_env(Config(), None, "u", "/home/u")
    assert "HTTPS_PROXY" not in env
    assert "CLOUD_ML_REGION" not in env
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env


def test_relocated_var_forwarded_when_named(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8080")
    cfg = Config(forward=("HTTPS_PROXY",))
    env = lifecycle._exec_env(cfg, None, "u", "/home/u")
    assert env["HTTPS_PROXY"] == "http://proxy:8080"


# --- precedence (only literal vs forwarded can differ in value) --------------


def test_literal_overrides_forwarded(monkeypatch):
    # Same key forwarded *and* set as a literal -> the explicit literal wins.
    monkeypatch.setenv("FOO", "fromhost")
    cfg = Config(env={"FOO": "literal"}, forward=("FOO",))
    env = lifecycle._exec_env(cfg, None, "u", "/home/u")
    assert env["FOO"] == "literal"


def test_context_literal_overrides_global_literal(monkeypatch):
    cfg = Config(env={"DEPLOY": "global"})
    ctx = _ctx(env={"DEPLOY": "ctx"})
    env = lifecycle._exec_env(cfg, ctx, "u", "/home/u")
    assert env["DEPLOY"] == "ctx"


def test_full_precedence_context_wins(monkeypatch):
    # KEY present at forward (host) + global literal + context literal layers.
    monkeypatch.setenv("KEY", "host")
    cfg = Config(env={"KEY": "global"}, forward=("KEY",))
    ctx = _ctx(env={"KEY": "ctx"})
    env = lifecycle._exec_env(cfg, ctx, "u", "/home/u")
    assert env["KEY"] == "ctx"


def test_context_forward_union_with_global(monkeypatch):
    monkeypatch.setenv("G_TOK", "g")
    monkeypatch.setenv("C_TOK", "c")
    cfg = Config(forward=("G_TOK",))
    ctx = _ctx(forward=("C_TOK",))
    env = lifecycle._exec_env(cfg, ctx, "u", "/home/u")
    assert env["G_TOK"] == "g"
    assert env["C_TOK"] == "c"


def test_unset_forward_skipped(monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    cfg = Config(forward=("MISSING_VAR",))
    env = lifecycle._exec_env(cfg, None, "u", "/home/u")
    assert "MISSING_VAR" not in env  # absent, not set empty


# --- env is NOT part of the build identity (no recreation on an env edit) ----


def test_base_build_id_ignores_env():
    plain = Config(setup=SetupConfig(packages=("jq",)))
    with_env = Config(
        setup=SetupConfig(packages=("jq",)),
        env={"EDITOR": "vim"},
        forward=("GH_TOKEN",),
    )
    assert lifecycle._base_build_id(plain) == lifecycle._base_build_id(with_env)


def test_template_build_id_ignores_env():
    plain = _ctx()
    with_env = _ctx(env={"DEPLOY": "work"}, forward=("WORK_TOKEN",))
    assert lifecycle._template_build_id("b", plain) == lifecycle._template_build_id(
        "b", with_env
    )
