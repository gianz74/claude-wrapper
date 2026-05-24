"""Unit tests for T12 source build-identity + stale-instance drift decision.

The recreation mechanism itself (list_instances read, delete + cold-path CoW,
the live-session guard) is I/O against the incus daemon and is verified by a
throwaway integration run, like T3/T4/T5/T8. These cover the pure pieces: the
content-hash build ids (deterministic, sensitive to the right inputs) and the
``_instance_is_stale`` drift decision with injected tag values.
"""

from claude_wrapper import lifecycle
from claude_wrapper.config import Config, Context, MountSpec, SetupConfig


# --- _instance_is_stale: the pure drift decision (DESIGN §4/§10) -------------


def test_stale_equal_ids_not_stale():
    assert lifecycle._instance_is_stale("abc", "abc") is False


def test_stale_differing_ids_stale():
    assert lifecycle._instance_is_stale("old", "new") is True


def test_stale_none_source_is_unknown_not_stale():
    # Source predates build-stamping (e.g. a pre-T12 base): never recreate on
    # missing source info — wait for the next setup to stamp it.
    assert lifecycle._instance_is_stale("abc", None) is False
    assert lifecycle._instance_is_stale(None, None) is False


def test_stale_pre_t12_instance_against_stamped_source():
    # Instance built before T12 (no tag) but its source is now stamped -> stale.
    assert lifecycle._instance_is_stale(None, "new") is True


# --- _base_build_id: sensitive to base-defining inputs -----------------------


def _cfg(*, packages=("jq",), provision=None, mounts=(), contexts=()):
    return Config(
        setup=SetupConfig(packages=tuple(packages), provision_script=provision),
        mounts=tuple(mounts),
        contexts=tuple(contexts),
    )


def test_base_build_id_deterministic():
    cfg = _cfg(packages=("jq", "emacs"))
    assert lifecycle._base_build_id(cfg) == lifecycle._base_build_id(cfg)


def test_base_build_id_changes_with_packages():
    a = lifecycle._base_build_id(_cfg(packages=("jq",)))
    b = lifecycle._base_build_id(_cfg(packages=("jq", "git")))
    assert a != b


def test_base_build_id_changes_with_global_mounts():
    a = lifecycle._base_build_id(_cfg(mounts=(MountSpec(path="/a"),)))
    b = lifecycle._base_build_id(
        _cfg(mounts=(MountSpec(path="/a"), MountSpec(path="/b")))
    )
    assert a != b


def test_base_build_id_changes_with_mount_mode():
    a = lifecycle._base_build_id(_cfg(mounts=(MountSpec(path="/a", mode="rw"),)))
    b = lifecycle._base_build_id(_cfg(mounts=(MountSpec(path="/a", mode="ro"),)))
    assert a != b


def test_base_build_id_changes_with_provision_content(tmp_path):
    p = tmp_path / "provision.sh"
    p.write_text("echo one\n")
    before = lifecycle._base_build_id(_cfg(provision=str(p)))
    p.write_text("echo two\n")  # same path, different *content*
    after = lifecycle._base_build_id(_cfg(provision=str(p)))
    assert before != after


def test_base_build_id_ignores_missing_provision_path(tmp_path):
    # An absent provision file hashes as "" (consistent with run_provision_script
    # warning + skipping), so it must not crash and must equal the no-script case.
    missing = str(tmp_path / "nope.sh")
    assert lifecycle._base_build_id(_cfg(provision=missing)) == lifecycle._base_build_id(
        _cfg(provision=None)
    )


# --- _template_build_id: base id + per-context inputs ------------------------


def _ctx(name="api", *, provision=None, mounts=()):
    return Context(name=name, when=("/x",), provision_script=provision, mounts=tuple(mounts))


def test_template_build_id_deterministic():
    ctx = _ctx(mounts=(MountSpec(path="/p"),))
    assert lifecycle._template_build_id("base1", ctx) == lifecycle._template_build_id(
        "base1", ctx
    )


def test_template_build_id_changes_with_base_id():
    # A base rebuild must cascade to every template (hence every instance).
    ctx = _ctx()
    assert lifecycle._template_build_id("base1", ctx) != lifecycle._template_build_id(
        "base2", ctx
    )


def test_template_build_id_changes_with_context_mounts():
    a = lifecycle._template_build_id("b", _ctx(mounts=(MountSpec(path="/p"),)))
    b = lifecycle._template_build_id(
        "b", _ctx(mounts=(MountSpec(path="/p"), MountSpec(path="/q"))))
    assert a != b


def test_template_build_id_changes_with_context_provision_content(tmp_path):
    p = tmp_path / "provision-api.sh"
    p.write_text("setup-a\n")
    before = lifecycle._template_build_id("b", _ctx(provision=str(p)))
    p.write_text("setup-b\n")
    after = lifecycle._template_build_id("b", _ctx(provision=str(p)))
    assert before != after


def test_template_build_id_isolates_unrelated_context_change():
    # Editing one context's inputs must not change another's id (only that
    # context's instances recreate) — the per-source precision the user chose.
    base = "shared-base"
    ctx_a1 = _ctx(name="a", mounts=(MountSpec(path="/a"),))
    ctx_a2 = _ctx(name="a", mounts=(MountSpec(path="/a"), MountSpec(path="/a2")))
    ctx_b = _ctx(name="b", mounts=(MountSpec(path="/b"),))
    # ctx_a changed; ctx_b is identical across both worlds -> b's id is stable.
    assert lifecycle._template_build_id(base, ctx_a1) != lifecycle._template_build_id(
        base, ctx_a2
    )
    assert lifecycle._template_build_id(base, ctx_b) == lifecycle._template_build_id(
        base, ctx_b
    )
