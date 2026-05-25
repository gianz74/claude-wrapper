"""Scope keying, context resolution and launch guards (DESIGN §5/§6/§8).

Pure logic — no daemon or filesystem state beyond a single optional ``git``
call (dependency-injected, so the core is unit-testable):

* :func:`resolve_context` — longest-prefix match of the cwd over every
  context's ``when`` list (OR semantics), config order breaking ties (§6).
* :func:`compute_scope` — when the cwd is inside *any* of the context's mounts
  the scope is the *context* token ``ctx:<name>`` (all subsumed cwds of a context
  share one instance, since each instance CoWs from the one template exposing the
  *union* of the context's mounts); otherwise the git project root → the literal
  cwd, with the *subsumption* flag (no separate project mount when subsumed; §5).
* :func:`check_cwd_allowed` — the refuse-guard (cwd at/under any *alias*
  ``from``/``path``) and the cwd denylist (``$HOME`` itself, ``/``, the system
  roots, and the in-container claude install — ``~/.local``/``~/.local/share``
  exact, ``~/.local/share/claude`` at/under; §8). Raises :class:`RefuseError`.
* :func:`resolve` — the orchestrator the run path (T8) calls: guard → context →
  scope, returning a :class:`Resolution`.
* :func:`scope_hash` — the stable ``hash8(scope)`` keying instances; T8 joins it
  with ``lifecycle._template_name`` to form ``claude-sandbox-<ctx>-<hash8>`` (§5).
* :func:`mask_container_paths` / :func:`ensure_mask_dir` — the ``exclude`` masking
  primitives (§8): the container-side paths to overmount and the shared empty
  read-only host dir bind-mounted on top of them. ``lifecycle._add_mount_devices``
  turns these into the nested incus disk devices. The whitelist posture (§15.6)
  needs no code here — it is just mounting each allowed path as its own entry.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass

from .config import Config, Context, MountSpec

# The ctx component of an instance name when no context matches the cwd (§5/§6).
# Such instances CoW from claude-base directly (no tier-2 template); that's T8.
DEFAULT_CONTEXT = "default"

# cwd denylist (§8). `/` and `$HOME` are *exact*-match denials (subdirectories of
# either are fine); the system roots deny at-or-under. Out-of-home project dirs
# (e.g. /tmp/x, /opt/x) are intentionally permitted — per-cwd isolation earns it.
_SYSTEM_ROOTS = (
    "/etc", "/usr", "/bin", "/boot", "/dev", "/proc", "/sys", "/run", "/var",
)


class RefuseError(Exception):
    """The cwd is disallowed as a workspace (denylist or refuse-guard); §8."""


@dataclass(frozen=True)
class Resolution:
    """The run path's resolved target (DESIGN §5/§6).

    ``context`` is ``None`` for the no-match *default* context; ``context_name``
    is then :data:`DEFAULT_CONTEXT`. ``scope`` is hashed into the instance name
    and, when ``add_project_mount`` is true, is also the host path to bind-mount
    as the per-cwd project mount (false ⇒ the cwd is subsumed by a context mount).
    """

    context: Context | None
    context_name: str
    scope: str
    add_project_mount: bool


# --- path helpers (pure) -----------------------------------------------------


def _norm(path: str) -> str:
    """Normalise for comparison: collapse ``..``/``//`` and strip a trailing ``/``.

    Config paths are already ``~``-expanded (config.py) and the cwd comes from
    :func:`os.getcwd`; we do not resolve symlinks, since DESIGN relies on literal
    path identity between host and container.
    """
    return os.path.normpath(path)


def _is_within(path: str, prefix: str) -> bool:
    """True if *path* is *prefix* itself or a descendant (component-wise).

    Component-wise so ``/a/bc`` is **not** within ``/a/b`` (a plain string
    prefix would wrongly match).
    """
    p = _norm(path)
    pre = _norm(prefix)
    if p == pre:
        return True
    if pre == "/":  # every absolute path descends from root
        return True
    return p.startswith(pre + "/")


# --- context resolution (DESIGN §6) ------------------------------------------


def resolve_context(cwd: str, contexts: tuple[Context, ...]) -> Context | None:
    """Return the context whose ``when`` has the longest prefix covering *cwd*.

    ``when`` entries are OR'd; across all contexts the longest matching prefix
    wins. Two prefixes that both cover one cwd are necessarily nested, so the
    longer string is the more specific (deeper) one — comparing normalised
    prefix length is unambiguous. A true length tie means identical prefixes in
    two contexts; the first in config order wins (DESIGN §6). ``None`` ⇒ the
    cwd matched no context (the *default* context).
    """
    best: Context | None = None
    best_len = -1
    for ctx in contexts:
        for prefix in ctx.when:
            if _is_within(cwd, prefix):
                plen = len(_norm(prefix))
                if plen > best_len:  # strict > keeps the earlier config entry on ties
                    best_len = plen
                    best = ctx
    return best


# --- scope keying + subsumption (DESIGN §5) ----------------------------------


def _is_subsumed(cwd: str, context: Context | None) -> bool:
    """True if *cwd* lies inside any of *context*'s mounts (DESIGN §5).

    Only context mounts count (global mounts are auth/config baked into base,
    never a workspace). When subsumed, every cwd of the context shares **one**
    instance keyed on the *context* (not the individual covering mount): all of a
    context's instances CoW from the one ``claude-sandbox-<ctx>`` template, which
    bakes in the *union* of the context's mounts, so they are byte-identical in
    blast radius — keying per covering mount would only fork redundant instances
    (the multi-disjoint-mount waste this rule fixes). The refuse-guard has already
    excluded any cwd under an *alias*, so a covering mount is always a parity
    mount (``path`` == host backing).
    """
    if context is None:
        return False
    return any(_is_within(cwd, m.path) for m in context.mounts)


def compute_scope(
    cwd: str,
    context: Context | None,
    *,
    project_root_fn=None,
) -> tuple[str, bool]:
    """Return ``(scope, add_project_mount)`` for *cwd* under *context* (DESIGN §5).

    scope = the context token ``ctx:<name>`` when the cwd is inside any of the
    context's mounts (subsumed ⇒ no project mount; all subsumed cwds of a context
    share one instance) → else the git project root → else the literal cwd.
    *project_root_fn* maps a cwd to its project root (or ``None``); it defaults to
    :func:`git_project_root` and is injectable to keep the logic unit-testable
    without shelling out.

    The ``ctx:`` token is **only ever hashed** (``add_project_mount`` is ``False``
    in that branch, so the run path never treats the scope as a host path); the
    prefix keeps it disjoint from the absolute-path scopes of the other branches.
    """
    if _is_subsumed(cwd, context):
        # context is not None here (_is_subsumed is False for the default context).
        return f"ctx:{context.name}", False  # subsumed: one shared per-context instance
    fn = git_project_root if project_root_fn is None else project_root_fn
    root = fn(cwd)
    return _norm(root or cwd), True


def scope_hash(scope: str) -> str:
    """Stable 8-hex-char key for *scope* (keyed on the normalised path).

    T8 forms the instance name as ``_template_name(ctx) + "-" + scope_hash(scope)``
    so equal scopes share one instance (DESIGN §5).
    """
    return hashlib.md5(_norm(scope).encode()).hexdigest()[:8]


# --- exclude masking (DESIGN §8) ---------------------------------------------

# A single shared empty, read-only host dir bind-mounted over every excluded
# sub-path. mode 555 (r-xr-xr-x): listable + traversable but unwritable, so a
# masked path appears as an empty, unmodifiable directory inside the container.
# `/dev/null` can't be used (file-over-directory type mismatch — §8).
MASK_DIR = os.path.expanduser("~/.cache/claude-wrapper/empty")


def mask_container_paths(spec: MountSpec) -> list[str]:
    """Container-side paths to overmount with the empty dir for *spec* (§8).

    ``spec.exclude`` entries are sub-paths *relative* to the mount ``path``
    (config.py leaves them relative); join each onto the container-side ``path``.
    A leading ``/`` on an entry is treated as relative (stripped) so it can never
    escape the mount. Returns ``[]`` for a mount with no exclusions.
    """
    base = _norm(spec.path)
    return [_norm(os.path.join(base, e.lstrip("/"))) for e in spec.exclude]


def ensure_mask_dir(path: str = MASK_DIR) -> str:
    """Create the shared empty mask dir (mode 555) if absent; return its path.

    Idempotent host-side I/O — called lazily by the build path only when a mount
    actually has exclusions. *path* is injectable for tests.
    """
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o555)
    return path


# --- launch guards: refuse-guard + cwd denylist (DESIGN §8) -------------------


def _alias_dirs(cfg: Config) -> list[tuple[str, str]]:
    """Every ``(dir, role)`` off-limits as a cwd via an *alias* mount (§8).

    Both sides of a ``from``-bearing entry are forbidden: the container ``path``
    and the host backing (``from``). Covers global and per-context mounts.
    """
    specs: list[MountSpec] = list(cfg.mounts)
    for ctx in cfg.contexts:
        specs.extend(ctx.mounts)
    out: list[tuple[str, str]] = []
    for s in specs:
        if s.is_alias:
            out.append((s.path, "container path"))
            out.append((s.host_path, "host backing"))
    return out


def check_cwd_allowed(cwd: str, *, home: str, cfg: Config) -> None:
    """Raise :class:`RefuseError` if *cwd* is a disallowed workspace (DESIGN §8).

    Denied: ``$HOME`` itself and ``/`` (exact), anything at/under a system root,
    anything at/under an *alias* ``from``/``path`` (the refuse-guard), and the
    in-container claude install — ``~/.local`` and ``~/.local/share`` (exact)
    plus ``~/.local/share/claude`` (at/under). The last three are the cwd-side
    twin of the setup-time claude-shadow guard (:func:`lifecycle._check_no_claude_shadow`):
    the per-cwd mount is home-parity, so a cwd that *contains* the claude tree
    (``~/.local``/``~/.local/share``) or *is/under* it would bind the host over
    the container's own claude and silently break ``exec claude``. A normal
    project dir — in-home subdir (incl. ``~/.local/bin``) or out-of-home — passes.
    """
    c = _norm(cwd)

    if c == _norm(home):
        raise RefuseError(
            f"refusing to run with the working directory set to $HOME ({home}); "
            "cd into a project subdirectory first."
        )
    if c == "/":
        raise RefuseError("refusing to run with the working directory set to / (filesystem root).")
    for root in _SYSTEM_ROOTS:
        if _is_within(c, root):
            raise RefuseError(
                f"refusing to run inside the system directory {root} "
                f"(cwd {cwd}); cd into a project directory."
            )
    # claude-shadow (cwd side, §8): refuse cwds whose home-parity mount would
    # cover the in-container claude tree. ~/.local and ~/.local/share are
    # exact-match denials (only mounting them *whole* shadows claude — their
    # other children, e.g. ~/.local/bin, are fine workspaces); ~/.local/share/claude
    # is denied at/under (mounting any part of it masks the install's own files).
    local = os.path.join(home, ".local")
    share = os.path.join(local, "share")
    claude_share = os.path.join(share, "claude")
    if c in (_norm(local), _norm(share)):
        raise RefuseError(
            f"refusing to run with the working directory set to {cwd} — its per-cwd "
            "mount would shadow the container's own claude install "
            "(~/.local/share/claude) and silently break `exec claude`. cd into a "
            "project subdirectory (~/.local/bin and other ~/.local children are fine)."
        )
    if _is_within(c, claude_share):
        raise RefuseError(
            f"refusing to run inside the in-container claude install "
            f"(~/.local/share/claude; cwd {cwd}) — its per-cwd mount would shadow "
            "the container's own claude and silently break `exec claude`."
        )
    for adir, role in _alias_dirs(cfg):
        if _is_within(c, adir):
            raise RefuseError(
                f"refusing to run inside the aliased credential store {adir} "
                f"({role}; cwd {cwd}) — a remapped credential dir must never be "
                "used as a workspace."
            )


# --- orchestrator (the run path's entry; DESIGN §5/§6/§8) --------------------


def resolve(
    cwd: str,
    cfg: Config,
    *,
    home: str,
    project_root_fn=None,
) -> Resolution:
    """Guard → resolve context → compute scope, as one :class:`Resolution`.

    Raises :class:`RefuseError` (from :func:`check_cwd_allowed`) before any
    resolution, so a disallowed cwd never reaches scope keying.
    """
    check_cwd_allowed(cwd, home=home, cfg=cfg)
    ctx = resolve_context(cwd, cfg.contexts)
    scope, add_project_mount = compute_scope(cwd, ctx, project_root_fn=project_root_fn)
    return Resolution(
        context=ctx,
        context_name=ctx.name if ctx is not None else DEFAULT_CONTEXT,
        scope=scope,
        add_project_mount=add_project_mount,
    )


# --- the one impure helper (I/O; not unit-tested, like incus.py) -------------


def git_project_root(cwd: str) -> str | None:
    """``git rev-parse --show-toplevel`` from *cwd*; ``None`` if not a repo.

    Returns ``None`` when *cwd* is outside any git work tree or ``git`` is absent
    — the caller then falls back to the literal cwd (DESIGN §5).
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    return root or None
