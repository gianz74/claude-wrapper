"""Container lifecycle (DESIGN §4/§10): the 3-tier CoW hierarchy.

This module owns the *internal* (mechanism) provisioning — identity rename,
idmap, apparmor, DNS-wait, claude install — and the tier orchestration.

* ``build_base`` (T4) — build the frozen tier-1 ``claude-base`` per §3/§11/§12.
* ``setup`` (T4) — the ``setup`` subcommand entry point; extended with
  ``build_templates`` + context pruning (T5), the config stamp (T8), and a
  closing reaper pass (T10).
* ``build_templates`` (T5), ``run`` + stamp drift (T8).
* ``reap`` / ``gc`` / ``delete_containers`` + amortized background reap (T10).
* Source build-identity stamping + stale-instance recreation (T12): each source
  carries a content hash of its rootfs inputs (:func:`_base_build_id` /
  :func:`_template_build_id`); :func:`_ensure_instance` recreates an instance
  CoW'd from a now-rebuilt source.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import incus, mcp, provision
from .config import (
    SCHEMA_VERSION,
    Config,
    Context,
    MountSpec,
    ReaperConfig,
    ensure_user_config,
    load_config,
)
from .mounts import (
    _is_within,
    ensure_mask_dir,
    mask_container_paths,
    resolve,
    scope_hash,
)

if TYPE_CHECKING:  # avoid a run-time cli<->lifecycle import cycle
    from .cli import Mount

# Tier 1: the frozen base. Tier-2 templates CoW-copy from it (T5); it is built
# (started, provisioned) by setup, then stopped and never run again.
BASE = "claude-base"
IMAGE = "images:ubuntu/24.04"

# Container-private claude launcher dir (DESIGN §11/§8). Lives OUTSIDE $HOME so a
# bind mount of host ~/.local/bin can never shadow it; _exec_env prepends it to
# PATH ahead of ~/.local/bin so bare `claude` resolves to the container's own
# binary even when the host ~/.local/bin is mounted over the container's.
LAUNCHER_DIR = "/usr/local/lib/claude-wrapper/bin"

# Tier-2 template naming: claude-sandbox-<ctx>. Tier-3 instances (T8) extend
# this with -<hash8(scope)>, so we tag tier explicitly via incus `user.*` config
# rather than parsing names (a context name may itself contain dashes).
TEMPLATE_PREFIX = "claude-sandbox-"
ROLE_KEY = "user.cw-role"  # "template" (this tier) | "instance" (tier 3) | unset
CONTEXT_KEY = "user.cw-context"  # the owning context's name
LAST_USED_KEY = "user.last-used"  # epoch seconds; bumped each run, read by the reaper (T10)
BUILD_KEY = "user.cw-build"  # content hash of the source's rootfs inputs; instances
                             # inherit it via `incus copy` as their built-from marker (T12)

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


# --- container-private claude launcher (DESIGN §11/§8) -----------------------

# Resolve the freshly-installed native claude and point a launcher at it from a
# dir OUTSIDE $HOME. Run as root while ~/.local/bin/claude is still the
# container's own symlink (before any host ~/.local/bin mount is attached).
# $1 = launcher dir, $2 = ~/.local/bin/claude.
_LAUNCHER_SCRIPT = r"""
set -euo pipefail
LAUNCHER_DIR="$1"
CLAUDE_LINK="$2"
target="$(readlink -f "$CLAUDE_LINK" || true)"
if [ -z "$target" ] || [ ! -x "$target" ]; then
    echo "private launcher: no executable claude at $CLAUDE_LINK (resolved '${target:-}')" >&2
    exit 1
fi
mkdir -p "$LAUNCHER_DIR"
ln -sfn "$target" "$LAUNCHER_DIR/claude"
"""


def _install_private_launcher(home: str, method: str) -> None:
    """Create the container-private claude launcher (DESIGN §11/§8).

    Only the *native* install lives under $HOME (``~/.local/bin/claude`` ->
    ``~/.local/share/claude/versions/<v>``), so only it can be shadowed by a
    host ``~/.local/bin`` mount. While ``~/.local/bin/claude`` is still the
    container's own symlink, resolve the binary and symlink it from
    :data:`LAUNCHER_DIR` (outside $HOME), which :func:`_exec_env` prepends to
    PATH ahead of ``~/.local/bin``. The non-native install (``/usr/bin/claude``)
    is not under any home mount and needs no launcher — the PATH prepend is then
    harmless (the dir simply does not exist).
    """
    if method != "native":
        return
    claude_link = f"{home}/.local/bin/claude"
    incus.exec_(
        BASE, ["bash", "-c", _LAUNCHER_SCRIPT, "launcher", LAUNCHER_DIR, claude_link]
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


# --- source build identity (DESIGN §4/§10, T12) -----------------------------
#
# Each source (claude-base, each claude-sandbox-<ctx> template) is stamped with
# a content hash of the inputs that define its rootfs. A tier-3 instance CoW'd
# from a source inherits the tag, so the run path can tell whether the instance
# was built from a *now-rebuilt* source and recreate it — otherwise a
# [setup].packages / provision_script / context-mount change never reaches an
# already-created per-cwd instance (the 2026-05-24 missing-`git` bug).
#
# The run path reads the tag *as stamped on the source*, never recomputes the
# hash: a provision-script edit changes the hash but does NOT drift the config
# stamp (so no auto-setup), so recomputing locally would flag every run as stale
# and recreate forever. Reading what `setup` actually stamped means the tag only
# changes when `setup` rebuilds the source — no such loop.


def _read_provision(path: str | None) -> str:
    """Provision-script *contents* (a rootfs input); absent/None/unreadable -> ''."""
    if not path:
        return ""
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def _mount_inputs(mounts: tuple[MountSpec, ...]) -> list:
    """Stable, JSON-serialisable view of mount specs for hashing."""
    return [[m.path, m.from_, m.mode, list(m.exclude)] for m in mounts]


def _base_build_id(cfg: Config) -> str:
    """Content hash of claude-base's rootfs inputs (DESIGN §4/§10/§11).

    Hashes the global packages, the global provision-script *content*, and the
    global mounts. claude's own version is intentionally *not* an input — it is
    frozen/pinned by the base model (§11), refreshed only by a full rebuild — so
    a setup that only re-pulls the same inputs leaves the id unchanged (no churn).
    """
    import hashlib

    payload = json.dumps(
        {
            "schema": SCHEMA_VERSION,
            "packages": list(cfg.setup.packages),
            "provision": _read_provision(cfg.setup.provision_script),
            "mounts": _mount_inputs(cfg.mounts),
        },
        sort_keys=True,
    )
    return hashlib.md5(payload.encode()).hexdigest()


def _template_build_id(base_id: str, ctx: Context) -> str:
    """Content hash for a context template (DESIGN §4/§10).

    Folds in *base_id* (so a base rebuild cascades to every template, hence every
    instance) plus the context's own inputs — mounts + provision-script content —
    so editing one context recreates only that context's instances, not all.
    """
    import hashlib

    payload = json.dumps(
        {
            "base": base_id,
            "name": ctx.name,
            "provision": _read_provision(ctx.provision_script),
            "mounts": _mount_inputs(ctx.mounts),
        },
        sort_keys=True,
    )
    return hashlib.md5(payload.encode()).hexdigest()


def _instance_is_stale(instance_build: str | None, source_build: str | None) -> bool:
    """Pure drift decision: was the instance CoW'd from an older build of its source?

    Compares the instance's inherited ``user.cw-build`` against the source's
    *current* one (DESIGN §4/§10). ``source_build is None`` (the source predates
    build-stamping, e.g. a pre-T12 base) reads as 'unknown' -> not stale, so we
    never recreate on missing source info; the next ``setup`` stamps the source
    and recreation resumes. A ``None`` instance build against a stamped source is
    a pre-T12 instance -> stale (recreate to adopt the current rootfs).
    """
    if source_build is None:
        return False
    return instance_build != source_build


# --- tier 1: base build (DESIGN §3/§11/§12) ----------------------------------


def build_base(
    cfg: Config,
    *,
    host_user: str,
    host_uid: int,
    host_gid: int,
    home: str,
    build_id: str,
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

    method = _detect_install_method(home)
    _install_claude(host_user, home, method)
    # Private launcher MUST be created here — after install, before the global
    # mounts are attached — so `readlink -f ~/.local/bin/claude` still reads the
    # container's own symlink (not a host ~/.local/bin mounted over it). §11/§8.
    _install_private_launcher(home, method)

    provision.install_packages(BASE, cfg.setup.packages)
    provision.run_provision_script(BASE, cfg.setup.provision_script, label="global")

    _add_mount_devices(BASE, cfg.mounts)

    # Stamp the build identity so instances CoW'd from this base inherit it and
    # the run path can recreate them when a later setup rebuilds base (§4/§10).
    incus.config_set(BASE, BUILD_KEY, build_id)

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


def _build_template(ctx: Context, base_id: str) -> None:
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
    # Overwrite the build id inherited from base with this template's own (folds
    # in base_id + the context's inputs), so its instances recreate on drift (§4/§10).
    incus.config_set(name, BUILD_KEY, _template_build_id(base_id, ctx))
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


def build_templates(cfg: Config, base_id: str) -> None:
    """Build/refresh every configured context's tier-2 template; prune the rest.

    Requires ``claude-base`` to exist (built first by :func:`build_base`).
    Templates inherit identity/idmap/apparmor/global mounts from base via the
    CoW copy, so no identity arguments are needed here. *base_id* is base's
    current build identity, folded into each template's id (§4/§10).
    """
    for ctx in cfg.contexts:
        _check_template_name(ctx.name)
    _prune_templates({c.name for c in cfg.contexts})
    for ctx in cfg.contexts:
        _build_template(ctx, base_id)


# --- host-install checks (DESIGN §13/§11/§8) ---------------------------------


def _check_no_claude_shadow(cfg: Config, home: str) -> None:
    """Hard-refuse a config whose mount would shadow the in-container claude (§8).

    A mount whose container-side ``path`` is at or above ``~/.local/share/claude``
    (the native binary) or :data:`LAUNCHER_DIR` (the private launcher) would
    replace the container's own claude with host content and silently break
    ``exec claude``. Detect → refuse; never mutate. The path logic reuses
    :func:`mounts._is_within` — ``_is_within(protected, mount.path)`` is true
    exactly when the mount sits at or above the protected location. Mounting
    ``~/.local/bin`` alone is fine; the launcher lives outside it.
    """
    claude_share = os.path.join(home, ".local", "share", "claude")
    protected = (
        (claude_share, "the in-container claude binary (~/.local/share/claude)"),
        (LAUNCHER_DIR, "the container-private claude launcher"),
    )
    specs: list[MountSpec] = list(cfg.mounts)
    for ctx in cfg.contexts:
        specs.extend(ctx.mounts)
    for spec in specs:
        for prot, what in protected:
            if _is_within(prot, spec.path):
                raise SetupError(
                    f"mount {spec.path!r} is at or above {what} — it would shadow "
                    "the container's own claude and silently break `exec claude`. "
                    "Remove or narrow that mount. (Mounting ~/.local/bin alone is "
                    "fine; the launcher lives outside it.)"
                )


def _claude_resolves_to_wrapper(
    path_env: str,
    wrapper_path: str,
    *,
    is_exec,
    realpath,
) -> tuple[bool, str | None]:
    """Replicate the shell's first-match ``claude`` lookup over *path_env* (§13).

    Returns ``(resolves_to_wrapper, found)`` where *found* is the first
    executable ``<dir>/claude`` on the PATH (or ``None``), and the bool is true
    iff that match canonicalises to the same file as *wrapper_path*. Pure:
    *is_exec* and *realpath* are injected so the lookup is unit-testable without
    touching the filesystem.
    """
    wrapper_real = realpath(wrapper_path)
    for d in path_env.split(os.pathsep):
        if not d:
            continue
        cand = os.path.join(d, "claude")
        if is_exec(cand):
            return realpath(cand) == wrapper_real, cand
    return False, None


def _check_claude_on_path(home: str) -> None:
    """Advisory: check ``claude`` on $PATH launches the wrapper (DESIGN §13).

    Detect → print → never mutate (the :func:`_check_subuid` idiom). Silent when
    ``claude`` already resolves to the wrapper. Otherwise prints suggested
    commands — a ``claude`` symlink to the wrapper in a $PATH dir of the user's
    choosing, ordered ahead of the real binary — and flags any leftover legacy
    ``~/.local/bin/claude-wrapper.py``/``.sh``. The package never creates the
    shim, edits an rc, or deletes anything; the user decides where and runs them.
    """
    wrapper = shutil.which("claude-wrapper") or os.path.join(
        home, ".local", "bin", "claude-wrapper"
    )
    ok, found = _claude_resolves_to_wrapper(
        os.environ.get("PATH", ""),
        wrapper,
        is_exec=lambda p: os.path.isfile(p) and os.access(p, os.X_OK),
        realpath=os.path.realpath,
    )
    if ok:
        return

    lines = ["", "NOTE: `claude` does not launch the sandbox on your $PATH."]
    if found is not None:
        lines.append(f"  `claude` currently resolves to: {found}")
    else:
        lines.append("  No `claude` was found on your $PATH.")
    lines += [
        "  For `claude` to start the sandbox, symlink the wrapper into a $PATH",
        "  directory you control, ordered AHEAD of the real claude binary (any",
        "  dir works — e.g. ~/bin):",
        "",
        f"      ln -s {wrapper} <DIR>/claude",
        "",
        "  then ensure <DIR> precedes the real binary's directory on $PATH.",
    ]
    legacy = [
        p for p in (
            os.path.join(home, ".local", "bin", "claude-wrapper.py"),
            os.path.join(home, ".local", "bin", "claude-wrapper.sh"),
        )
        if os.path.exists(p)
    ]
    if legacy:
        lines += [
            "",
            "  Leftover legacy wrapper file(s) detected — remove if unused:",
            "      rm " + " ".join(legacy),
        ]
    print("\n".join(lines))


# --- setup entry point (DESIGN §9) -------------------------------------------


def setup(cfg: Config | None = None) -> int:
    """The ``setup`` subcommand: unconditional, idempotent full provision.

    Builds the base (T4), the context templates + prunes removed ones (T5),
    writes the config stamp (T8) so the next normal run takes the fast path, and
    runs a closing reaper pass over existing instances (T10, DESIGN §9).
    """
    config_path = ensure_user_config()
    if cfg is None:
        cfg = load_config(config_path)
    host_user = os.environ.get("USER") or getpass.getuser()
    host_uid = os.getuid()
    host_gid = os.getgid()
    home = os.environ.get("HOME") or os.path.expanduser("~")
    # §8 claude-shadow guard: refuse before any build work (the run path inherits
    # this refusal via stamp-drift auto-setup, so it lives in setup only).
    _check_no_claude_shadow(cfg, home)
    base_id = _base_build_id(cfg)  # stamped on base + folded into each template (§4/§10)
    build_base(
        cfg, host_user=host_user, host_uid=host_uid, host_gid=host_gid,
        home=home, build_id=base_id,
    )
    build_templates(cfg, base_id)
    _write_stamp(_config_stamp(cfg))
    result = reap(cfg)
    _write_reap_stamp(int(time.time()))
    n = len(cfg.contexts)
    print(f"setup: base + {n} context template(s) complete; stamp written"
          f"{result.summary_suffix()}.")
    # §13 advisory: tell the user how to make `claude` launch the wrapper if it
    # does not already resolve to it on $PATH (prints suggested commands only).
    _check_claude_on_path(home)
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


def _config_stamp(cfg: Config) -> str:
    """Hash of the config's *build identity* — the auto-``setup`` drift key (§10).

    Keyed on the same build-ids that decide what touches the rootfs (and so what
    T12 recreates): ``_base_build_id`` (schema + global packages + global
    provision-script *content* + global mounts) plus each context's
    ``_template_build_id`` (its mounts + provision content), sorted for
    stability. This is the *single source of truth* shared with T12's
    instance-recreation decision, so the auto-``setup`` trigger and the
    instance-staleness check can never disagree.

    Two consequences vs. the old ``hash(SCHEMA_VERSION + config.toml bytes)``:
    runtime-only edits (``[env]`` per §7.3, ``[reaper]`` thresholds) are absent
    from every build-id, so they no longer drift the stamp / force a rebuild;
    and a provision-script *content* edit now drifts it even with ``config.toml``
    byte-identical (the build-ids read the script contents), where before it was
    inert until a manual ``setup``. ``SCHEMA_VERSION`` stays covered because
    ``_base_build_id`` folds it in.
    """
    import hashlib

    base_id = _base_build_id(cfg)
    template_ids = sorted(_template_build_id(base_id, ctx) for ctx in cfg.contexts)
    payload = json.dumps({"base": base_id, "templates": template_ids}, sort_keys=True)
    return hashlib.md5(payload.encode()).hexdigest()


def _read_stamp() -> str | None:
    try:
        return _stamp_path().read_text().strip()
    except OSError:
        return None


def _write_stamp(value: str) -> None:
    p = _stamp_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value + "\n")


def _clear_stamp() -> None:
    """Remove the config stamp so the next run auto-``setup``s (used by delete)."""
    try:
        _stamp_path().unlink()
    except OSError:
        pass


# Amortized background-reap cadence (DESIGN §10): the run path triggers a
# background pass at most this often, gated by a local stamp (no daemon calls on
# the hot path). gc/setup run a pass unconditionally.
REAP_INTERVAL_S = 3600


def _reap_stamp_path() -> Path:
    return _state_dir() / "last-reap"


def _read_reap_stamp() -> int | None:
    try:
        return int(_reap_stamp_path().read_text().strip())
    except (OSError, ValueError):
        return None


def _write_reap_stamp(epoch: int) -> None:
    p = _reap_stamp_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{epoch}\n")


def _reap_due(now: int | None = None) -> bool:
    """True if no reap has run within ``REAP_INTERVAL_S`` (or ever)."""
    now = int(time.time()) if now is None else now
    last = _read_reap_stamp()
    return last is None or (now - last) > REAP_INTERVAL_S


# Host env forwarded into the exec. Terminal/locale so the TUI renders right;
# IDE hints so claude-code-ide recognises its host (§12); cloud/proxy/cert knobs
# claude honours. The matching credential *files* are exposed via config
# [[mounts]] (DESIGN §7), not hardcoded here. ANTHROPIC_*/CLAUDE_*/AWS_* are
# forwarded by prefix (covers the API key, feature flags, CLAUDE_CODE_SSE_PORT).
_FORWARD_ENV = (
    "TERM", "COLORTERM", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
    "LC_MESSAGES", "LC_TIME", "LC_NUMERIC", "LC_COLLATE", "LC_MONETARY",
    "TERM_PROGRAM", "FORCE_CODE_TERMINAL",
    "CLOUD_ML_REGION", "NODE_EXTRA_CA_CERTS", "GOOGLE_APPLICATION_CREDENTIALS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "API",
)
_FORWARD_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "AWS_")


def _exec_env(
    cfg: Config, context: "Context | None", host_user: str, home: str
) -> dict[str, str]:
    """Env for ``exec claude``: identity, claude-launcher PATH, forwarded + user vars.

    PATH prepends the container-private launcher dir (:data:`LAUNCHER_DIR`) ahead
    of ``$HOME/.local/bin`` (where the native installer puts claude; DESIGN
    §11/§12). The launcher wins even when the host ``~/.local/bin`` is mounted
    over the container's, so bare ``claude`` always resolves to the container's
    own binary. HOME/USER are set explicitly even though the renamed identity
    already matches, so the exec never relies on incus's env defaults.

    User-declared env (DESIGN §7.3) is merged broadest→narrowest, later-wins:
    identity → built-in forwarded baseline (``setdefault``) → user ``forward``
    (global ∪ context, pulled from ``os.environ``, skipped if unset) → user
    literals (global, then context overrides global; literals override forwarded)
    → identity re-asserted last so nothing clobbers HOME/USER/PATH. Env is a
    run-path concern only — it touches no rootfs and is never part of the §4
    build identity.
    """
    identity = {
        "HOME": home,
        "USER": host_user,
        "PATH": f"{LAUNCHER_DIR}:{home}/.local/bin:/usr/local/sbin:/usr/local/bin:"
                "/usr/sbin:/usr/bin:/sbin:/bin",
    }
    env = dict(identity)
    # (2) built-in forwarded baseline — setdefault so it never clobbers identity.
    for key, val in os.environ.items():
        if key in _FORWARD_ENV or key.startswith(_FORWARD_PREFIXES):
            env.setdefault(key, val)
    # (3) user `forward` = global ∪ context names, by value; unset host var skipped.
    forward_names = list(cfg.forward)
    if context is not None:
        forward_names += list(context.forward)
    for name in forward_names:
        if name in os.environ:
            env[name] = os.environ[name]
    # (4) user literals: global, then context overrides global (literals beat forwarded).
    env.update(cfg.env)
    if context is not None:
        env.update(context.env)
    # (5) re-assert identity last (config rejects HOME/USER/PATH in [env], so belt-and-suspenders).
    env.update(identity)
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
    """Ensure tier-3 *instance* exists (CoW of *source*), is current, and running.

    One ``list_instances`` call yields both the instance and its source, so the
    warm path stays within the §15.2 budget (it substitutes for the per-instance
    ``instance_info`` query — still one daemon call). Behaviour (DESIGN §4/§10):

    * **Stale** (instance's inherited ``user.cw-build`` differs from the source's
      current one — a later ``setup`` rebuilt the source): delete and recreate so
      the new packages/provision/mounts actually reach this per-cwd instance.
      **Liveness guard (T10):** never yank a *live* claude session — if the stale
      instance is running with a live session, warn and reuse it this run; it is
      recreated on the next run once idle.
    * **Missing / just-deleted:** CoW-copy from the context template (or
      ``claude-base`` for the *default* context), tag tier + context, add the
      per-cwd project mount unless the cwd is subsumed by a context mount, start,
      and wait for the agent + DNS.
    * **Warm (current):** start it if stopped. Either way it is left running.
    """
    insts = {i.get("name"): i for i in incus.list_instances()}
    info = insts.get(instance)
    source_info = insts.get(source)

    if info is not None:
        inst_build = (info.get("config") or {}).get(BUILD_KEY)
        source_build = (source_info.get("config") or {}).get(BUILD_KEY) if source_info else None
        if _instance_is_stale(inst_build, source_build):
            if info.get("status") == "Running" and _has_live_session(instance):
                print(f"warning: instance {instance} is stale (source {source} was "
                      "rebuilt) but has a live claude session; reusing it this run. "
                      "It will be recreated on the next run once idle.")
            else:
                print(f"Recreating stale instance {instance} "
                      f"(source {source} was rebuilt)...")
                incus.delete(instance, check=False)
                info = None  # fall through to the cold (re)create path below

    if info is None:
        if source_info is None and not incus.container_exists(source):
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

    # Stamp drift (build-relevant config edited, or first run) → one auto-setup
    # (§10). Keyed on build identity, so runtime-only edits ([env]/[reaper]) skip
    # this and a provision-content change triggers it (DESIGN §7.3/§10/§15.13).
    if _read_stamp() != _config_stamp(cfg):
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

    # The MCP/IDE bridge stages config files, adds loopback proxies, and runs the
    # sentinel + lockfile patch for the duration of the session, tearing it all
    # down on exit (§12). SIGTERM/SIGHUP are converted to SystemExit so its
    # context-manager cleanup still fires; normal exit and Ctrl-C already unwind
    # through it. With no --mcp-config / SSE port the bridge is a no-op.
    old_handlers = {}

    def _on_term(signum, frame):  # noqa: ANN001
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        old_handlers[sig] = signal.signal(sig, _on_term)
    try:
        with mcp.Bridge(instance, home=home) as bridge:
            argv = bridge.prepare(passthrough)
            incus.config_set(instance, LAST_USED_KEY, str(int(time.time())))
            # Amortized background reap (§10): a local stamp check (no daemon
            # calls) → detached pass while claude runs, so the hot path is
            # untouched. This session's instance was just re-stamped, so it is
            # never a reap target of its own pass.
            _maybe_background_reap()
            return incus.exec_(
                instance,
                ["claude", *argv],
                uid=1000,
                gid=1000,
                cwd=cwd,
                env=_exec_env(cfg, res.context, host_user, home),
                check=False,
            )
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


# --- reaper / gc / delete (DESIGN §9/§10) ------------------------------------


@dataclass(frozen=True)
class ReapPlan:
    """Pure reaper decision (no I/O): which instances to stop vs. delete."""

    stop: tuple[str, ...]    # running + idle past stop_idle_after
    delete: tuple[str, ...]  # unused past delete_unused_after, or LRU-trimmed


@dataclass(frozen=True)
class ReapResult:
    """What a reaper pass actually did (after the liveness guard)."""

    stopped: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    skipped_live: tuple[str, ...] = ()  # left alone: a live claude session

    def summary_suffix(self) -> str:
        """`'; stopped N, deleted M'` (or `''` when nothing happened)."""
        bits = []
        if self.stopped:
            bits.append(f"stopped {len(self.stopped)}")
        if self.deleted:
            bits.append(f"deleted {len(self.deleted)}")
        return ("; reaper " + ", ".join(bits)) if bits else ""


def _inst_name(inst: dict) -> str:
    return inst.get("name", "")


def _inst_status(inst: dict) -> str | None:
    return inst.get("status")


def _last_used_epoch(inst: dict) -> int:
    """The instance's ``user.last-used`` as epoch seconds; 0 if absent/bad.

    A missing tag sorts oldest and ages out fastest — an untagged orphan is
    treated as long-unused, which is the cleanup we want.
    """
    try:
        return int((inst.get("config") or {}).get(LAST_USED_KEY))
    except (TypeError, ValueError):
        return 0


def plan_reap(instances: list[dict], reaper: ReaperConfig, now: int) -> ReapPlan:
    """Decide stop/delete for tier-3 *instances* — pure, no daemon calls (§10).

    Three phases over the instances (already filtered to ``role=instance``):

    1. **delete** any unused longer than ``delete_unused_after``;
    2. **stop** any *running* survivor idle longer than ``stop_idle_after``;
    3. **LRU-trim**: if survivors exceed ``max_instances`` (>0), delete the
       oldest (by ``last-used``) down to the cap.

    A ``0`` threshold disables its phase (matches ``max_instances = 0`` =
    unlimited, and avoids the footgun of an always-true age comparison). The
    liveness guard that protects a running-but-live instance is applied by the
    executor :func:`reap`, not here.
    """
    delete: list[str] = []
    survivors: list[dict] = []
    for inst in instances:
        age = now - _last_used_epoch(inst)
        if reaper.delete_unused_after > 0 and age > reaper.delete_unused_after:
            delete.append(_inst_name(inst))
        else:
            survivors.append(inst)

    stop: list[str] = []
    for inst in survivors:
        age = now - _last_used_epoch(inst)
        if (_inst_status(inst) == "Running"
                and reaper.stop_idle_after > 0
                and age > reaper.stop_idle_after):
            stop.append(_inst_name(inst))

    if reaper.max_instances > 0 and len(survivors) > reaper.max_instances:
        oldest_first = sorted(survivors, key=_last_used_epoch)
        excess = len(survivors) - reaper.max_instances
        delete.extend(_inst_name(i) for i in oldest_first[:excess])

    # Delete wins over stop for any instance caught by both phases.
    delete_unique = tuple(dict.fromkeys(delete))
    delete_set = set(delete_unique)
    return ReapPlan(
        stop=tuple(n for n in stop if n not in delete_set),
        delete=delete_unique,
    )


# Dependency-free liveness probe: scan /proc for a process whose comm is
# `claude`. Avoids a procps dependency and works on the minimal base image.
_LIVE_CHECK = (
    r'for c in /proc/[0-9]*/comm; do '
    r'IFS= read -r n < "$c" 2>/dev/null && [ "$n" = claude ] && '
    r'{ echo live; exit 0; }; '
    r'done; exit 1'
)


def _has_live_session(name: str) -> bool:
    """True if a ``claude`` process is alive inside running instance *name*.

    The reaper never stops/deletes an instance with a live session (the user
    chose this guard): the session's own run re-stamps ``last-used``, so it ages
    out only once it actually goes idle. Probes via ``/proc`` so no in-image
    tooling (pgrep/procps) is required.
    """
    out = incus.exec_(name, ["sh", "-c", _LIVE_CHECK], uid=0, capture=True, check=False)
    return isinstance(out, str) and out.strip() == "live"


def _tier3_instances() -> list[dict]:
    """Every tier-3 instance (``user.cw-role=instance``) as a REST object."""
    return [
        i for i in incus.list_instances()
        if (i.get("config") or {}).get(ROLE_KEY) == "instance"
    ]


def reap(cfg: Config) -> ReapResult:
    """Run one reaper pass (DESIGN §10), honouring the live-session guard.

    Enumerates tier-3 instances in one listing call, plans stop/delete with
    :func:`plan_reap`, then executes — skipping any *running* instance that
    still has a live claude session. Always safe: instances hold no unique
    state, and a skipped live one is re-evaluated next pass.
    """
    now = int(time.time())
    instances = _tier3_instances()
    plan = plan_reap(instances, cfg.reaper, now)
    by_name = {_inst_name(i): i for i in instances}

    deleted: list[str] = []
    stopped: list[str] = []
    skipped: list[str] = []

    for name in plan.delete:
        inst = by_name.get(name)
        if inst is not None and _inst_status(inst) == "Running" and _has_live_session(name):
            skipped.append(name)
            continue
        print(f"gc: deleting unused instance {name}...")
        incus.delete(name, check=False)
        deleted.append(name)

    for name in plan.stop:
        if _has_live_session(name):  # running by construction
            skipped.append(name)
            continue
        print(f"gc: stopping idle instance {name}...")
        incus.stop(name)
        stopped.append(name)

    return ReapResult(
        stopped=tuple(stopped),
        deleted=tuple(deleted),
        skipped_live=tuple(dict.fromkeys(skipped)),
    )


def _maybe_background_reap() -> None:
    """Spawn a detached reap pass if one is due (DESIGN §10) — hot-path safe.

    Reads only a local stamp (no daemon calls); claims the slot by writing the
    stamp *before* spawning so concurrent/back-to-back runs don't pile on. The
    child runs in its own session with its std streams discarded, so it survives
    this process and never touches the terminal.
    """
    if not _reap_due():
        return
    _write_reap_stamp(int(time.time()))
    try:
        subprocess.Popen(
            [sys.executable, "-c",
             "from claude_wrapper.lifecycle import _reap_main; _reap_main()"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass  # best-effort background work; next run retries


def _reap_main() -> None:
    """Entry point for the detached background reap (see :func:`_maybe_background_reap`)."""
    try:
        reap(load_config(ensure_user_config()))
    except Exception:
        pass  # detached + silent: a failed pass just defers to the next one


def gc(cfg: Config | None = None) -> int:
    """The ``gc`` subcommand: a foreground reaper pass across all instances (§9)."""
    if cfg is None:
        cfg = load_config(ensure_user_config())
    result = reap(cfg)
    _write_reap_stamp(int(time.time()))
    if not (result.stopped or result.deleted):
        print("gc: nothing to reap.")
    else:
        print(f"gc: stopped {len(result.stopped)}, deleted {len(result.deleted)} "
              f"instance(s).")
    if result.skipped_live:
        print(f"gc: left {len(result.skipped_live)} live session(s) running.")
    return 0


def _confirm(prompt: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _ours_for_delete_all(instances: list[dict]) -> list[str]:
    """Base + every tier-2 template + tier-3 instance, in deletion order."""
    targets: list[str] = []
    if incus.container_exists(BASE):
        targets.append(BASE)
    targets += [
        _inst_name(i) for i in instances
        if (i.get("config") or {}).get(ROLE_KEY) in ("template", "instance")
    ]
    return list(dict.fromkeys(targets))


def delete_containers(name: str | None = None, *, assume_yes: bool = False) -> int:
    """The ``delete`` subcommand (DESIGN §9).

    No *name* → base + all templates + all instances (``[y/N]`` confirm), then
    clear the config stamp so the next run re-``setup``s. A *name* → that
    context's tier-2 template + its tier-3 instances only (base and other
    contexts untouched). Always safe — containers hold no unique state.
    """
    instances = incus.list_instances()

    if name is None:
        targets = _ours_for_delete_all(instances)
        if not targets:
            print("delete: no claude-wrapper containers found.")
            return 0
        if not _confirm(
            f"Delete ALL {len(targets)} claude-wrapper container(s) "
            "(base + templates + instances)?",
            assume_yes=assume_yes,
        ):
            print("Aborted.")
            return 1
        for t in targets:
            print(f"Deleting {t}...")
            incus.delete(t, check=False)
        _clear_stamp()
        print(f"Deleted {len(targets)} container(s). "
              "Run `claude-wrapper setup` to rebuild.")
        return 0

    template = _template_name(name)
    targets = [template] if incus.container_exists(template) else []
    targets += [
        _inst_name(i) for i in instances
        if (i.get("config") or {}).get(ROLE_KEY) == "instance"
        and (i.get("config") or {}).get(CONTEXT_KEY) == name
    ]
    targets = list(dict.fromkeys(targets))
    if not targets:
        print(f"delete: nothing found for context {name!r}.")
        return 0
    if not _confirm(
        f"Delete context {name!r}: {len(targets)} container(s) "
        "(template + instances)?",
        assume_yes=assume_yes,
    ):
        print("Aborted.")
        return 1
    for t in targets:
        print(f"Deleting {t}...")
        incus.delete(t, check=False)
    print(f"Deleted {len(targets)} container(s) for context {name!r}.")
    return 0
