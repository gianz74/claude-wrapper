"""Unit tests for the pure scope/resolution/guard logic in mounts (T6).

Covers DESIGN §15.3 (per-cwd isolation), §15.4 (covering-mount keying) and
§15.7 (refuse-guard + cwd denylist), plus the §6 longest-prefix resolution.
All paths are absolute literals (config paths are ~-expanded at load), so the
tests are hermetic — no real $HOME, git or daemon. The git project root is
injected via ``project_root_fn``.
"""

import pytest

from claude_wrapper.config import Config, Context, MountSpec
from claude_wrapper.mounts import (
    DEFAULT_CONTEXT,
    RefuseError,
    Resolution,
    _is_within,
    check_cwd_allowed,
    compute_scope,
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


def test_covering_mount_subsumes_and_shares_instance():
    # §15.4: a context mounting ~/work; A and B under it → same scope,
    # no project mount, same instance hash.
    c = ctx("wk", "/home/u/work", mounts=[mount("/home/u/work")])
    scope_a, add_a = compute_scope("/home/u/work/A", c, project_root_fn=lambda _: None)
    scope_b, add_b = compute_scope("/home/u/work/B", c, project_root_fn=lambda _: None)
    assert scope_a == scope_b == "/home/u/work"
    assert add_a is False and add_b is False
    assert scope_hash(scope_a) == scope_hash(scope_b)


def test_broadest_covering_mount_chosen():
    # Nested mounts both cover the cwd → the broadest (shortest path) wins.
    c = ctx(
        "nested",
        "/home/u/work",
        mounts=[mount("/home/u/work/special"), mount("/home/u/work")],
    )
    scope, add = compute_scope("/home/u/work/special/x", c, project_root_fn=lambda _: None)
    assert scope == "/home/u/work"
    assert add is False


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


# --- orchestrator ------------------------------------------------------------


def test_resolve_covering_mount_resolution():
    cfg = Config(contexts=(ctx("wk", "/home/u/work", mounts=[mount("/home/u/work")]),))
    r = resolve("/home/u/work/A", cfg, home=HOME, project_root_fn=lambda _: None)
    assert isinstance(r, Resolution)
    assert r.context_name == "wk"
    assert r.scope == "/home/u/work"
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
