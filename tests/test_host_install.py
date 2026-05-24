"""Unit tests for the T11 host-install pure logic (DESIGN §13/§11/§8).

Two pieces:

* the claude-shadow guard (``_check_no_claude_shadow``) — reuses
  ``mounts._is_within`` to refuse a mount at/above ``~/.local/share/claude`` or
  the private launcher dir;
* the ``claude``-resolves-to-wrapper $PATH check (``_claude_resolves_to_wrapper``)
  — the shell's first-match lookup, with filesystem facts injected so it is
  hermetic.
"""

import pytest

from claude_wrapper.config import Config, Context, MountSpec
from claude_wrapper.lifecycle import (
    LAUNCHER_DIR,
    SetupError,
    _check_no_claude_shadow,
    _claude_resolves_to_wrapper,
)

HOME = "/home/u"


def cfg(global_mounts=(), ctx_mounts=()):
    contexts = ()
    if ctx_mounts:
        contexts = (
            Context(
                name="c",
                when=("/home/u/proj",),
                mounts=tuple(MountSpec(path=p) for p in ctx_mounts),
            ),
        )
    return Config(
        mounts=tuple(MountSpec(path=p) for p in global_mounts),
        contexts=contexts,
    )


# --- claude-shadow guard (§8) ------------------------------------------------


def test_shadow_guard_allows_normal_mounts():
    # The default-config mounts plus ~/.local/bin (explicitly allowed by §8).
    _check_no_claude_shadow(
        cfg(["/home/u/.claude", "/home/u/.claude.json", "/home/u/.local/bin"]), HOME
    )  # no raise


@pytest.mark.parametrize(
    "bad",
    [
        "/home/u/.local/share/claude",  # exactly the binary dir
        "/home/u/.local/share",         # an ancestor
        "/home/u/.local",               # a higher ancestor
        "/home/u",                       # $HOME itself
        LAUNCHER_DIR,                    # the private launcher dir
        "/usr/local/lib",                # an ancestor of the launcher dir
    ],
)
def test_shadow_guard_refuses_global_mount(bad):
    with pytest.raises(SetupError):
        _check_no_claude_shadow(cfg([bad]), HOME)


def test_shadow_guard_checks_context_mounts_too():
    with pytest.raises(SetupError):
        _check_no_claude_shadow(cfg(ctx_mounts=["/home/u/.local/share/claude"]), HOME)


def test_shadow_guard_allows_sibling_under_local_share():
    # ~/.local/share/foo is not at/above the claude dir → fine.
    _check_no_claude_shadow(cfg(["/home/u/.local/share/foo"]), HOME)


# --- claude-resolves-to-wrapper $PATH check (§13) ----------------------------


def _checker(execs, links):
    """Build (is_exec, realpath) from a set of executables + a symlink map."""
    return (
        lambda p: p in execs,
        lambda p: links.get(p, p),
    )


def test_resolves_when_shim_precedes_real_binary():
    # ~/bin/claude (a wrapper symlink) comes before the real ~/.local/bin/claude.
    execs = {"/home/u/bin/claude", "/home/u/.local/bin/claude"}
    links = {
        "/home/u/bin/claude": "/venv/bin/claude-wrapper",
        "/home/u/.local/bin/claude-wrapper": "/venv/bin/claude-wrapper",
        "/home/u/.local/bin/claude": "/home/u/.local/share/claude/versions/2",
    }
    is_exec, realpath = _checker(execs, links)
    ok, found = _claude_resolves_to_wrapper(
        "/home/u/bin:/home/u/.local/bin",
        "/home/u/.local/bin/claude-wrapper",
        is_exec=is_exec,
        realpath=realpath,
    )
    assert ok is True
    assert found == "/home/u/bin/claude"


def test_not_resolved_when_real_binary_wins():
    # Same files, but ~/.local/bin (real binary) is ordered first → loses.
    execs = {"/home/u/bin/claude", "/home/u/.local/bin/claude"}
    links = {
        "/home/u/bin/claude": "/venv/bin/claude-wrapper",
        "/home/u/.local/bin/claude-wrapper": "/venv/bin/claude-wrapper",
        "/home/u/.local/bin/claude": "/home/u/.local/share/claude/versions/2",
    }
    is_exec, realpath = _checker(execs, links)
    ok, found = _claude_resolves_to_wrapper(
        "/home/u/.local/bin:/home/u/bin",
        "/home/u/.local/bin/claude-wrapper",
        is_exec=is_exec,
        realpath=realpath,
    )
    assert ok is False
    assert found == "/home/u/.local/bin/claude"


def test_not_resolved_when_no_claude_on_path():
    is_exec, realpath = _checker(set(), {})
    ok, found = _claude_resolves_to_wrapper(
        "/a:/b",
        "/venv/bin/claude-wrapper",
        is_exec=is_exec,
        realpath=realpath,
    )
    assert ok is False
    assert found is None


def test_empty_path_entries_are_skipped():
    execs = {"/x/claude"}
    is_exec, realpath = _checker(execs, {})
    ok, found = _claude_resolves_to_wrapper(
        "::/x",  # leading empty entries must be ignored, not matched as cwd
        "/x/claude",
        is_exec=is_exec,
        realpath=realpath,
    )
    assert ok is True
    assert found == "/x/claude"
