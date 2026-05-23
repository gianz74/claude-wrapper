"""incus CLI helpers (DESIGN §2). incus-only — no LXD/lxc.

A thin *mechanism* layer over the ``incus`` binary: it knows how to invoke the
CLI and parse its output, but holds no policy (which image, which mappings,
which devices) — that lives in :mod:`lifecycle` / :mod:`mounts`.

Public surface:

* :class:`IncusError` — any ``incus`` invocation that fails.
* :func:`cli_run` / :func:`cli_quiet` — generic runners (streamed vs captured).
* :func:`instance_info` / :func:`container_exists` / :func:`is_running` —
  state queries (via the REST API over ``incus query``, so output is JSON).
* :func:`launch` / :func:`start` / :func:`stop` / :func:`delete` / :func:`copy`
  — instance lifecycle (CoW copy is implicit on a CoW storage pool).
* :func:`exec_` — run a command inside an instance.
* :func:`device_show` / :func:`device_exists` / :func:`device_add` /
  :func:`device_remove` — disk/proxy device management. ``device_show`` is
  cached per process run (one daemon call), invalidated on add/remove, to keep
  the hot path's daemon-call count low (DESIGN §15.2).
* :func:`config_set` / :func:`config_get` / :func:`set_idmap` /
  :func:`set_apparmor` — instance config (thin wrappers; the exact mapping /
  apparmor rules are the caller's policy).
"""

from __future__ import annotations

import json
import subprocess

INCUS = "incus"


class IncusError(RuntimeError):
    """An ``incus`` CLI invocation failed (non-zero exit, or binary missing)."""


# --- generic runners ---------------------------------------------------------


def _run(
    args: list[str], *, capture: bool, check: bool, stdin_text: str | None
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            [INCUS, *args],
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            text=True,
            input=stdin_text,
        )
    except FileNotFoundError as e:
        raise IncusError(
            f"`{INCUS}` not found on PATH — is incus installed? "
            "(Ubuntu: `apt install incus`)"
        ) from e
    if check and proc.returncode != 0:
        msg = f"incus {' '.join(args)} failed (exit {proc.returncode})"
        if capture and proc.stderr:
            msg += f": {proc.stderr.strip()}"
        raise IncusError(msg)
    return proc


def cli_run(*args: str, check: bool = True, stdin_text: str | None = None) -> int:
    """Run ``incus <args>`` with stdout/stderr inherited (streamed to the user).

    Use for visible/long operations (launch, copy, exec, apt). Returns the
    process return code; raises :class:`IncusError` on failure when *check*.
    """
    return _run(list(args), capture=False, check=check, stdin_text=stdin_text).returncode


def cli_quiet(*args: str, check: bool = True, stdin_text: str | None = None) -> str:
    """Run ``incus <args>`` capturing stdout (text) and return it.

    Use for queries whose output we parse and don't want on the terminal.
    Raises :class:`IncusError` on failure when *check*.
    """
    return _run(list(args), capture=True, check=check, stdin_text=stdin_text).stdout


def _prop(value: object) -> str:
    """Render a config/device value the way the incus CLI expects it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# --- state queries -----------------------------------------------------------


def instance_info(name: str) -> dict | None:
    """Return the instance's REST object (JSON) or ``None`` if it doesn't exist.

    Uses ``incus query /1.0/instances/<name>`` so the result is parseable with
    stdlib :mod:`json` (no YAML dependency). Includes ``config``, ``devices``
    (local, non-inherited), ``status``, etc.
    """
    proc = _run(
        ["query", f"/1.0/instances/{name}"],
        capture=True,
        check=False,
        stdin_text=None,
    )
    if proc.returncode != 0:
        return None
    try:
        obj = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) and obj.get("name") == name else None


def container_exists(name: str) -> bool:
    return instance_info(name) is not None


def is_running(name: str) -> bool:
    info = instance_info(name)
    return info is not None and info.get("status") == "Running"


# --- instance lifecycle ------------------------------------------------------


def launch(image: str, name: str, *, extra_args: list[str] | None = None) -> int:
    """``incus launch <image> <name>`` — create *and start* a new instance."""
    return cli_run("launch", image, name, *(extra_args or []))


def start(name: str) -> int:
    return cli_run("start", name)


def stop(name: str, *, force: bool = True) -> int:
    args = ["stop", name]
    if force:
        args.append("--force")
    return cli_run(*args)


def delete(name: str, *, force: bool = True, check: bool = True) -> int:
    args = ["delete", name]
    if force:
        args.append("--force")
    rc = cli_run(*args, check=check)
    invalidate_cache(name)
    return rc


def copy(src: str, dst: str, *, extra_args: list[str] | None = None) -> int:
    """``incus copy <src> <dst>`` — CoW on a copy-on-write storage pool.

    Used to materialise tier-2 templates from ``claude-base`` and tier-3
    instances from a template; the copy inherits the source's devices in one
    daemon call (DESIGN §4).
    """
    return cli_run("copy", src, dst, *(extra_args or []))


def exec_(
    name: str,
    command: list[str],
    *,
    uid: int | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
    check: bool = True,
    stdin_text: str | None = None,
) -> int | str:
    """Run *command* inside instance *name* (``incus exec``).

    ``uid`` maps to ``--user`` (the numeric UID keys the exec path, never the
    possibly-``@`` username — DESIGN §3). With *capture* the stdout string is
    returned; otherwise output is streamed and the return code is returned.
    """
    args = ["exec", name]
    if uid is not None:
        args += ["--user", str(uid)]
    if cwd is not None:
        args += ["--cwd", cwd]
    for key, val in (env or {}).items():
        args += ["--env", f"{key}={val}"]
    args += ["--", *command]
    if capture:
        return cli_quiet(*args, check=check, stdin_text=stdin_text)
    return cli_run(*args, check=check, stdin_text=stdin_text)


# --- device management (cached show) -----------------------------------------

# One parsed `devices` dict per instance name, populated lazily and invalidated
# on add/remove, so repeated existence checks cost a single daemon call.
_device_cache: dict[str, dict[str, dict[str, str]]] = {}


def invalidate_cache(name: str | None = None) -> None:
    """Drop the cached device view for *name* (or all names if ``None``)."""
    if name is None:
        _device_cache.clear()
    else:
        _device_cache.pop(name, None)


def device_show(name: str, *, refresh: bool = False) -> dict[str, dict[str, str]]:
    """Return ``{device_name: {key: value}}`` for *name*'s local devices.

    Cached per process run (DESIGN §15.2); pass *refresh* to force a re-query.
    Returns ``{}`` if the instance doesn't exist.
    """
    if refresh or name not in _device_cache:
        info = instance_info(name)
        _device_cache[name] = dict(info.get("devices", {})) if info else {}
    return _device_cache[name]


def device_exists(name: str, device: str) -> bool:
    return device in device_show(name)


def device_add(name: str, device: str, dtype: str, **props: object) -> int:
    """``incus config device add <name> <device> <dtype> key=value ...``.

    e.g. ``device_add(inst, "proj", "disk", source="/p", path="/p",
    readonly=True)`` or a ``proxy`` device for MCP loopback ports (DESIGN §12).
    """
    args = ["config", "device", "add", name, device, dtype]
    args += [f"{key}={_prop(val)}" for key, val in props.items()]
    rc = cli_run(*args)
    invalidate_cache(name)
    return rc


def device_remove(name: str, device: str, *, check: bool = True) -> int:
    rc = cli_run("config", "device", "remove", name, device, check=check)
    invalidate_cache(name)
    return rc


# --- instance config ---------------------------------------------------------


def config_set(name: str, key: str, value: object) -> int:
    return cli_run("config", "set", name, key, _prop(value))


def config_get(name: str, key: str) -> str:
    return cli_quiet("config", "get", name, key).strip()


def set_idmap(name: str, mapping: str) -> int:
    """Set ``raw.idmap`` (host→1000 file-ownership parity; DESIGN §3).

    The *mapping* string is the caller's policy, e.g. ``"both 1001 1000"`` or a
    multi-line ``"uid 1001 1000\\ngid 1001 1000"``.
    """
    return config_set(name, "raw.idmap", mapping)


def set_apparmor(name: str, rules: str) -> int:
    """Set ``raw.apparmor`` (ptrace+signal for the Bun SIGPWR crash; §12)."""
    return config_set(name, "raw.apparmor", rules)
