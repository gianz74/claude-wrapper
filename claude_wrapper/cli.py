"""CLI dispatch / argument parsing (DESIGN §9).

Two paths:

* **Subcommands** (``setup``, ``delete [<name>]``, ``gc``) — terminal; they do
  their thing and exit, never invoking ``claude``.
* **Run path** (anything else) — a *leading-block* parse: leading
  ``--mount PATH[:ro]`` modifiers are consumed by the wrapper; the first
  non-wrapper token ends the block and everything from there is passed to
  ``claude`` verbatim. An explicit ``--`` force-terminates wrapper parsing.

T1 wires up dispatch + parsing only. Subcommands and the run path forward to
stubs; the real implementations land in later tasks (lifecycle/mounts/...).
"""

from __future__ import annotations

import sys
from typing import NamedTuple

SUBCOMMANDS = ("setup", "delete", "gc")


class Mount(NamedTuple):
    """An ad-hoc per-session mount requested via ``--mount``."""

    path: str
    mode: str  # "ro" | "rw"


def _parse_mount_spec(spec: str) -> Mount:
    """Parse a ``--mount`` argument: ``PATH``, ``PATH:ro`` or ``PATH:rw``."""
    if spec.endswith(":ro"):
        return Mount(spec[:-3], "ro")
    if spec.endswith(":rw"):
        return Mount(spec[:-3], "rw")
    return Mount(spec, "rw")


def parse_run_args(args: list[str]) -> tuple[list[Mount], list[str]]:
    """Leading-block parse for the run path.

    Returns ``(mounts, passthrough)`` where *mounts* are wrapper-consumed
    ``--mount`` modifiers and *passthrough* is forwarded to ``claude``
    verbatim. The first non-wrapper token (or an explicit ``--``) ends the
    leading block.
    """
    mounts: list[Mount] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            # Force-terminate wrapper parsing; the rest is passthrough.
            return mounts, args[i + 1 :]
        if tok == "--mount":
            if i + 1 >= len(args):
                sys.exit("claude-wrapper: --mount requires a PATH argument")
            mounts.append(_parse_mount_spec(args[i + 1]))
            i += 2
            continue
        if tok.startswith("--mount="):
            mounts.append(_parse_mount_spec(tok[len("--mount=") :]))
            i += 1
            continue
        # First non-wrapper token ends the block.
        return mounts, args[i:]
    return mounts, []


# --- subcommand stubs (real impls land in later tasks) ----------------------


def cmd_setup(args: list[str]) -> int:
    from . import config, incus, lifecycle

    try:
        return lifecycle.setup()
    except (config.ConfigError, lifecycle.SetupError, incus.IncusError) as e:
        print(f"claude-wrapper setup: {e}", file=sys.stderr)
        return 1


def cmd_delete(args: list[str]) -> int:
    """``delete [-y] [<name>]`` — remove all containers, or one context's.

    ``-y``/``--yes`` skips the confirmation prompt (handy for scripts/cleanup).
    """
    from . import config, incus, lifecycle

    assume_yes = False
    name: str | None = None
    for a in args:
        if a in ("-y", "--yes"):
            assume_yes = True
        elif a.startswith("-"):
            print(f"claude-wrapper delete: unknown option {a!r}", file=sys.stderr)
            return 2
        elif name is None:
            name = a
        else:
            print("claude-wrapper delete: too many arguments", file=sys.stderr)
            return 2
    try:
        return lifecycle.delete_containers(name, assume_yes=assume_yes)
    except (config.ConfigError, lifecycle.SetupError, incus.IncusError) as e:
        print(f"claude-wrapper delete: {e}", file=sys.stderr)
        return 1


def cmd_gc(args: list[str]) -> int:
    from . import config, incus, lifecycle

    try:
        return lifecycle.gc()
    except (config.ConfigError, lifecycle.SetupError, incus.IncusError) as e:
        print(f"claude-wrapper gc: {e}", file=sys.stderr)
        return 1


def run_passthrough(mounts: list[Mount], passthrough: list[str]) -> int:
    """Run path (DESIGN §9/§10): resolve context/scope/instance and ``exec claude``.

    Ad-hoc ``--mount`` modifiers and the passthrough args are handed to
    :func:`lifecycle.run`, which auto-``setup``s on stamp drift, ensures the
    per-cwd instance exists + runs, then execs claude. Returns claude's exit code.
    """
    from . import config, incus, lifecycle
    from .mounts import RefuseError

    try:
        return lifecycle.run(mounts, passthrough)
    except RefuseError as e:
        print(f"claude-wrapper: {e}", file=sys.stderr)
        return 1
    except (config.ConfigError, lifecycle.SetupError, incus.IncusError) as e:
        print(f"claude-wrapper: {e}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] in SUBCOMMANDS:
        name, rest = args[0], args[1:]
        return {
            "setup": cmd_setup,
            "delete": cmd_delete,
            "gc": cmd_gc,
        }[name](rest)

    # Run path: everything that is not a known subcommand.
    mounts, passthrough = parse_run_args(args)
    return run_passthrough(mounts, passthrough)


if __name__ == "__main__":
    raise SystemExit(main())
