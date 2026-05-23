"""Container lifecycle (DESIGN §4/§10): the 3-tier CoW hierarchy.

This module owns the *internal* (mechanism) provisioning — identity rename,
idmap, apparmor, DNS-wait, claude install — and the tier orchestration.

* ``build_base`` (T4) — build the frozen tier-1 ``claude-base`` per §3/§11/§12.
* ``setup`` (T4) — the ``setup`` subcommand entry point; T5 extends it with
  ``build_templates`` + context pruning, T8/T10 with the stamp + reaper.
* ``build_templates`` (T5), ``run`` + stamp drift (T8), reaper/gc/delete (T10).
"""

from __future__ import annotations

import getpass
import json
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import incus, provision
from .config import (
    SCHEMA_VERSION,
    Config,
    Context,
    MountSpec,
    ensure_user_config,
    load_config,
)
from .mounts import ensure_mask_dir, mask_container_paths, resolve, scope_hash

if TYPE_CHECKING:  # avoid a run-time cli<->lifecycle import cycle
    from .cli import Mount

# Tier 1: the frozen base. Tier-2 templates CoW-copy from it (T5); it is built
# (started, provisioned) by setup, then stopped and never run again.
BASE = "claude-base"
IMAGE = "images:ubuntu/24.04"

# Tier-2 template naming: claude-sandbox-<ctx>. Tier-3 instances (T8) extend
# this with -<hash8(scope)>, so we tag tier explicitly via incus `user.*` config
# rather than parsing names (a context name may itself contain dashes).
TEMPLATE_PREFIX = "claude-sandbox-"
ROLE_KEY = "user.cw-role"  # "template" (this tier) | "instance" (tier 3) | unset
CONTEXT_KEY = "user.cw-context"  # the owning context's name
LAST_USED_KEY = "user.last-used"  # epoch seconds; bumped each run, read by the reaper (T10)

# incus instance names: 2-63 chars, letters/digits/dashes, must not end in a
# dash (ours always start with "claude-", so the leading-char rule is moot).
_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]{0,61}[a-zA-Z0-9]$")

# ptrace + signal across the stacked apparmor sub-profiles. Bun/JSC sends
# SIGPWR to suspend sibling threads for stop-the-world GC; the default profile
# denies it cross-context, crashing claude (and stalling dpkg triggers). §12.
APPARMOR_RULES = "ptrace,\nsignal,\n"

# Wait budgets after the post-idmap restart (the community image has no
# cloud-init, so we poll the agent and a real DNS lookup rather than waiting on
# `cloud-init status`).
_AGENT_TIMEOUT_S = 60
_DNS_TIMEOUT_S = 30


class SetupError(Exception):
    """A setup/build failure with a user-facing message (caught by the CLI)."""


# --- subuid/subgid prerequisite (DESIGN §3) ----------------------------------


def _subid_covered(path: str, target: int) -> bool:
    """True if some ``root:start:count`` line in *path* covers id *target*."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return False
    for line in lines:
        parts = line.split(":")
        if len(parts) != 3:
            continue
        owner, start, count = parts
        if owner not in ("root", "0"):
            continue
        try:
            s, c = int(start), int(count)
        except ValueError:
            continue
        if s <= target < s + c:
            return True
    return False


def _check_subuid(host_uid: int, host_gid: int) -> None:
    """Raise :class:`SetupError` with the exact ``sudo`` fix if subids are missing.

    ``raw.idmap`` mapping host uid/gid -> 1000 needs root to own those ids as a
    sub-id range. We never run sudo ourselves — we print the command and stop.
    """
    need_uid = not _subid_covered("/etc/subuid", host_uid)
    need_gid = not _subid_covered("/etc/subgid", host_gid)
    if not (need_uid or need_gid):
        return
    if need_uid and need_gid and host_uid == host_gid:
        cmds = [f"echo 'root:{host_uid}:1' | sudo tee -a /etc/subuid /etc/subgid"]
    else:
        cmds = []
        if need_uid:
            cmds.append(f"echo 'root:{host_uid}:1' | sudo tee -a /etc/subuid")
        if need_gid:
            cmds.append(f"echo 'root:{host_gid}:1' | sudo tee -a /etc/subgid")
    cmds.append("sudo systemctl restart incus")
    raise SetupError(
        f"raw.idmap requires root to own a sub-id for host UID {host_uid} / "
        f"GID {host_gid}, but /etc/subuid or /etc/subgid lacks it.\n"
        "Run on the host, then re-run `claude-wrapper setup`:\n\n  "
        + "\n  ".join(cmds)
    )


# --- identity (DESIGN §3) ----------------------------------------------------

# Rename the stock `ubuntu` user to the host $USER and move its home to the
# exact host $HOME. The home move uses usermod while the login is still the
# NAME_REGEX-valid `ubuntu` (usermod -l rejects `@`); the rename itself is a
# field-exact edit of passwd/shadow/group/gshadow, which handles `@`. sudoers
# is keyed by UID 1000 so the (possibly `@`) name never enters sudoers, where
# `@` is netgroup syntax. `cat >file` rewrites in place, preserving the
# sensitive shadow-file mode/owner.  $1 = new username, $2 = home path.
_IDENTITY_SCRIPT = r"""
set -euo pipefail
NEWUSER="$1"
HOMEDIR="$2"

cur_home="$(getent passwd ubuntu | cut -d: -f6)"
if [ "$cur_home" != "$HOMEDIR" ]; then
    mkdir -p "$(dirname "$HOMEDIR")"
    usermod -d "$HOMEDIR" -m ubuntu
fi

if [ "$NEWUSER" != "ubuntu" ]; then
    for f in /etc/passwd /etc/shadow; do
        [ -f "$f" ] || continue
        awk -F: -v OFS=: -v old=ubuntu -v new="$NEWUSER" '$1==old{$1=new} 1' "$f" > "$f.cw" && cat "$f.cw" > "$f" && rm -f "$f.cw"
    done
    for f in /etc/group /etc/gshadow; do
        [ -f "$f" ] || continue
        awk -F: -v OFS=: -v old=ubuntu -v new="$NEWUSER" '
            $1==old { $1=new }
            {
                n=split($NF, m, ",")
                out=""
                for (i=1;i<=n;i++) { if (m[i]==old) m[i]=new; out=out (i>1?",":"") m[i] }
                $NF=out
            }
            1' "$f" > "$f.cw" && cat "$f.cw" > "$f" && rm -f "$f.cw"
    done
fi

echo '#1000 ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/claude-wrapper
chmod 0440 /etc/sudoers.d/claude-wrapper

install -d -o 1000 -g 1000 -m 755 "$HOMEDIR/.local" "$HOMEDIR/.local/bin"
"""


def _setup_identity(host_user: str, home: str) -> None:
    incus.exec_(BASE, ["bash", "-c", _IDENTITY_SCRIPT, "identity", host_user, home])


# --- bootstrap waits ---------------------------------------------------------


def _wait_for_agent(container: str, *, timeout: int = _AGENT_TIMEOUT_S) -> None:
    """Poll until the incus agent accepts exec (quietly: a sentinel echo)."""
    for _ in range(timeout):
        if incus.exec_(container, ["echo", "ok"], capture=True, check=False).strip() == "ok":
            return
        time.sleep(1)
    raise SetupError(f"{container}: agent did not become ready within {timeout}s")


def _wait_for_dns(container: str, *, host: str = "claude.ai", timeout: int = _DNS_TIMEOUT_S) -> None:
    """Poll a real lookup until DNS resolves (no cloud-init to wait on)."""
    for _ in range(timeout):
        if incus.exec_(container, ["getent", "hosts", host], capture=True, check=False).strip():
            return
        time.sleep(1)
    print(f"warning: DNS for {host!r} did not resolve in {container}; "
          "network-dependent steps may fail.")


# --- claude install (DESIGN §11/§12) -----------------------------------------


def _detect_install_method(home: str) -> str:
    """Mirror the host's ``installMethod`` so the container layout matches."""
    try:
        with open(Path(home) / ".claude.json") as f:
            return json.load(f).get("installMethod") or "native"
    except Exception:
        return "native"


def _install_claude(host_user: str, home: str, method: str) -> None:
    claude_path = f"{home}/.local/bin/claude" if method == "native" else "/usr/bin/claude"
    if incus.exec_(BASE, ["test", "-x", claude_path], check=False) == 0:
        print(f"claude already present at {claude_path}.")
        return
    print(f"Installing claude ({method})...")
    # The community images:ubuntu/24.04 image is minimal and lacks curl.
    incus.exec_(
        BASE,
        ["bash", "-c",
         "command -v curl >/dev/null 2>&1 || { apt-get update -qq && "
         "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates; }"],
    )
    if method == "native":
        incus.exec_(
            BASE,
            ["bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"],
            uid=1000, gid=1000, cwd=home, env={"HOME": home, "USER": host_user},
        )
    else:
        incus.exec_(
            BASE,
            ["bash", "-c",
             "set -e; curl -fsSL https://deb.nodesource.com/setup_20.x | bash -; "
             "DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs; "
             "npm install -g @anthropic-ai/claude-code"],
        )


# --- mounts ------------------------------------------------------------------


def _mount_device_name(spec: MountSpec) -> str:
    """Stable, collision-free device name keyed on the container-side path.

    Deterministic so the same mount keeps its name across base/template/instance
    (devices propagate by name down the CoW chain — DESIGN §4).
    """
    import hashlib

    return "mnt-" + hashlib.md5(spec.path.encode()).hexdigest()[:8]


def _mask_device_name(container_path: str) -> str:
    """Device name for the empty-RO overmount masking *container_path* (§8).

    The ``msk-`` prefix sorts after every ``mnt-`` parent device by name, which
    — together with incus's path-depth mount ordering — guarantees a mask lands
    *on top of* its parent mount, never under it.
    """
    import hashlib

    return "msk-" + hashlib.md5(container_path.encode()).hexdigest()[:8]


def _add_mount_devices(container: str, mounts: tuple[MountSpec, ...]) -> None:
    """Add persistent bind-mount disk devices for *mounts*; skip absent sources.

    Per §7, host paths absent on this machine are skipped. Each ``spec.exclude``
    sub-path gets a nested empty read-only overmount (§8): a disk device sourcing
    the shared empty dir (mode 555) at the excluded container path, added *after*
    its parent so incus stacks the mask on top — the masked path then appears as
    an empty, unwritable directory inside.
    """
    skipped: list[str] = []
    mask_src: str | None = None
    for spec in mounts:
        src = spec.host_path
        if not os.path.exists(src):
            skipped.append(src)
            continue
        props: dict[str, object] = {"source": src, "path": spec.path}
        if spec.mode == "ro":
            props["readonly"] = True
        incus.device_add(container, _mount_device_name(spec), "disk", **props)
        for cpath in mask_container_paths(spec):
            if mask_src is None:
                mask_src = ensure_mask_dir()  # lazy: only when something is excluded
            incus.device_add(
                container, _mask_device_name(cpath), "disk",
                source=mask_src, path=cpath, readonly=True,
            )
    if skipped:
        print("Skipped absent mount sources:\n  " + "\n  ".join(skipped))


# --- tier 1: base build (DESIGN §3/§11/§12) ----------------------------------


def build_base(
    cfg: Config,
    *,
    host_user: str,
    host_uid: int,
    host_gid: int,
    home: str,
) -> None:
    """Build the frozen tier-1 ``claude-base`` (delete-and-recreate; §4).

    Safe to repeat: the base holds no unique state (config lives in the host
    bind-mount sources), so each ``setup`` rebuilds it from scratch.
    """
    _check_subuid(host_uid, host_gid)

    if incus.container_exists(BASE):
        print(f"Rebuilding {BASE} (delete + recreate)...")
        incus.delete(BASE)

    print(f"Launching {BASE} from {IMAGE}...")
    incus.launch(IMAGE, BASE)

    # idmap + apparmor must be set, then applied with a forced restart (the
    # just-launched init may not honour a clean shutdown yet).
    incus.set_idmap(BASE, f"uid {host_uid} 1000\ngid {host_gid} 1000")
    incus.set_apparmor(BASE, APPARMOR_RULES)
    incus.cli_run("restart", "--force", BASE)

    _wait_for_agent(BASE)
    _wait_for_dns(BASE)

    print(f"Configuring identity: user={host_user!r}, home={home!r}...")
    _setup_identity(host_user, home)

    _install_claude(host_user, home, _detect_install_method(home))

    provision.install_packages(BASE, cfg.setup.packages)
    provision.run_provision_script(BASE, cfg.setup.provision_script, label="global")

    _add_mount_devices(BASE, cfg.mounts)

    print(f"Stopping {BASE} (frozen CoW source; never run again)...")
    incus.stop(BASE)
    print(f"{BASE} ready.")


# --- tier 2: context templates (DESIGN §4/§11) -------------------------------


def _template_name(ctx_name: str) -> str:
    return f"{TEMPLATE_PREFIX}{ctx_name}"


def _check_template_name(ctx_name: str) -> None:
    """Reject a context name that yields an illegal incus instance name."""
    name = _template_name(ctx_name)
    if not _NAME_RE.match(name):
        raise SetupError(
            f"context {ctx_name!r} yields invalid instance name {name!r}: "
            "context names may contain only ASCII letters, digits and dashes "
            "(no underscores/spaces), and the full name must be 2-63 chars and "
            "not end with a dash."
        )


def _provision_template(name: str, ctx: Context) -> None:
    """Transiently start *name* (setup only) to run its per-context script (§4).

    A template is otherwise never started; this is the sole exception and the
    only way to ``incus exec`` the script. ``finally: stop`` guarantees the
    template returns to STOPPED even if the script fails (which still aborts
    setup loudly — ``run_provision_script`` raises on a non-zero exit).
    """
    print(f"Starting {name} transiently to run its provision script...")
    incus.start(name)
    try:
        _wait_for_agent(name)
        _wait_for_dns(name)
        provision.run_provision_script(
            name, ctx.provision_script, label=f"context {ctx.name!r}"
        )
    finally:
        incus.stop(name)


def _build_template(ctx: Context) -> None:
    """Build one tier-2 template by CoW of base + context mounts + provision.

    Delete-and-recopy (templates hold no unique state). A template that is
    somehow running is skipped with a warning rather than clobbered (§4).
    """
    name = _template_name(ctx.name)
    if incus.is_running(name):
        print(f"warning: template {name} is running; skipping rebuild "
              "(stop it, then re-run `claude-wrapper setup`).")
        return
    if incus.container_exists(name):
        print(f"Rebuilding template {name} (delete + recopy)...")
        incus.delete(name)

    print(f"Building template {name} (CoW of {BASE})...")
    incus.copy(BASE, name)  # inherits idmap/apparmor/global mounts; stays STOPPED
    incus.config_set(name, ROLE_KEY, "template")
    incus.config_set(name, CONTEXT_KEY, ctx.name)
    _add_mount_devices(name, ctx.mounts)  # exclude-masking is T7
    if ctx.provision_script:
        _provision_template(name, ctx)
    print(f"Template {name} ready (STOPPED).")


def _prune_templates(configured: set[str]) -> None:
    """Delete tier-2 templates whose context was removed from config (§4).

    Identifies templates by the ``user.cw-role`` tag (not by name) in a single
    listing call; a running template is skipped with a warning.
    """
    for inst in incus.list_instances():
        conf = inst.get("config") or {}
        if conf.get(ROLE_KEY) != "template":
            continue
        ctx_name = conf.get(CONTEXT_KEY)
        if ctx_name in configured:
            continue  # still configured — rebuilt by the build loop
        name = inst.get("name", "")
        if inst.get("status") == "Running":
            print(f"warning: template {name} (removed context {ctx_name!r}) is "
                  "running; skipping prune.")
            continue
        print(f"Pruning template {name} (context {ctx_name!r} removed from config)...")
        incus.delete(name)


def build_templates(cfg: Config) -> None:
    """Build/refresh every configured context's tier-2 template; prune the rest.

    Requires ``claude-base`` to exist (built first by :func:`build_base`).
    Templates inherit identity/idmap/apparmor/global mounts from base via the
    CoW copy, so no identity arguments are needed here.
    """
    for ctx in cfg.contexts:
        _check_template_name(ctx.name)
    _prune_templates({c.name for c in cfg.contexts})
    for ctx in cfg.contexts:
        _build_template(ctx)


# --- setup entry point (DESIGN §9) -------------------------------------------


def setup(cfg: Config | None = None) -> int:
    """The ``setup`` subcommand: unconditional, idempotent full provision.

    Builds the base (T4), the context templates + prunes removed ones (T5), and
    writes the config stamp (T8) so the next normal run takes the fast path. The
    reaper pass is T10.
    """
    config_path = ensure_user_config()
    if cfg is None:
        cfg = load_config(config_path)
    host_user = os.environ.get("USER") or getpass.getuser()
    host_uid = os.getuid()
    host_gid = os.getgid()
    home = os.environ.get("HOME") or os.path.expanduser("~")
    build_base(cfg, host_user=host_user, host_uid=host_uid, host_gid=host_gid, home=home)
    build_templates(cfg)
    _write_stamp(_config_stamp(config_path))
    n = len(cfg.contexts)
    print(f"setup: base + {n} context template(s) complete; stamp written. "
          "(reaper lands in T10.)")
    return 0


# --- run path: stamp drift, instance lifecycle, exec claude (DESIGN §9/§10) ---


def _state_dir() -> Path:
    """``$XDG_STATE_HOME/claude-wrapper`` (falling back to ``~/.local/state``).

    Holds local, regenerable run-path state (the config stamp now; T10's
    ``last-reap`` stamp later) — not config, so it lives outside the config dir.
    """
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.environ.get("HOME") or os.path.expanduser("~"), ".local", "state"
    )
    return Path(base) / "claude-wrapper"


def _stamp_path() -> Path:
    return _state_dir() / "stamp"


def _config_stamp(config_path: str | os.PathLike[str]) -> str:
    """``hash(schema_version + config.toml bytes)`` (DESIGN §10).

    A cheap local fingerprint: a schema bump or any config edit changes it, so
    the next run re-provisions exactly once.
    """
    import hashlib

    h = hashlib.md5()
    h.update(f"{SCHEMA_VERSION}\n".encode())
    h.update(Path(config_path).read_bytes())
    return h.hexdigest()


def _read_stamp() -> str | None:
    try:
        return _stamp_path().read_text().strip()
    except OSError:
        return None


def _write_stamp(value: str) -> None:
    p = _stamp_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value + "\n")


# Terminal/locale vars forwarded so the TUI renders correctly; any ANTHROPIC_*/
# CLAUDE_* host vars are forwarded too (API key, feature flags) — auth itself
# still comes from the bind-mounted ~/.claude.json.
_FORWARD_ENV = (
    "TERM", "COLORTERM", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
    "LC_MESSAGES", "LC_TIME", "LC_NUMERIC", "LC_COLLATE", "LC_MONETARY",
)
_FORWARD_PREFIXES = ("ANTHROPIC_", "CLAUDE_")


def _exec_env(host_user: str, home: str) -> dict[str, str]:
    """Env for ``exec claude``: identity, ``~/.local/bin`` PATH, forwarded vars.

    PATH prepends ``$HOME/.local/bin`` (where the native installer puts claude;
    DESIGN §12). HOME/USER are set explicitly even though the renamed identity
    already matches, so the exec never relies on incus's env defaults.
    """
    env = {
        "HOME": home,
        "USER": host_user,
        "PATH": f"{home}/.local/bin:/usr/local/sbin:/usr/local/bin:"
                "/usr/sbin:/usr/bin:/sbin:/bin",
    }
    for key, val in os.environ.items():
        if key in _FORWARD_ENV or key.startswith(_FORWARD_PREFIXES):
            env.setdefault(key, val)
    return env


def _add_session_mounts(instance: str, session_mounts: "list[Mount]") -> None:
    """Add ad-hoc ``--mount`` modifiers as idempotent disk devices (DESIGN §9).

    Caveat: these are per-invocation, but instances are scope-shared and
    persistent, so a session mount lingers on the instance for later sessions in
    the same scope. Accepted (the user opted in); we only add devices not already
    present, so re-runs are no-ops and never error.
    """
    if not session_mounts:
        return
    specs = tuple(
        MountSpec(path=os.path.abspath(os.path.expanduser(m.path)), mode=m.mode)
        for m in session_mounts
    )
    existing = incus.device_show(instance)
    new = tuple(s for s in specs if _mount_device_name(s) not in existing)
    if new:
        _add_mount_devices(instance, new)


def _ensure_instance(
    instance: str,
    source: str,
    *,
    ctx_name: str,
    scope: str,
    add_project_mount: bool,
) -> None:
    """Ensure tier-3 *instance* exists (CoW of *source*) and is running (§4/§10).

    Cold path (missing): CoW-copy from the context template (or ``claude-base``
    for the *default* context), tag its tier, add the per-cwd project mount
    unless the cwd is subsumed by a context mount, start, and wait for the agent
    + DNS. Warm path: start it if stopped. Either way it is left running.
    """
    info = incus.instance_info(instance)
    if info is None:
        if not incus.container_exists(source):
            raise SetupError(
                f"source container {source!r} is missing — run "
                "`claude-wrapper setup` to (re)build the base/templates."
            )
        print(f"Creating instance {instance} (CoW of {source})...")
        incus.copy(source, instance)
        incus.config_set(instance, ROLE_KEY, "instance")
        incus.config_set(instance, CONTEXT_KEY, ctx_name)
        if add_project_mount:
            _add_mount_devices(instance, (MountSpec(path=scope),))
        incus.start(instance)
        _wait_for_agent(instance)
        _wait_for_dns(instance)
        return
    if info.get("status") != "Running":
        print(f"Starting instance {instance}...")
        incus.start(instance)
        _wait_for_agent(instance)


def run(session_mounts: "list[Mount]", passthrough: list[str]) -> int:
    """The run path (DESIGN §9/§10): stamp → resolve → instance → ``exec claude``.

    Returns claude's own exit code. The instance is left running on exit; the
    reaper that later stops/deletes idle instances is T10.
    """
    config_path = ensure_user_config()
    cfg = load_config(config_path)

    # Stamp drift (config edited, or first run) → exactly one auto-setup (§10).
    if _read_stamp() != _config_stamp(config_path):
        print("claude-wrapper: config changed (or first run) — running setup.")
        setup(cfg)  # rebuilds base/templates and rewrites the stamp

    host_user = os.environ.get("USER") or getpass.getuser()
    home = os.environ.get("HOME") or os.path.expanduser("~")
    cwd = os.getcwd()

    res = resolve(cwd, cfg, home=home)  # RefuseError on a disallowed cwd (§8)

    instance = f"{_template_name(res.context_name)}-{scope_hash(res.scope)}"
    source = BASE if res.context is None else _template_name(res.context_name)

    _ensure_instance(
        instance,
        source,
        ctx_name=res.context_name,
        scope=res.scope,
        add_project_mount=res.add_project_mount,
    )
    _add_session_mounts(instance, session_mounts)
    incus.config_set(instance, LAST_USED_KEY, str(int(time.time())))

    return incus.exec_(
        instance,
        ["claude", *passthrough],
        uid=1000,
        gid=1000,
        cwd=cwd,
        env=_exec_env(host_user, home),
        check=False,
    )
