"""Unit tests for claude_wrapper.config (DESIGN §7, TASKS T2)."""

from __future__ import annotations

import os

import pytest

from claude_wrapper import config
from claude_wrapper.config import (
    Config,
    ConfigError,
    ensure_user_config,
    load_config,
    parse_config,
)

SAMPLE = """\
[setup]
packages = ["jq", "build-essential"]
provision_script = "~/.config/claude-wrapper/provision.sh"

[reaper]
stop_idle_after     = "30m"
delete_unused_after = "14d"
max_instances       = 5

[[mounts]]
path = "~/.claude"
[[mounts]]
path = "~/.aws"
mode = "ro"

[[contexts]]
name = "api"
when = ["~/work/acme-api", "~/work/other"]
provision_script = "~/prov-api.sh"
  [[contexts.mounts]]
  path = "~/.ssh"
  from = "~/.ssh-api"
  mode = "ro"
  [[contexts.mounts]]
  path    = "~/work"
  exclude = ["secrets", "secret"]

[[contexts]]
name = "catchall"
when = "~"
"""


def _write(tmp_path, text, name="config.toml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_load_sample_config(tmp_path):
    cfg = load_config(_write(tmp_path, SAMPLE))
    assert isinstance(cfg, Config)

    # [setup]
    assert cfg.setup.packages == ("jq", "build-essential")
    assert cfg.setup.provision_script == os.path.expanduser(
        "~/.config/claude-wrapper/provision.sh"
    )

    # [reaper] — durations parsed to seconds
    assert cfg.reaper.stop_idle_after == 30 * 60
    assert cfg.reaper.delete_unused_after == 14 * 86400
    assert cfg.reaper.max_instances == 5

    # global mounts, ~-expanded
    assert [m.path for m in cfg.mounts] == [
        os.path.expanduser("~/.claude"),
        os.path.expanduser("~/.aws"),
    ]
    assert cfg.mounts[0].mode == "rw"  # default
    assert cfg.mounts[1].mode == "ro"
    assert cfg.mounts[0].is_alias is False

    # contexts
    assert [c.name for c in cfg.contexts] == ["api", "catchall"]
    api = cfg.contexts[0]
    assert api.when == (
        os.path.expanduser("~/work/acme-api"),
        os.path.expanduser("~/work/other"),
    )
    # alias mount: path is container-side, host_path is the `from` backing
    ssh = api.mounts[0]
    assert ssh.is_alias is True
    assert ssh.path == os.path.expanduser("~/.ssh")
    assert ssh.host_path == os.path.expanduser("~/.ssh-api")
    assert ssh.mode == "ro"
    # exclude preserved as relative sub-paths
    assert api.mounts[1].exclude == ("secrets", "secret")

    # `when` given as a bare string is coerced to a one-element list
    assert cfg.contexts[1].when == (os.path.expanduser("~"),)


def test_empty_config_uses_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, ""))
    assert cfg.setup.packages == ()
    assert cfg.setup.provision_script is None
    assert cfg.reaper.stop_idle_after == 30 * 60
    assert cfg.reaper.max_instances == 0
    assert cfg.mounts == ()
    assert cfg.contexts == ()


def test_duplicate_context_name_rejected(tmp_path):
    text = """\
[[contexts]]
name = "dup"
when = ["~/a"]
[[contexts]]
name = "dup"
when = ["~/b"]
"""
    with pytest.raises(ConfigError, match="duplicate context name 'dup'"):
        load_config(_write(tmp_path, text))


def test_malformed_toml_rejected(tmp_path):
    with pytest.raises(ConfigError, match="malformed TOML"):
        load_config(_write(tmp_path, "this is = = not toml ["))


def test_missing_context_name_rejected(tmp_path):
    text = '[[contexts]]\nwhen = ["~/a"]\n'
    with pytest.raises(ConfigError, match="missing required 'name'"):
        load_config(_write(tmp_path, text))


def test_missing_when_rejected(tmp_path):
    text = '[[contexts]]\nname = "x"\n'
    with pytest.raises(ConfigError, match="missing required 'when'"):
        load_config(_write(tmp_path, text))


def test_reserved_default_name_rejected(tmp_path):
    text = '[[contexts]]\nname = "default"\nwhen = ["~/a"]\n'
    with pytest.raises(ConfigError, match="reserved"):
        load_config(_write(tmp_path, text))


def test_invalid_mount_mode_rejected(tmp_path):
    text = '[[mounts]]\npath = "~/x"\nmode = "rx"\n'
    with pytest.raises(ConfigError, match="invalid mode 'rx'"):
        load_config(_write(tmp_path, text))


def test_mount_missing_path_rejected(tmp_path):
    text = '[[mounts]]\nmode = "ro"\n'
    with pytest.raises(ConfigError, match="missing required 'path'"):
        load_config(_write(tmp_path, text))


def test_invalid_duration_rejected(tmp_path):
    text = '[reaper]\nstop_idle_after = "soon"\n'
    with pytest.raises(ConfigError, match="invalid duration"):
        load_config(_write(tmp_path, text))


def test_negative_max_instances_rejected(tmp_path):
    text = "[reaper]\nmax_instances = -1\n"
    with pytest.raises(ConfigError, match="max_instances"):
        load_config(_write(tmp_path, text))


def test_missing_file_rejected(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does-not-exist.toml")


def test_parse_config_pure_dict():
    cfg = parse_config({"setup": {"packages": ["jq"]}})
    assert cfg.setup.packages == ("jq",)


@pytest.mark.parametrize(
    "value,seconds",
    [("90s", 90), ("30m", 1800), ("2h", 7200), ("14d", 14 * 86400), ("45", 45)],
)
def test_duration_units(tmp_path, value, seconds):
    cfg = load_config(_write(tmp_path, f'[reaper]\nstop_idle_after = "{value}"\n'))
    assert cfg.reaper.stop_idle_after == seconds


# --- [vars] / ${NAME} expansion (DESIGN §7.1, TASKS T13) ---------------------


def test_vars_expansion_in_mount_from(tmp_path):
    text = """\
[vars]
WM = "~/x"

[[contexts]]
name = "v"
when = ["~/proj"]
  [[contexts.mounts]]
  path = "~/.gnupg"
  from = "${WM}/.gnupg"
"""
    cfg = load_config(_write(tmp_path, text))
    mount = cfg.contexts[0].mounts[0]
    # ${WM} substituted first, then ~ expanded -> /home/<user>/x/.gnupg
    assert mount.host_path == os.path.expanduser("~/x/.gnupg")
    assert mount.path == os.path.expanduser("~/.gnupg")


def test_undefined_var_rejected_naming_the_key(tmp_path):
    text = """\
[[mounts]]
path = "${NOPE}/data"
"""
    with pytest.raises(ConfigError, match=r"undefined variable \$\{NOPE\}"):
        load_config(_write(tmp_path, text))


def test_bare_dollar_name_left_literal(tmp_path):
    # No braces -> not a substitution target; the literal string survives.
    text = '[[mounts]]\npath = "/data/$HOME/x"\n'
    cfg = load_config(_write(tmp_path, text))
    assert cfg.mounts[0].path == "/data/$HOME/x"


def test_varless_config_parses_identically(tmp_path):
    # The pre-pass must be a no-op when there is no [vars] table / no ${...}.
    cfg = load_config(_write(tmp_path, SAMPLE))
    assert [m.path for m in cfg.mounts] == [
        os.path.expanduser("~/.claude"),
        os.path.expanduser("~/.aws"),
    ]
    assert cfg.contexts[0].mounts[0].host_path == os.path.expanduser("~/.ssh-api")


def test_vars_expansion_across_sections(tmp_path):
    # ${NAME} reaches every string: packages, when, provision_script, mounts.
    text = """\
[vars]
ROOT = "~/work"
PKG = "ripgrep"

[setup]
packages = ["${PKG}"]

[[contexts]]
name = "v"
when = ["${ROOT}/a"]
provision_script = "${ROOT}/prov.sh"
  [[contexts.mounts]]
  path = "${ROOT}/a"
"""
    cfg = load_config(_write(tmp_path, text))
    assert cfg.setup.packages == ("ripgrep",)
    assert cfg.contexts[0].when == (os.path.expanduser("~/work/a"),)
    assert cfg.contexts[0].provision_script == os.path.expanduser(
        "~/work/prov.sh"
    )
    assert cfg.contexts[0].mounts[0].path == os.path.expanduser("~/work/a")


def test_vars_value_not_recursively_expanded(tmp_path):
    # A ${...} inside a [vars] value is inserted verbatim, never re-resolved.
    text = """\
[vars]
A = "${B}/x"

[[mounts]]
path = "/data/${A}"
"""
    cfg = load_config(_write(tmp_path, text))
    assert cfg.mounts[0].path == "/data/${B}/x"


# --- mount groups + context `include` (DESIGN §7.2, TASKS T14) ---------------

_CREDS_GROUP = """\
[vars]
WM = "~/work-mappings"

[mount_groups.creds]
mounts = [
  { path = "~/.ssh",       from = "${WM}/.ssh",       mode = "ro" },
  { path = "~/.gnupg",     from = "${WM}/.gnupg",     mode = "ro" },
  { path = "~/.gitconfig", from = "${WM}/.gitconfig", mode = "ro" },
]
"""


def test_two_contexts_share_group_plus_own(tmp_path):
    text = _CREDS_GROUP + """\
[[contexts]]
name    = "api"
when    = ["~/work/api"]
include = ["creds"]
  [[contexts.mounts]]
  path = "~/work/api"

[[contexts]]
name    = "web"
when    = ["~/work/web"]
include = "creds"
"""
    cfg = load_config(_write(tmp_path, text))
    api, web = cfg.contexts

    # both contexts carry the three group mounts (~-expanded, ${WM} resolved)
    creds = {
        os.path.expanduser("~/.ssh"): os.path.expanduser("~/work-mappings/.ssh"),
        os.path.expanduser("~/.gnupg"): os.path.expanduser("~/work-mappings/.gnupg"),
        os.path.expanduser("~/.gitconfig"): os.path.expanduser("~/work-mappings/.gitconfig"),
    }
    for ctx in (api, web):
        by_path = {m.path: m for m in ctx.mounts}
        for path, host in creds.items():
            assert path in by_path, ctx.name
            assert by_path[path].host_path == host
            assert by_path[path].mode == "ro"

    # api has its own extra mount; web (bare-string include) has only the creds
    assert os.path.expanduser("~/work/api") in {m.path for m in api.mounts}
    assert len(web.mounts) == 3


def test_include_order_then_inline(tmp_path):
    # included groups (in `include` order) come before the context's own inline
    # mounts; first-seen position is stable.
    text = """\
[mount_groups.a]
mounts = [{ path = "~/a1" }, { path = "~/a2" }]
[mount_groups.b]
mounts = [{ path = "~/b1" }]

[[contexts]]
name    = "x"
when    = ["~/x"]
include = ["a", "b"]
  [[contexts.mounts]]
  path = "~/own"
"""
    cfg = load_config(_write(tmp_path, text))
    paths = [m.path for m in cfg.contexts[0].mounts]
    assert paths == [os.path.expanduser(p) for p in ("~/a1", "~/a2", "~/b1", "~/own")]


def test_inline_overrides_group_mount(tmp_path):
    # an inline mount with the same container `path` as a group mount wins
    # (later-wins): asserted on mode + from_.
    text = """\
[mount_groups.creds]
mounts = [{ path = "~/.ssh", from = "~/work-mappings/.ssh", mode = "ro" }]

[[contexts]]
name    = "x"
when    = ["~/x"]
include = ["creds"]
  [[contexts.mounts]]
  path = "~/.ssh"
  mode = "rw"
"""
    cfg = load_config(_write(tmp_path, text))
    by_path = {m.path: m for m in cfg.contexts[0].mounts}
    ssh = by_path[os.path.expanduser("~/.ssh")]
    assert ssh.mode == "rw"            # inline mode wins
    assert ssh.from_ is None           # inline is parity, overriding the alias
    # deduped: only one ~/.ssh entry
    assert [m.path for m in cfg.contexts[0].mounts].count(
        os.path.expanduser("~/.ssh")
    ) == 1


def test_later_group_overrides_earlier(tmp_path):
    text = """\
[mount_groups.a]
mounts = [{ path = "~/.ssh", mode = "ro" }]
[mount_groups.b]
mounts = [{ path = "~/.ssh", mode = "rw" }]

[[contexts]]
name    = "x"
when    = ["~/x"]
include = ["a", "b"]
"""
    cfg = load_config(_write(tmp_path, text))
    by_path = {m.path: m for m in cfg.contexts[0].mounts}
    assert by_path[os.path.expanduser("~/.ssh")].mode == "rw"  # later group wins


def test_unknown_include_rejected(tmp_path):
    text = """\
[[contexts]]
name    = "x"
when    = ["~/x"]
include = ["nope"]
"""
    with pytest.raises(
        ConfigError, match=r"context 'x': unknown mount group 'nope'"
    ):
        load_config(_write(tmp_path, text))


def test_template_build_id_tracks_group_change(tmp_path):
    # changing a group's mounts changes the flattened Context.mounts, hence the
    # template build-id of every context that includes it (T12 sensitivity).
    from claude_wrapper import lifecycle

    base = """\
[mount_groups.creds]
mounts = [{{ path = "~/.ssh", from = "~/work-mappings/.ssh", mode = "{mode}" }}]

[[contexts]]
name    = "x"
when    = ["~/x"]
include = ["creds"]
"""
    cfg_ro = load_config(_write(tmp_path, base.format(mode="ro"), "ro.toml"))
    cfg_rw = load_config(_write(tmp_path, base.format(mode="rw"), "rw.toml"))
    id_ro = lifecycle._template_build_id("base", cfg_ro.contexts[0])
    id_rw = lifecycle._template_build_id("base", cfg_rw.contexts[0])
    assert id_ro != id_rw


def test_ensure_user_config_writes_defaults(tmp_path):
    d = tmp_path / "cfgdir"
    path = ensure_user_config(d)
    assert path == d / "config.toml"
    assert path.exists()
    provision = d / "provision.sh"
    assert provision.exists()
    assert provision.stat().st_mode & 0o111  # executable

    # the shipped default must itself parse + validate cleanly
    cfg = load_config(path)
    assert isinstance(cfg, Config)
    assert "~/.claude" not in [m.path for m in cfg.mounts]  # ~ is expanded
    assert os.path.expanduser("~/.claude") in [m.path for m in cfg.mounts]


def test_ensure_user_config_idempotent_no_overwrite(tmp_path):
    d = tmp_path / "cfgdir"
    path = ensure_user_config(d)
    path.write_text('[[mounts]]\npath = "~/custom"\n')
    again = ensure_user_config(d)  # must not clobber the user's edits
    assert again == path
    cfg = load_config(path)
    assert [m.path for m in cfg.mounts] == [os.path.expanduser("~/custom")]
