"""MCP / IDE bridge (DESIGN §12), per-session, bound to the selected instance.

Emacs integrations point claude at host-side MCP servers bound to
``127.0.0.1:<port>``. Two flavours:

* **ai-code** passes ``--mcp-config /tmp/….json`` — a file whose JSON references
  the loopback URL.
* **claude-code-ide** passes ``--mcp-config '{…}'`` (JSON inline) and exports
  ``CLAUDE_CODE_SSE_PORT=<port>`` for its WebSocket "IDE" socket.

Inside the per-cwd instance neither the host file path nor the loopback URLs
resolve, so for each invocation :class:`Bridge` (a context manager) targets the
*selected* instance and:

* stages ``--mcp-config`` **files** into a per-session ``/tmp`` dir bind-mounted
  at the same path, rewriting the args to the staged copies (inline JSON passes
  through). Host ``/tmp`` stays unexposed — only this session's staging dir.
* adds a loopback **proxy device** per port discovered (in any ``--mcp-config``
  file/inline JSON, plus ``CLAUDE_CODE_SSE_PORT``) so container-side
  ``127.0.0.1:PORT`` forwards to the host's ``127.0.0.1:PORT`` (``bind=container``).
* for the IDE SSE port, starts a long-lived uid-1000 **sentinel** and patches the
  IDE **lockfile** (pid → sentinel; ``workspaceFolders`` trailing slash stripped).

Everything is torn down on exit (:meth:`Bridge.cleanup`, run from ``__exit__``).
The pure helpers (:func:`extract_loopback_ports_from_text`,
:func:`normalize_workspace_folders`) are unit-tested; the device/sentinel I/O is
verified by a throwaway integration run, like the rest of the lifecycle layer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

from . import incus

# A loopback host:port reference inside MCP config JSON (or CLAUDE_CODE_SSE_PORT).
_LOOPBACK_RE = re.compile(r"(?:127\.0\.0\.1|localhost):(\d+)")

# Long-lived uid-1000 process whose container-namespace pid the IDE lockfile can
# name. `echo $$` prints that pid; `exec sleep infinity` keeps the *same* pid
# alive (exec preserves it), so no python3 dependency in the container.
_SENTINEL_CMD = ["sh", "-c", "echo $$; exec sleep infinity"]


# --- pure helpers (unit-tested) ----------------------------------------------


def extract_loopback_ports_from_text(text: str) -> list[str]:
    """Loopback ports referenced anywhere in a JSON string (sorted, unique).

    Walks the parsed JSON for ``127.0.0.1:PORT`` / ``localhost:PORT`` substrings.
    Returns ``[]`` for non-JSON or JSON with no loopback reference.
    """
    try:
        data = json.loads(text)
    except Exception:
        return []
    ports: set[str] = set()

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
        elif isinstance(obj, str):
            for m in _LOOPBACK_RE.finditer(obj):
                ports.add(m.group(1))

    walk(data)
    return sorted(ports)


def normalize_workspace_folders(folders: object) -> object:
    """Strip a trailing slash from each workspace folder except ``/`` (§12).

    claude-code-ide records Emacs's ``default-directory`` with a trailing slash,
    but container claude's ``getcwd()`` never has one and matches by strict
    compare. Non-list input and non-string elements are returned unchanged.
    """
    if not isinstance(folders, list):
        return folders
    return [
        f.rstrip("/") if isinstance(f, str) and f != "/" else f for f in folders
    ]


def _md5_short(s: str, n: int = 8) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:n]


# --- the per-session bridge --------------------------------------------------


class Bridge:
    """Per-session MCP/IDE bridge for one *instance* (use as a context manager).

    :meth:`prepare` does the staging/proxy/sentinel/lockfile work and returns the
    rewritten claude args; :meth:`cleanup` (run on ``__exit__``) removes the
    session devices, kills the sentinel and deletes the staging dir. With no
    ``--mcp-config`` and no ``CLAUDE_CODE_SSE_PORT`` it touches the daemon zero
    times, preserving the §15.2 hot-path budget.
    """

    def __init__(self, instance: str, *, home: str) -> None:
        self.instance = instance
        self.home = home
        self._devices: list[str] = []
        self._staging_dir: str | None = None
        self._sentinel_pid: int | None = None
        self._sentinel_proc: subprocess.Popen | None = None

    def __enter__(self) -> "Bridge":
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()

    # --- --mcp-config file staging ---

    def _ensure_staging_dir(self) -> str:
        if self._staging_dir is not None:
            return self._staging_dir
        self._staging_dir = tempfile.mkdtemp(prefix="claude-mcp-", dir="/tmp")
        dev = f"mcpstage-{_md5_short(self._staging_dir)}"
        incus.device_add(
            self.instance, dev, "disk",
            source=self._staging_dir, path=self._staging_dir,
        )
        self._devices.append(dev)
        return self._staging_dir

    def _stage_cfg(self, src: str, sources: list[str], inline: list[str]) -> str:
        """Stage one ``--mcp-config`` value; return the value claude should see.

        Inline JSON (starts with ``{``) is recorded for port scanning and passed
        through unchanged; a file is copied into the staging dir (and its host
        path recorded for port scanning); a missing file passes through so claude
        surfaces its own error.
        """
        if src.lstrip().startswith("{"):
            inline.append(src)
            return src
        if not os.path.isfile(src):
            return src
        staging = self._ensure_staging_dir()
        src_abs = os.path.realpath(src)
        staged = os.path.join(staging, os.path.basename(src_abs))
        shutil.copy(src_abs, staged)
        sources.append(src_abs)
        return staged

    def _rewrite_mcp_args(
        self, args: list[str], sources: list[str], inline: list[str]
    ) -> list[str]:
        out: list[str] = []
        expect_cfg = False
        for arg in args:
            if expect_cfg:
                out.append(self._stage_cfg(arg, sources, inline))
                expect_cfg = False
            elif arg == "--mcp-config":
                out.append(arg)
                expect_cfg = True
            elif arg.startswith("--mcp-config="):
                rest = arg[len("--mcp-config="):]
                out.append("--mcp-config=" + self._stage_cfg(rest, sources, inline))
            else:
                out.append(arg)
        return out

    # --- loopback proxy devices ---

    def _add_proxy(self, port: str) -> None:
        dev = f"mcp-proxy-{port}"
        if incus.device_exists(self.instance, dev):
            return  # a prior session's proxy for this port; it owns teardown
        incus.device_add(
            self.instance, dev, "proxy",
            listen=f"tcp:127.0.0.1:{port}",
            connect=f"tcp:127.0.0.1:{port}",
            bind="container",
        )
        self._devices.append(dev)

    # --- uid-1000 sentinel ---

    def _start_sentinel(self) -> int | None:
        """Launch the sentinel; return its container-namespace pid (or None)."""
        if self._sentinel_pid is not None:
            return self._sentinel_pid
        try:
            proc = incus.exec_popen(
                self.instance, _SENTINEL_CMD, uid=1000, gid=1000,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, text=True,
            )
        except incus.IncusError:
            return None
        try:
            line = proc.stdout.readline().strip() if proc.stdout else ""
            self._sentinel_pid = int(line)
        except (ValueError, AttributeError):
            proc.terminate()
            return None
        self._sentinel_proc = proc
        return self._sentinel_pid

    def _stop_sentinel(self) -> None:
        if self._sentinel_pid is not None:
            incus.exec_(self.instance, ["kill", str(self._sentinel_pid)], check=False)
            self._sentinel_pid = None
        if self._sentinel_proc is not None:
            try:
                self._sentinel_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._sentinel_proc.terminate()
            self._sentinel_proc = None

    # --- IDE lockfile patch ---

    def _patch_lockfile(self, port: str, sentinel_pid: int) -> None:
        """Patch ``~/.claude/ide/<port>.lock`` so container claude accepts it (§12).

        pid → the sentinel's (the host Emacs pid is meaningless in the container
        pid namespace, and claude prunes locks not owned by its own uid);
        ``workspaceFolders`` trailing slash stripped. The lockfile lives under the
        globally-mounted ``~/.claude`` so the host edit is what the container reads.
        """
        lock = os.path.join(self.home, ".claude", "ide", f"{port}.lock")
        try:
            with open(lock) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        changed = False
        if data.get("pid") != sentinel_pid:
            data["pid"] = sentinel_pid
            changed = True
        folders = data.get("workspaceFolders")
        if isinstance(folders, list):
            normalized = normalize_workspace_folders(folders)
            if normalized != folders:
                data["workspaceFolders"] = normalized
                changed = True
        if not changed:
            return
        tmp = f"{lock}.wrapper-tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, lock)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # --- orchestration ---

    def prepare(self, claude_args: list[str]) -> list[str]:
        """Set up the bridge for this invocation; return the rewritten claude args."""
        sources: list[str] = []
        inline: list[str] = []
        out = self._rewrite_mcp_args(claude_args, sources, inline)

        ports: set[str] = set()
        for path in sources:
            try:
                with open(path) as f:
                    ports.update(extract_loopback_ports_from_text(f.read()))
            except OSError:
                pass
        for js in inline:
            ports.update(extract_loopback_ports_from_text(js))
        for port in sorted(ports):
            self._add_proxy(port)

        sse_port = os.environ.get("CLAUDE_CODE_SSE_PORT")
        if sse_port and sse_port.isdigit():
            self._add_proxy(sse_port)
            pid = self._start_sentinel()
            if pid is not None:
                self._patch_lockfile(sse_port, pid)
        return out

    def cleanup(self) -> None:
        """Tear down the session: sentinel, proxy/staging devices, staging dir."""
        self._stop_sentinel()
        for dev in self._devices:
            incus.device_remove(self.instance, dev, check=False)
        self._devices = []
        if self._staging_dir and os.path.isdir(self._staging_dir):
            shutil.rmtree(self._staging_dir, ignore_errors=True)
            self._staging_dir = None
