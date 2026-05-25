"""Unit tests for the pure scope/resolution/guard logic in mounts (T6).

Covers DESIGN §15.3 (per-cwd isolation), §15.4 (covering-mount keying) and
§15.7 (refuse-guard + cwd denylist), plus the §6 longest-prefix resolution.
All paths are absolute literals (config paths are ~-expanded at load), so the
tests are hermetic — no real $HOME, git or daemon. The git project root is
injected via ``project_root_fn``.
"""

import pytest

from claude_wrapper.config import Config, Context, MountSpec
from claude_wrapper.lifecycle import _template_name
from claude_wrapper.mounts import (
    DEFAULT_CONTEXT,
    RefuseError,
    Resolution,
    _is_within,
    check_cwd_allowed,
    compute_scope,
    ensure_mask_dir,
    mask_container_paths,
    resolve,
    resolve_context,
    scope_hash,
)

HOME = "/home/u"


def ctx(name, when, mounts=()):
    when = (when,) if isinstance(when, str) else tuple(when)
    return Context(name=name, when=when, mounts=tuple(mounts))


def mount(path, *, from_=None, mode="rw"):
    return MountSpec(path=path, from_=from_, mode=mode)


# --- _is_within boundary -----------------------------------------------------


@pytest.mark.parametrize(
    "path,prefix,expected",
    [
        ("/a/b", "/a/b", True),       # equal
        ("/a/b/c", "/a/b", True),     # descendant
        ("/a/bc", "/a/b", False),     # NOT a component prefix
        ("/a/b/", "/a/b", True),      # trailing slash normalised
        ("/a", "/a/b", False),        # ancestor is not within
        ("/etc/x", "/", True),        # everything is under root
        ("/", "/", True),
    ],
)
def test_is_within(path, prefix, expected):
    assert _is_within(path, prefix) is expected


# --- context resolution (§6) -------------------------------------------------


def test_resolve_no_contexts_is_default():
    assert resolve_context("/home/u/proj", ()) is None


def test_resolve_no_match_is_default():
    c = ctx("api", "/home/u/work/acme-api")
    assert resolve_context("/home/u/other", (c,)) is None


def test_resolve_simple_match():
    c = ctx("api", "/home/u/work/acme-api")
    assert resolve_context("/home/u/work/acme-api/sub", (c,)) is c


def test_resolve_longest_prefix_wins():
    broad = ctx("broad", "/home/u/work")
    narrow = ctx("narrow", "/home/u/work/acme-api")
    # cwd under both → the longer (more specific) prefix wins, regardless of order.
    got = resolve_context("/home/u/work/acme-api/x", (broad, narrow))
    assert got is narrow
    got = resolve_context("/home/u/work/acme-api/x", (narrow, broad))
    assert got is narrow
    # cwd under only the broad one → broad.
    assert resolve_context("/home/u/work/other/x", (broad, narrow)) is broad


def test_resolve_or_semantics():
    c = ctx("multi", ["/home/u/a", "/home/u/b"])
    assert resolve_context("/home/u/a/x", (c,)) is c
    assert resolve_context("/home/u/b/y", (c,)) is c
    assert resolve_context("/home/u/c/z", (c,)) is None


def test_resolve_exact_length_tie_takes_config_order():
    first = ctx("first", "/home/u/shared")
    second = ctx("second", "/home/u/shared")
    assert resolve_context("/home/u/shared/x", (first, second)) is first
    assert resolve_context("/home/u/shared/x", (second, first)) is second


# --- scope keying + subsumption (§5, §15.4) ----------------------------------


def test_disjoint_covering_mounts_share_one_instance():
    # Revised §15.4: a context mounting two *disjoint* trees (~/work and
    # ~/workspace). cwds under either — A, B under work, C under workspace —
    # all key on the context token, so one shared instance, no project mount.
    c = ctx(
        "api",
        ["/home/u/work", "/home/u/workspace"],
        mounts=[mount("/home/u/work"), mount("/home/u/workspace")],
    )
    none = lambda _: None
    scope_a, add_a = compute_scope("/home/u/work/A", c, project_root_fn=none)
    scope_b, add_b = compute_scope("/home/u/work/B", c, project_root_fn=none)
    scope_c, add_c = compute_scope("/home/u/workspace/C", c, project_root_fn=none)
    assert scope_a == scope_b == scope_c == "ctx:api"
    assert add_a is False and add_b is False and add_c is False
    assert scope_hash(scope_a) == scope_hash(scope_b) == scope_hash(scope_c)


def test_nested_covering_mounts_collapse_to_one_instance():
    # Nested mounts (~/work + ~/work/foo) both cover the cwd; keying on
    # the context (not the individual mount) still collapses to a single instance.
    c = ctx(
        "nested",
        "/home/u/work",
        mounts=[mount("/home/u/work/foo"), mount("/home/u/work")],
    )
    none = lambda _: None
    deep, add_deep = compute_scope("/home/u/work/foo/x", c, project_root_fn=none)
    shallow, add_shallow = compute_scope("/home/u/work/bar", c, project_root_fn=none)
    assert deep == shallow == "ctx:nested"
    assert add_deep is False and add_shallow is False


def test_subsumed_scope_token_is_context_constant_not_template_name():
    # (4) The subsumed token is constant per context, `ctx:`-prefixed (so it can't
    # collide with a real absolute-path scope), and is NOT the bare template name
    # — the instance name appends scope_hash, so it never equals the template.
    c = ctx("api", "/home/u/work", mounts=[mount("/home/u/work")])
    none = lambda _: None
    t1, _ = compute_scope("/home/u/work/A", c, project_root_fn=none)
    t2, _ = compute_scope("/home/u/work/deep/sub", c, project_root_fn=none)
    assert t1 == t2 == "ctx:api"  # constant per context
    assert t1.startswith("ctx:") and not t1.startswith("/")
    assert t1 != _template_name("api")  # != bare template `claude-sandbox-api`
    # its hash is disjoint from any real-path scope's hash (no instance collision)
    path_scope, add = compute_scope(
        "/home/u/elsewhere/repo", None, project_root_fn=lambda _: "/home/u/elsewhere/repo"
    )
    assert add is True
    assert scope_hash(t1) != scope_hash(path_scope)


def test_scope_falls_back_to_project_root():
    # ssh-only context (mount does not cover the cwd) → scope = git root, with
    # a project mount.
    c = ctx("ssh", "/home/u/work", mounts=[mount("/home/u/.ssh", from_="/home/u/.ssh-api", mode="ro")])
    scope, add = compute_scope(
        "/home/u/work/repo/sub", c, project_root_fn=lambda _: "/home/u/work/repo"
    )
    assert scope == "/home/u/work/repo"
    assert add is True


def test_scope_falls_back_to_cwd_without_repo():
    scope, add = compute_scope("/home/u/work/loose", None, project_root_fn=lambda _: None)
    assert scope == "/home/u/work/loose"
    assert add is True


def test_per_cwd_isolation_distinct_instances():
    # §15.3: an ssh-only context, two different project dirs → different scopes
    # → different instance hashes, each with its own project mount.
    c = ctx("ssh", "/home/u", mounts=[mount("/home/u/.ssh", from_="/home/u/.ssh-api", mode="ro")])
    roots = {"/home/u/p1/x": "/home/u/p1", "/home/u/p2/y": "/home/u/p2"}
    s1, a1 = compute_scope("/home/u/p1/x", c, project_root_fn=lambda cwd: roots[cwd])
    s2, a2 = compute_scope("/home/u/p2/y", c, project_root_fn=lambda cwd: roots[cwd])
    assert s1 != s2
    assert a1 is True and a2 is True
    assert scope_hash(s1) != scope_hash(s2)


def test_scope_hash_is_stable_8_hex():
    h = scope_hash("/home/u/work")
    assert len(h) == 8 and all(ch in "0123456789abcdef" for ch in h)
    assert h == scope_hash("/home/u/work/")  # normalised before hashing


# --- guards: refuse-guard + cwd denylist (§8, §15.7) -------------------------


def alias_cfg():
    return Config(
        contexts=(
            ctx(
                "api",
                "/home/u/work",
                mounts=[
                    mount("/home/u/.ssh", from_="/home/u/.ssh-api", mode="ro"),
                    mount("/home/u/work"),  # parity, not an alias
                ],
            ),
        ),
    )


def test_refuse_home_itself():
    with pytest.raises(RefuseError, match="HOME"):
        check_cwd_allowed(HOME, home=HOME, cfg=Config())


def test_home_subdir_allowed():
    check_cwd_allowed("/home/u/proj", home=HOME, cfg=Config())  # must not raise


def test_refuse_filesystem_root():
    with pytest.raises(RefuseError, match="root"):
        check_cwd_allowed("/", home=HOME, cfg=Config())


@pytest.mark.parametrize("cwd", ["/etc", "/etc/nginx", "/usr/local/x", "/var/log"])
def test_refuse_system_roots(cwd):
    with pytest.raises(RefuseError, match="system directory"):
        check_cwd_allowed(cwd, home=HOME, cfg=Config())


def test_refuse_alias_from_dir():
    with pytest.raises(RefuseError, match="credential store"):
        check_cwd_allowed("/home/u/.ssh-api/sub", home=HOME, cfg=alias_cfg())


def test_refuse_alias_container_path():
    with pytest.raises(RefuseError, match="credential store"):
        check_cwd_allowed("/home/u/.ssh", home=HOME, cfg=alias_cfg())


def test_parity_mount_is_not_a_refused_alias():
    # ~/work is a parity (non-alias) mount — a valid workspace.
    check_cwd_allowed("/home/u/work/A", home=HOME, cfg=alias_cfg())


def test_out_of_home_project_allowed():
    check_cwd_allowed("/tmp/scratch/proj", home=HOME, cfg=Config())


# --- claude-shadow cwd guard (§8): per-cwd mount must not cover the install ---


@pytest.mark.parametrize("cwd", ["/home/u/.local", "/home/u/.local/"])
def test_refuse_dot_local_exact(cwd):
    with pytest.raises(RefuseError, match="claude install"):
        check_cwd_allowed(cwd, home=HOME, cfg=Config())


@pytest.mark.parametrize("cwd", ["/home/u/.local/share", "/home/u/.local/share/"])
def test_refuse_dot_local_share_exact(cwd):
    with pytest.raises(RefuseError, match="claude install"):
        check_cwd_allowed(cwd, home=HOME, cfg=Config())


@pytest.mark.parametrize(
    "cwd",
    ["/home/u/.local/share/claude", "/home/u/.local/share/claude/versions/1.2.3"],
)
def test_refuse_claude_install_at_or_under(cwd):
    # The install dir itself and anything inside it (e.g. version files).
    with pytest.raises(RefuseError, match="claude install"):
        check_cwd_allowed(cwd, home=HOME, cfg=Config())


@pytest.mark.parametrize(
    "cwd",
    [
        "/home/u/.local/bin",          # beside the tree — launcher PATH owns it
        "/home/u/.local/bin/sub",
        "/home/u/.local/state/x",
        "/home/u/.local/share/other",  # sibling of claude under share — fine
        "/home/u/.localx",             # not a component prefix of ~/.local
    ],
)
def test_other_dot_local_children_allowed(cwd):
    check_cwd_allowed(cwd, home=HOME, cfg=Config())  # must not raise


# --- orchestrator ------------------------------------------------------------


def test_resolve_covering_mount_resolution():
    cfg = Config(contexts=(ctx("wk", "/home/u/work", mounts=[mount("/home/u/work")]),))
    r = resolve("/home/u/work/A", cfg, home=HOME, project_root_fn=lambda _: None)
    assert isinstance(r, Resolution)
    assert r.context_name == "wk"
    assert r.scope == "ctx:wk"
    assert r.add_project_mount is False


def test_resolve_default_context_with_project_mount():
    r = resolve(
        "/home/u/loose/repo/sub", Config(), home=HOME,
        project_root_fn=lambda _: "/home/u/loose/repo",
    )
    assert r.context is None
    assert r.context_name == DEFAULT_CONTEXT
    assert r.scope == "/home/u/loose/repo"
    assert r.add_project_mount is True


def test_resolve_runs_guard_first():
    with pytest.raises(RefuseError):
        resolve(HOME, Config(), home=HOME, project_root_fn=lambda _: None)


# --- exclude masking (§8, §15.5) ---------------------------------------------


def test_mask_paths_empty_without_exclude():
    assert mask_container_paths(mount("/home/u/work")) == []


def test_mask_paths_joins_exclude_onto_path():
    spec = MountSpec(path="/home/u/work", exclude=("secrets",))
    assert mask_container_paths(spec) == ["/home/u/work/secrets"]


def test_mask_paths_multiple_and_nested():
    spec = MountSpec(path="/home/u/wk", exclude=("a", "b/c"))
    assert mask_container_paths(spec) == ["/home/u/wk/a", "/home/u/wk/b/c"]


def test_mask_paths_leading_slash_is_relative_not_escape():
    # A leading "/" must not escape the mount (os.path.join would otherwise
    # discard the base) — it is treated as relative.
    spec = MountSpec(path="/home/u/wk", exclude=("/secrets",))
    assert mask_container_paths(spec) == ["/home/u/wk/secrets"]


def test_mask_paths_normalised():
    spec = MountSpec(path="/home/u/wk/", exclude=("./sub/",))
    assert mask_container_paths(spec) == ["/home/u/wk/sub"]


def test_ensure_mask_dir_creates_empty_555(tmp_path):
    target = tmp_path / "cache" / "empty"
    got = ensure_mask_dir(str(target))
    assert got == str(target)
    assert target.is_dir()
    assert list(target.iterdir()) == []  # empty
    assert (target.stat().st_mode & 0o777) == 0o555
    # idempotent: a second call neither fails nor changes the result.
    assert ensure_mask_dir(str(target)) == str(target)
    assert (target.stat().st_mode & 0o777) == 0o555
