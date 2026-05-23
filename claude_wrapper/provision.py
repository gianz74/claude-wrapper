"""Provisioning (DESIGN §11): external policy applied during setup.

The *external* half of provisioning — your policy, supplied via config:

* ``[setup].packages`` -> ``apt-get install -y`` (idempotent: probes first).
* optional global / per-context ``provision_script`` -> run on the target
  container as root with ``set -euo pipefail``; output is streamed and a
  non-zero exit fails ``setup`` loudly.

The *internal* half (claude install, identity rename, idmap, apparmor,
DNS-wait) is mechanism and lives in :mod:`lifecycle`.

Filled in alongside T4 (consumed by ``build_base``); reused by T5 for
per-context scripts/packages.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from . import incus


def install_packages(container: str, packages: tuple[str, ...]) -> None:
    """Install any *packages* missing in *container* (one dpkg probe, then apt).

    Idempotent: ``apt-get`` only runs if something is actually missing, so a
    re-``setup`` on an unchanged package set does no work.
    """
    if not packages:
        return
    probe = incus.exec_(
        container,
        ["dpkg-query", "-W", "-f=${Package}\n", *packages],
        capture=True,
        check=False,
    )
    installed = set(str(probe).split())
    missing = [p for p in packages if p not in installed]
    if not missing:
        return
    print(f"Installing apt packages: {' '.join(missing)}")
    incus.exec_(
        container,
        [
            "bash",
            "-c",
            "set -e; apt-get update -qq; DEBIAN_FRONTEND=noninteractive "
            f"apt-get install -y -qq {shlex.join(missing)}",
        ],
    )


def run_provision_script(container: str, script_path: str | None, *, label: str) -> None:
    """Run the *script_path* file inside *container* as root (``set -euo pipefail``).

    A configured-but-absent script is a portability gap (the file lives on the
    host and may differ per machine), so it warns and skips rather than failing
    — matching the §7 "paths absent on a machine are skipped" posture. A script
    that *runs* and errors fails ``setup`` loudly (``check=True``).
    """
    if not script_path:
        return
    p = Path(script_path)
    if not p.exists():
        print(f"warning: {label} provision script not found, skipping: {p}")
        return
    print(f"Running {label} provision script: {p}")
    content = p.read_text()
    incus.exec_(container, ["bash", "-c", "set -euo pipefail\n" + content])
