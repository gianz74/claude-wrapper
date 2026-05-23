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
import time
from pathlib import Path

from . import incus, provision
from .config import Config, MountSpec, load_user_config

# Tier 1: the frozen base. Tier-2 templates CoW-copy from it (T5); it is built
# (started, provisioned) by setup, then stopped and never run again.
BASE = "claude-base"
IMAGE = "images:ubuntu/24.04"

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


def _add_mount_devices(container: str, mounts: tuple[MountSpec, ...]) -> None:
    """Add persistent bind-mount disk devices for *mounts*; skip absent sources.

    Per §7, host paths absent on this machine are skipped. ``spec.exclude``
    masking (the nested empty-RO overmount) is intentionally deferred to T7.
    """
    skipped: list[str] = []
    for spec in mounts:
        src = spec.host_path
        if not os.path.exists(src):
            skipped.append(src)
            continue
        props: dict[str, object] = {"source": src, "path": spec.path}
        if spec.mode == "ro":
            props["readonly"] = True
        incus.device_add(container, _mount_device_name(spec), "disk", **props)
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


# --- setup entry point (DESIGN §9) -------------------------------------------


def setup(cfg: Config | None = None) -> int:
    """The ``setup`` subcommand: unconditional, idempotent full provision.

    T4 builds the base. T5 adds ``build_templates`` + context pruning; T8/T10
    add the stamp write + reaper pass.
    """
    if cfg is None:
        cfg = load_user_config()
    host_user = os.environ.get("USER") or getpass.getuser()
    host_uid = os.getuid()
    host_gid = os.getgid()
    home = os.environ.get("HOME") or os.path.expanduser("~")
    build_base(cfg, host_user=host_user, host_uid=host_uid, host_gid=host_gid, home=home)
    print("setup: base complete. (context templates land in T5.)")
    return 0
