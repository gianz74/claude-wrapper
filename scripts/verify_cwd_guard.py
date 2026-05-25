#!/usr/bin/env python3
"""Verify the §8 claude-shadow cwd guard fires — run this on the HOST.

The guard (`claude_wrapper.mounts.check_cwd_allowed`) is pure path logic, so the
authoritative check needs no incus daemon: it calls the guard directly against
your *real* ``$HOME`` and the actual deny/allow path set, and reports PASS/FAIL
per case. The run path reaches it verbatim (cli.main → lifecycle.run → resolve →
check_cwd_allowed; lifecycle.py:1119), so a green logic check means a launch from
those dirs refuses.

    python3 scripts/verify_cwd_guard.py          # authoritative, hermetic, no daemon
    python3 scripts/verify_cwd_guard.py --e2e    # also drive the real `claude-wrapper`
                                                 #   binary from each existing denied dir

Run from the repo root so `import claude_wrapper` resolves to the checkout (the
logic check then reports that copy; --e2e always tests the deployed binary).

``--e2e`` is the true end-to-end proof but only runs when the sandbox build stamp
is fresh — otherwise lifecycle.run would auto-run `setup` (heavy incus build)
*before* the guard (lifecycle.py:1111), which we don't want a verifier to trigger.

Exit 0 = guard behaves exactly as specified; non-zero = a case misbehaved.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

# Make the checkout importable when run as a plain script (no install needed) and
# so the logic check reports the repo copy. --e2e still tests the deployed binary.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_wrapper import mounts
from claude_wrapper.config import Config
from claude_wrapper.mounts import RefuseError, check_cwd_allowed

HOME = os.environ.get("HOME") or os.path.expanduser("~")


def _h(*parts: str) -> str:
    return os.path.join(HOME, *parts)


# (cwd, substring the refuse message must contain). The last DENY case is the
# inner-subtree one (~/.local/share/claude/versions/<v>) we chose to also block.
DENY: list[tuple[str, str]] = [
    (_h(".local"), "claude install"),
    (_h(".local", "share"), "claude install"),
    (_h(".local", "share", "claude"), "claude install"),
    (_h(".local", "share", "claude", "versions", "1.2.3"), "claude install"),
]
# Children that sit beside the claude tree — must stay legal workspaces.
ALLOW: list[str] = [
    _h(".local", "bin"),
    _h(".local", "bin", "sub"),
    _h(".local", "state", "x"),
    _h(".local", "share", "other"),
    _h(".localx"),  # not a component prefix of ~/.local
    _h("src", "proj"),  # an ordinary project dir
]


def check_logic() -> bool:
    """Call the guard directly for every case; pure, needs no dirs to exist."""
    print(f"guard module : {mounts.__file__}")
    print(f"$HOME        : {HOME}")
    # The logic check tests the module imported *above* (the repo copy when run
    # from the checkout) — NOT necessarily what `~/.local/bin/claude-wrapper`
    # runs. A pipx install can lag the repo, so logic-green + e2e-red means
    # "fix is committed but not deployed; reinstall". --e2e is deployment truth.
    if "site-packages" not in mounts.__file__:
        print("NOTE: testing a source-tree copy; run with --e2e to test the "
              "deployed `claude-wrapper` binary.")
    print()
    cfg = Config()
    ok = True

    print("DENY (expect refusal):")
    for path, needle in DENY:
        try:
            check_cwd_allowed(path, home=HOME, cfg=cfg)
        except RefuseError as e:
            good = needle in str(e)
            ok &= good
            print(f"  {'PASS' if good else 'FAIL'}  {path}")
            if not good:
                print(f"        refused but message lacked {needle!r}: {e}")
        else:
            ok = False
            print(f"  FAIL  {path}  (NOT refused!)")

    print("\nALLOW (expect no refusal):")
    for path in ALLOW:
        try:
            check_cwd_allowed(path, home=HOME, cfg=cfg)
        except RefuseError as e:
            ok = False
            print(f"  FAIL  {path}  (wrongly refused: {e})")
        else:
            print(f"  PASS  {path}")

    return ok


def _stamp_is_fresh() -> bool:
    """True if a real run would skip the pre-guard auto-setup (lifecycle.py:1111)."""
    from claude_wrapper.config import ensure_user_config, load_config
    from claude_wrapper.lifecycle import _config_stamp, _read_stamp

    cfg = load_config(ensure_user_config())
    return _read_stamp() == _config_stamp(cfg)


def check_e2e() -> bool:
    """Drive the real `claude-wrapper` binary from each existing denied dir."""
    binpath = shutil.which("claude-wrapper")
    if not binpath:
        print("\n[e2e] `claude-wrapper` not on PATH — skipping end-to-end check.")
        return True
    if not _stamp_is_fresh():
        print(
            "\n[e2e] sandbox build stamp is stale — a real run would auto-run "
            "`setup` before reaching the guard. Skipping e2e; run "
            "`claude-wrapper setup` first, then re-run with --e2e."
        )
        return True

    ok = True
    print(f"\n[e2e] driving {binpath} from each existing denied dir:")
    for path, needle in DENY:
        if not os.path.isdir(path):
            print(f"  skip  {path}  (absent on this host)")
            continue
        # Any passthrough token routes to the run path; the guard refuses before
        # `exec claude`, so claude never actually starts.
        proc = subprocess.run(
            [binpath, "--version"],
            cwd=path,
            capture_output=True,
            text=True,
        )
        out = proc.stderr + proc.stdout
        refused = proc.returncode != 0 and needle in out
        ok &= refused
        print(f"  {'PASS' if refused else 'FAIL'}  {path}  (exit={proc.returncode})")
        if not refused:
            print(f"        output: {out.strip()[:300]}")
    return ok


def main() -> int:
    logic_ok = check_logic()
    e2e_ok = True
    if "--e2e" in sys.argv[1:]:
        e2e_ok = check_e2e()
    ok = logic_ok and e2e_ok
    print(f"\n{'== ALL CHECKS PASSED ==' if ok else '== SOME CHECKS FAILED =='}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
