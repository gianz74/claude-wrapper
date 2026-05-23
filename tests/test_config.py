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
