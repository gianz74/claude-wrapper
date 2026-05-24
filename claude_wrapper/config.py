"""Config loading + validation (DESIGN §7).

Loads ``~/.config/claude-wrapper/config.toml`` via :mod:`tomllib` and models
``[setup]``, ``[reaper]``, ``[[mounts]]`` and ``[[contexts]]`` into frozen
dataclasses. All host paths are ``~``-expanded at load time so downstream code
never has to.

A ``[vars]`` table (DESIGN §7.1) supplies ``${NAME}`` substitution — a verbatim
pre-pass over every other string value, run *before* ``~`` expansion — so
per-machine configs can stop repeating long path prefixes. TOML has no native
interpolation; this is the loader's own sugar, consumed at parse time and absent
from the runtime :class:`Config`.

Public surface:

* :class:`Config` (+ :class:`SetupConfig`, :class:`ReaperConfig`,
  :class:`MountSpec`, :class:`Context`) — the parsed model.
* :class:`ConfigError` — raised with a clear, user-facing message on any
  malformed/invalid config (including a malformed TOML file).
* :func:`parse_config` — pure ``dict -> Config`` (easy to unit-test).
* :func:`load_config` — read + parse a specific file.
* :func:`ensure_user_config` — write the documented default ``config.toml`` +
  ``provision.sh`` stub on first run if absent; returns the config path.
* :func:`load_user_config` — convenience: ``load_config(ensure_user_config())``.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# Bumped when the *shape* of the config changes incompatibly. Folded into the
# lifecycle stamp hash (DESIGN §10) so a schema change forces a re-`setup`.
# v2: ``[vars]`` ``${NAME}`` expansion (T13) + mount groups / ``include`` (T14).
SCHEMA_VERSION = 2

_VALID_MODES = ("ro", "rw")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")

# Brace form only — a bare ``$NAME`` is left literal so paths containing ``$``
# survive untouched. Names match a conventional identifier.
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Environment-variable names (literals + ``forward`` entries) must be valid shell
# identifiers (DESIGN §7.3). ``HOME``/``USER``/``PATH`` are reserved — identity
# (§3) + the private claude launcher PATH (§11) — and rejected in any ``[env]``.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_ENV = ("HOME", "USER", "PATH")


class ConfigError(Exception):
    """A user-facing configuration error (bad TOML, missing/invalid field)."""


# --- model -------------------------------------------------------------------


@dataclass(frozen=True)
class MountSpec:
    """A persistent bind mount (global ``[[mounts]]`` or ``[[contexts.mounts]]``).

    ``path`` is the container-side location (and, with no ``from``, also the
    host backing — "parity"). ``from_`` is the host backing when *aliasing*
    (e.g. ``~/.ssh`` backed by ``~/.ssh-api``). ``exclude`` lists sub-paths
    (relative to ``path``) to mask with an empty read-only overmount (§8).
    """

    path: str  # container-side, ~-expanded
    from_: str | None = None  # host backing when aliasing, ~-expanded
    mode: str = "rw"  # "ro" | "rw"
    exclude: tuple[str, ...] = ()  # sub-paths relative to path

    @property
    def host_path(self) -> str:
        """Host-side backing path (the alias source, else ``path``)."""
        return self.from_ if self.from_ is not None else self.path

    @property
    def is_alias(self) -> bool:
        return self.from_ is not None


@dataclass(frozen=True)
class SetupConfig:
    packages: tuple[str, ...] = ()
    provision_script: str | None = None  # ~-expanded


@dataclass(frozen=True)
class ReaperConfig:
    stop_idle_after: int = 30 * 60  # seconds
    delete_unused_after: int = 14 * 86400  # seconds
    max_instances: int = 0  # 0 = unlimited


@dataclass(frozen=True)
class Context:
    name: str
    when: tuple[str, ...]  # host path prefixes (OR), ~-expanded
    provision_script: str | None = None  # ~-expanded
    mounts: tuple[MountSpec, ...] = ()
    # Per-context env (DESIGN §7.3): literal pairs + ``forward`` host var names.
    # Run-path-only — applied at ``exec claude``, never baked into a template.
    env: Mapping[str, str] = field(default_factory=dict)
    forward: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    setup: SetupConfig = field(default_factory=SetupConfig)
    reaper: ReaperConfig = field(default_factory=ReaperConfig)
    mounts: tuple[MountSpec, ...] = ()  # global, baked into claude-base
    contexts: tuple[Context, ...] = ()
    # Global env (DESIGN §7.3): merged broadest-first with each context's own env
    # at ``exec`` time. Run-path-only, so absent from the §4/§10 build identity.
    env: Mapping[str, str] = field(default_factory=dict)
    forward: tuple[str, ...] = ()


# --- locations ---------------------------------------------------------------


def user_config_dir() -> Path:
    """``$XDG_CONFIG_HOME/claude-wrapper`` (falling back to ``~/.config``)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(base) / "claude-wrapper"


# --- low-level coercion helpers ----------------------------------------------


def _expand(path: str) -> str:
    return os.path.expanduser(path)


def _require_str(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: expected a string, got {type(value).__name__}")
    return value


def _opt_str(value: object, where: str) -> str | None:
    return None if value is None else _require_str(value, where)


def _str_list(value: object, where: str) -> tuple[str, ...]:
    """Accept a single string (coerced to one element) or a list of strings."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(_require_str(v, f"{where}[{i}]") for i, v in enumerate(value))
    raise ConfigError(f"{where}: expected a string or list of strings")


def _parse_duration(value: object, where: str) -> int:
    """``"30m"`` / ``"14d"`` / ``"90s"`` / bare seconds -> int seconds."""
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0:
            raise ConfigError(f"{where}: duration must not be negative")
        return value
    if isinstance(value, str):
        m = _DURATION_RE.match(value)
        if m:
            n, unit = int(m.group(1)), m.group(2) or "s"
            return n * _DURATION_UNITS[unit]
    raise ConfigError(
        f"{where}: invalid duration {value!r} "
        "(use e.g. '30m', '14d', '90s', or seconds as an integer)"
    )


# --- variable expansion (`[vars]`, DESIGN §7.1) ------------------------------


def _parse_vars(raw: object) -> dict[str, str]:
    """Parse the ``[vars]`` table into a flat ``name -> str`` map.

    Values are used verbatim — a ``${...}`` inside a var value is *not* resolved
    (no recursion, vars cannot reference vars).
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("[vars]: expected a table")
    variables: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(value, str):
            raise ConfigError(
                f"[vars].{name}: expected a string, got {type(value).__name__}"
            )
        variables[name] = value
    return variables


def _substitute_vars(value: object, variables: dict[str, str], where: str) -> object:
    """Recursively replace ``${NAME}`` in every string under *value*.

    Brace form only; an undefined ``${NAME}`` raises :class:`ConfigError` naming
    the variable (and where it appeared). Non-string scalars pass through.
    """
    if isinstance(value, str):

        def _replace(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in variables:
                raise ConfigError(
                    f"{where}: undefined variable ${{{name}}} "
                    "(define it under [vars])"
                )
            return variables[name]

        return _VAR_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {
            k: _substitute_vars(v, variables, f"{where}.{k}") for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _substitute_vars(v, variables, f"{where}[{i}]")
            for i, v in enumerate(value)
        ]
    return value


# --- section parsers ---------------------------------------------------------


def _parse_mount(raw: object, where: str) -> MountSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    if "path" not in raw:
        raise ConfigError(f"{where}: missing required 'path'")

    mode = raw.get("mode", "rw")
    mode = _require_str(mode, f"{where}.mode")
    if mode not in _VALID_MODES:
        raise ConfigError(
            f"{where}.mode: invalid mode {mode!r} (expected 'ro' or 'rw')"
        )

    from_raw = _opt_str(raw.get("from"), f"{where}.from")
    exclude = _str_list(raw["exclude"], f"{where}.exclude") if "exclude" in raw else ()

    return MountSpec(
        path=_expand(_require_str(raw["path"], f"{where}.path")),
        from_=_expand(from_raw) if from_raw is not None else None,
        mode=mode,
        exclude=exclude,
    )


def _parse_setup(raw: object) -> SetupConfig:
    if raw is None:
        return SetupConfig()
    if not isinstance(raw, dict):
        raise ConfigError("[setup]: expected a table")
    packages = (
        _str_list(raw["packages"], "[setup].packages") if "packages" in raw else ()
    )
    script = _opt_str(raw.get("provision_script"), "[setup].provision_script")
    return SetupConfig(
        packages=packages,
        provision_script=_expand(script) if script is not None else None,
    )


def _parse_reaper(raw: object) -> ReaperConfig:
    if raw is None:
        return ReaperConfig()
    if not isinstance(raw, dict):
        raise ConfigError("[reaper]: expected a table")
    defaults = ReaperConfig()
    max_inst = raw.get("max_instances", defaults.max_instances)
    if not isinstance(max_inst, int) or isinstance(max_inst, bool) or max_inst < 0:
        raise ConfigError(
            "[reaper].max_instances: expected a non-negative integer "
            "(0 = unlimited)"
        )
    return ReaperConfig(
        stop_idle_after=(
            _parse_duration(raw["stop_idle_after"], "[reaper].stop_idle_after")
            if "stop_idle_after" in raw
            else defaults.stop_idle_after
        ),
        delete_unused_after=(
            _parse_duration(raw["delete_unused_after"], "[reaper].delete_unused_after")
            if "delete_unused_after" in raw
            else defaults.delete_unused_after
        ),
        max_instances=max_inst,
    )


# --- environment (`[env]` + context `env`, DESIGN §7.3) ----------------------


def _parse_env(raw: object, where: str) -> tuple[dict[str, str], tuple[str, ...]]:
    """Parse an ``[env]`` / per-context ``env`` table.

    Returns ``(literals, forward)``: the reserved lowercase key ``forward`` is a
    ``list[str]`` of host var names; every other pair is a literal ``KEY = "value"``.
    Validates env-name shape, string values, and rejects the reserved
    ``HOME``/``USER``/``PATH`` (identity §3 + launcher §11). ``${VAR}`` (§7.1) is
    already expanded into literal values by the pre-pass; env values are *not*
    ``~``-expanded (they are not paths).
    """
    if raw is None:
        return {}, ()
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    forward: tuple[str, ...] = ()
    literals: dict[str, str] = {}
    for key, value in raw.items():
        if key == "forward":
            forward = _str_list(value, f"{where}.forward")
            for name in forward:
                _check_env_name(name, f"{where}.forward")
            continue
        _check_env_name(key, where)
        if not isinstance(value, str):
            raise ConfigError(
                f"{where}.{key}: expected a string, got {type(value).__name__}"
            )
        literals[key] = value
    return literals, forward


def _check_env_name(name: str, where: str) -> None:
    if not _ENV_NAME_RE.match(name):
        raise ConfigError(
            f"{where}: invalid environment variable name {name!r} "
            "(expected letters, digits and underscores)"
        )
    if name in _RESERVED_ENV:
        raise ConfigError(
            f"{where}: {name} is reserved (identity + launcher PATH) "
            "and may not be set in [env]"
        )


# --- mount groups (`[mount_groups]` + context `include`, DESIGN §7.2) --------


def _parse_mount_groups(raw: object) -> dict[str, tuple[MountSpec, ...]]:
    """Parse ``[mount_groups.<name>]`` tables into a ``name -> mounts`` map.

    Each group's ``mounts`` array is parsed exactly like ``[[contexts.mounts]]``
    (inline or full tables). Parse-time-only — groups are flattened into the
    contexts that ``include`` them and never stored on :class:`Config`.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("[mount_groups]: expected a table")
    groups: dict[str, tuple[MountSpec, ...]] = {}
    for name, body in raw.items():
        where = f"[mount_groups.{name}]"
        if not isinstance(body, dict):
            raise ConfigError(f"{where}: expected a table")
        raw_mounts = body.get("mounts", [])
        if not isinstance(raw_mounts, list):
            raise ConfigError(f"{where}.mounts: expected an array of tables")
        groups[name] = tuple(
            _parse_mount(m, f"{where}.mounts[{i}]") for i, m in enumerate(raw_mounts)
        )
    return groups


def _flatten_context_mounts(
    included: list[tuple[MountSpec, ...]], inline: tuple[MountSpec, ...]
) -> tuple[MountSpec, ...]:
    """Merge included-group mounts (in ``include`` order) then a context's own
    inline mounts, deduped by container-side ``path`` with **later-wins** (inline
    overrides an included mount of the same ``path``; a later group overrides an
    earlier one). The flattened tuple is all downstream ever sees."""
    merged: dict[str, MountSpec] = {}
    for group_mounts in included:
        for m in group_mounts:
            merged[m.path] = m
    for m in inline:
        merged[m.path] = m
    return tuple(merged.values())


def _parse_context(
    raw: object, index: int, groups: dict[str, tuple[MountSpec, ...]]
) -> Context:
    where = f"[[contexts]][{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    if "name" not in raw:
        raise ConfigError(f"{where}: missing required 'name'")
    name = _require_str(raw["name"], f"{where}.name")
    if not name.strip():
        raise ConfigError(f"{where}.name: must not be empty")
    if "when" not in raw:
        raise ConfigError(f"context {name!r}: missing required 'when' (path prefixes)")
    when = tuple(_expand(p) for p in _str_list(raw["when"], f"context {name!r}.when"))
    if not when:
        raise ConfigError(f"context {name!r}: 'when' must list at least one path prefix")

    script = _opt_str(raw.get("provision_script"), f"context {name!r}.provision_script")
    raw_mounts = raw.get("mounts", [])
    if not isinstance(raw_mounts, list):
        raise ConfigError(f"context {name!r}.mounts: expected an array of tables")
    inline_mounts = tuple(
        _parse_mount(m, f"context {name!r}.mounts[{i}]")
        for i, m in enumerate(raw_mounts)
    )

    include = (
        _str_list(raw["include"], f"context {name!r}.include")
        if "include" in raw
        else ()
    )
    included: list[tuple[MountSpec, ...]] = []
    for gname in include:
        if gname not in groups:
            raise ConfigError(
                f"context {name!r}: unknown mount group {gname!r} in 'include'"
            )
        included.append(groups[gname])

    env, forward = _parse_env(raw.get("env"), f"context {name!r}.env")

    return Context(
        name=name,
        when=when,
        provision_script=_expand(script) if script is not None else None,
        mounts=_flatten_context_mounts(included, inline_mounts),
        env=env,
        forward=forward,
    )


def parse_config(data: dict, *, source: str = "<config>") -> Config:
    """Validate a parsed-TOML ``dict`` into a :class:`Config`.

    Raises :class:`ConfigError` with a clear message on any problem. ``source``
    is only used to make error messages locatable.
    """
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: top level must be a table")

    # `${NAME}` pre-pass (DESIGN §7.1): substitute into every string value
    # *except* the [vars] table itself, before the section parsers (and their
    # `~` expansion) run. [vars] is then dropped — it has no runtime effect.
    variables = _parse_vars(data.get("vars"))
    data = {
        key: _substitute_vars(value, variables, key)
        for key, value in data.items()
        if key != "vars"
    }

    raw_mounts = data.get("mounts", [])
    if not isinstance(raw_mounts, list):
        raise ConfigError("[[mounts]]: expected an array of tables")
    global_mounts = tuple(
        _parse_mount(m, f"[[mounts]][{i}]") for i, m in enumerate(raw_mounts)
    )

    # Mount groups (§7.2): parsed here so contexts can `include` them; flattened
    # into Context.mounts and never stored on Config.
    groups = _parse_mount_groups(data.get("mount_groups"))

    raw_contexts = data.get("contexts", [])
    if not isinstance(raw_contexts, list):
        raise ConfigError("[[contexts]]: expected an array of tables")
    contexts = tuple(
        _parse_context(c, i, groups) for i, c in enumerate(raw_contexts)
    )

    seen: set[str] = set()
    for ctx in contexts:
        if ctx.name in seen:
            raise ConfigError(f"duplicate context name {ctx.name!r}")
        seen.add(ctx.name)
    if "default" in seen:
        raise ConfigError(
            "context name 'default' is reserved for the no-context fallback"
        )

    env, forward = _parse_env(data.get("env"), "[env]")

    return Config(
        setup=_parse_setup(data.get("setup")),
        reaper=_parse_reaper(data.get("reaper")),
        mounts=global_mounts,
        contexts=contexts,
        env=env,
        forward=forward,
    )


# --- file I/O ----------------------------------------------------------------


def load_config(path: str | os.PathLike[str]) -> Config:
    """Read, parse and validate a config file at *path*.

    Raises :class:`ConfigError` if the file is missing or malformed.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {p}") from e
    except OSError as e:
        raise ConfigError(f"cannot read config file {p}: {e}") from e
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"malformed TOML in {p}: {e}") from e
    return parse_config(data, source=str(p))


def ensure_user_config(config_dir: str | os.PathLike[str] | None = None) -> Path:
    """Write the documented default ``config.toml`` + ``provision.sh`` if absent.

    Idempotent: never overwrites an existing file. Returns the config.toml path.
    """
    d = Path(config_dir) if config_dir is not None else user_config_dir()
    d.mkdir(parents=True, exist_ok=True)
    config_path = d / "config.toml"
    provision_path = d / "provision.sh"
    if not config_path.exists():
        config_path.write_text(_DEFAULT_CONFIG_TOML)
    if not provision_path.exists():
        provision_path.write_text(_DEFAULT_PROVISION_SH)
        provision_path.chmod(0o755)
    return config_path


def load_user_config() -> Config:
    """Convenience: create defaults on first run, then load the user config."""
    return load_config(ensure_user_config())


# --- shipped defaults --------------------------------------------------------

_DEFAULT_CONFIG_TOML = """\
# claude-wrapper configuration. See DESIGN.md §7 for the full reference.
# Host paths use ~ expansion. Paths absent on this machine are silently skipped.

# --- Variables (§7.1) ---------------------------------------------------------
# ${NAME} is substituted into every string below *before* ~ expansion, to keep
# repeated path prefixes DRY. Brace form only (a bare $NAME is left literal).
# Vars cannot reference other vars (single level, no recursion).
# [vars]
# WM = "~/.config/claude-wrapper/work-mappings"

[setup]
# apt packages installed into claude-base (inherited by every instance).
packages = ["jq"]
# Optional script run on claude-base as root (set -e). Re-run on every `setup`.
provision_script = "~/.config/claude-wrapper/provision.sh"

[reaper]
stop_idle_after     = "30m"   # running + idle this long  -> stop
delete_unused_after = "14d"   # not used this long        -> delete instance
max_instances       = 0       # 0 = unlimited; else LRU-delete beyond this

# --- Environment (§7.3) -------------------------------------------------------
# Extra env passed into the sandbox at `exec claude`, on top of the always-
# forwarded baseline (terminal/locale, IDE hints, ANTHROPIC_*/CLAUDE_*/AWS_*).
# Run-path-only: applied at launch, never baked in, so an [env] edit never
# rebuilds or recreates an instance. Literal KEY = "value" sets it verbatim
# (${VAR} from [vars] expands; no ~ expansion); the reserved `forward` key lists
# host var names passed through by value (an unset host var is skipped). A
# per-context `env` overrides the global one on a key collision.
# HOME/USER/PATH are reserved and rejected.
# [env]
# EDITOR  = "vim"
# forward = ["GH_TOKEN"]

# --- Global persistent mounts: baked into claude-base, inherited everywhere ---
# `path` is the mount location (host & container identical). `from` aliases a
# different host backing dir. Default mode is rw; mark credentials `ro`.

# Claude's auth/history/config — shared across every instance (DESIGN §10).
[[mounts]]
path = "~/.claude"
[[mounts]]
path = "~/.claude.json"

# Example read-only credential mount:
# [[mounts]]
# path = "~/.aws"
# mode = "ro"

# --- Mount groups (§7.2) ------------------------------------------------------
# A reusable, named bundle of mounts that several contexts can `include` — so a
# shared credential set isn't duplicated across contexts. A group is NOT a
# context: no `when`, no template, never matched by resolution. Each entry under
# `mounts` is parsed exactly like a [[contexts.mounts]] table.
# [mount_groups.acme-creds]
# mounts = [
#   { path = "~/.ssh",       from = "${WM}/.ssh",       mode = "ro" },
#   { path = "~/.gnupg",     from = "${WM}/.gnupg",     mode = "ro" },
#   { path = "~/.gitconfig", from = "${WM}/.gitconfig", mode = "ro" },
# ]

# --- Contexts: cwd-prefix-selected templates with their own mounts (§6/§7) ----
# `name` is required (container = claude-sandbox-<name>). `when` is a list of
# host path prefixes (OR); the longest matching prefix across all contexts wins.
# `include` splices in mount groups (a list, or a bare string for one); a
# context's own inline [[contexts.mounts]] override an included mount with the
# same `path` (later wins).
#
# [[contexts]]
# name    = "api"
# when    = ["~/work/acme-api"]
# include = ["acme-creds"]
# provision_script = "~/.config/claude-wrapper/provision-api.sh"
# env     = { DEPLOY_ENV = "work", forward = ["WORK_TOKEN"] }  # overrides global [env]
#   [[contexts.mounts]]
#   path    = "~/work"  # whole-tree mount (broad)
#   exclude = ["secrets"]  # masked: appears as an empty read-only dir
"""

_DEFAULT_PROVISION_SH = """\
#!/usr/bin/env bash
# claude-wrapper global provision script.
# Runs on claude-base as root with `set -e`; output is streamed and a non-zero
# exit fails `setup` loudly. Re-run on every `setup` (which rebuilds base).
#
# Put apt-repo setup, pip/npm globals, dotfile bootstrap, etc. here. Prefer
# `[setup].packages` in config.toml for plain apt packages.
set -euo pipefail

# Example:
# apt-get install -y --no-install-recommends some-package
"""
