# claude-wrapper

Run the [`claude`](https://claude.com/claude-code) CLI inside an isolated
[incus](https://linuxcontainers.org/incus/) system container, while passing every
unrecognized argument straight through to `claude` and adding a handful of
management subcommands.

You type `claude` as usual; the wrapper transparently launches it in a sandbox
keyed to your current directory, so a compromised or untrusted repo session can't
reach the rest of your host.

> **`DESIGN.md` is the source of truth.** This README is the orientation; the
> design doc is authoritative for behaviour and rationale. `TASKS.md` is the
> dependency-ordered build log.

## Why

- **Isolation for untrusted content.** Running `claude` against arbitrary
  repos or security-sensitive material shouldn't expose sibling projects.
  Each working directory gets its own throwaway container; blast radius is scoped.
- **Fast hot path.** All the heavy work (image build, package install, identity
  setup) happens once in an explicit `setup`. A warm relaunch is ~3 daemon calls
  before `claude` starts — no `apt`, no `dpkg`, no install probing.
- **Path-identical home.** The container mirrors your real `$USER` (including an
  sssd `@`-username) and your exact `$HOME` path, so `getcwd()`, IDE
  `workspaceFolders`, and MCP paths all agree on both sides.
- **System containers, not VMs.** incus containers share the host kernel; an idle
  instance is a tens-of-MB process tree, not a reserved-RAM hypervisor guest.

## How it works (in brief)

A **3-tier copy-on-write hierarchy** (DESIGN §4):

| Tier | Name | Built by | Run? | Mutable? |
|---|---|---|---|---|
| 1 | `claude-base` | `setup` | never | no — OS, identity, idmap, apparmor, claude, packages, global mounts |
| 2 | `claude-sandbox-<ctx>` | `setup` (CoW of base + context mounts) | never | no — pure CoW source |
| 3 | `claude-sandbox-<ctx>-<hash8>` | on demand (CoW of tier 2) | yes | yes — work happens here; reaped by `gc` |

- **Contexts** are selected by matching your cwd against host-path prefixes
  (`when`), longest prefix wins. An unmatched cwd falls back to the `default`
  context off `claude-base`.
- **Scope keying** decides which cwds share an instance (DESIGN §5). If the cwd is
  covered by any of the context's mounts, all such cwds share one instance (same
  blast radius, no isolation lost). Otherwise the scope falls through to the git
  project root, so each project gets its own isolated instance.
- **Instances are left running** after `claude` exits for snappy re-launch; an
  amortized background reaper stops idle ones and deletes stale ones. Instances
  hold no unique state (files live on host bind-mounts), so reaping is always safe.

## Requirements

- Linux with **incus** installed and your user able to drive it (LXD/`lxc` is
  intentionally not supported).
- Python **3.11+** (stdlib `tomllib`; the package has zero runtime dependencies).
- A `root:<host_uid>:1` entry in `/etc/subuid` and `/etc/subgid` for the
  `raw.idmap` host→1000 mapping. `setup` detects a missing entry and **prints** the
  exact `sudo` command to add it — it never runs `sudo` itself.

## Install

Install the package with [pipx](https://pipx.pypa.io/):

```sh
pipx install -e .        # editable, for development
```

This puts a `claude-wrapper` console script on your `PATH`. To make the bare
`claude` command launch the sandbox, add a `claude` symlink to the wrapper in a
`PATH` directory **ahead of** the real claude binary, e.g.:

```sh
ln -s ~/.local/bin/claude-wrapper ~/bin/claude   # ~/bin must precede ~/.local/bin
```

`setup` **detects and instructs, never mutates** — it resolves `claude` against
your `$PATH` and, if it doesn't already land on the wrapper, prints the suggested
commands for you to run. It never edits your shell rc or creates the shim itself.

Then build the sandbox:

```sh
claude-wrapper setup
```

The first run also writes a documented default `config.toml` and `provision.sh`
into `~/.config/claude-wrapper/` if absent.

## Usage

### Running claude

Anything that isn't a known subcommand is the **run path** — it resolves the
context/scope for your cwd, ensures the instance exists and is running, and execs
`claude` with your arguments forwarded verbatim:

```sh
claude                       # interactive session in the cwd's instance
claude -p "explain this bug" # args pass straight through to claude
```

Leading `--mount PATH[:ro|:rw]` modifiers are consumed by the wrapper as ad-hoc
session mounts; the first non-wrapper token (or an explicit `--`) ends the leading
block and everything after passes to `claude`:

```sh
claude --mount /data:ro -- --resume
```

Editing `config.toml` flips a local stamp, so the **next launch auto-runs `setup`
exactly once** to re-provision.

### Management subcommands

| Command | Effect |
|---|---|
| `claude-wrapper setup` | Idempotent full provision: build base + all templates, prune removed contexts, run gc, write the stamp. Also the "refresh/repair" button. |
| `claude-wrapper gc` | Reap idle / orphaned / stale instances across all containers. |
| `claude-wrapper delete [<name>]` | No name → base + all templates + instances (with `[y/N]` confirm, `-y` to skip); `<name>` → just that context's template + instances. |

## Configuration

Per-machine config lives in `~/.config/claude-wrapper/config.toml` (the package
itself stays generic). Paths absent on a given machine are silently skipped. Full
reference in DESIGN §7; the shipped default is self-documenting.

```toml
[vars]                                    # §7.1 — DRY path prefixes; ${NAME} only
WM = "~/.config/claude-wrapper/work-mappings"

[setup]
packages = ["jq", "build-essential"]      # apt packages baked into claude-base
provision_script = "~/.config/claude-wrapper/provision.sh"   # run on base as root

[reaper]
stop_idle_after     = "30m"               # running + idle this long  -> stop
delete_unused_after = "14d"               # not used this long        -> delete
max_instances       = 0                   # 0 = unlimited; else LRU-trim

# Global mounts: baked into claude-base, inherited everywhere. Shared auth/history.
[[mounts]]
path = "~/.claude"
[[mounts]]
path = "~/.claude.json"

[mount_groups.acme-creds]               # §7.2 — reusable bundle, not a context
mounts = [
  { path = "~/.ssh",   from = "${WM}/.ssh",   mode = "ro" },
  { path = "~/.gnupg", from = "${WM}/.gnupg", mode = "rw" },  # gpg-agent writes
]

[[contexts]]
name    = "api"                           # container = claude-sandbox-api
when    = ["~/work/acme-api"]   # list = OR; longest prefix wins
include = ["acme-creds"]                # splice in the shared credential group
  [[contexts.mounts]]
  path    = "~/work"                 # whole-tree mount
  exclude = ["secrets"]                 # masked: appears as an empty read-only dir
```

Key config concepts:

- **`path` vs `from`** — `path` alone means the host and container locations are
  identical; adding `from` aliases a different host backing dir (e.g. a per-context
  credential store). Credentials should be `mode = "ro"` by convention.
- **`[vars]` (§7.1)** — TOML has no interpolation, so the loader does a `${NAME}`
  pre-pass (brace form only; a bare `$NAME` is left literal). Single level, no
  recursion. Undefined `${NAME}` is an error.
- **Mount groups (§7.2)** — a named, reusable bundle of mounts that contexts
  `include`. A group is *not* a context: no `when`, no template, no instance. An
  inline `[[contexts.mounts]]` overrides an included mount with the same `path`.
- **Masking / whitelist (§8)** — `exclude` overmounts a sub-path with an empty
  read-only dir (a blacklist; default-expose). For secrets material prefer the
  **whitelist** posture: mount each allowed repo as its own entry so anything not
  listed is simply absent.
- **Guards** — the wrapper refuses to launch from inside a credential alias dir,
  from `$HOME` itself, or from a system root; `setup` refuses a config whose mount
  would shadow the container's own `claude`.

## Layout

```
claude_wrapper/
  cli.py         dispatch + leading-block run-path arg parsing (§9)
  config.py      tomllib load, validate, [vars]/mount-group flattening (§7)
  incus.py       thin mechanism layer over the incus CLI (no policy)
  lifecycle.py   tiers, CoW, stamp, run path, reaper, gc, host-install checks (§4/§10)
  mounts.py      context resolution, scope keying, masking, guards (§5/§6/§8)
  mcp.py         MCP/IDE bridge: config staging, loopback proxies, sentinel, lockfile (§12)
  provision.py   apt packages + provision scripts (§11)
tests/           pytest unit tests (pure logic; daemon paths verified manually)
DESIGN.md        authoritative design + §15 acceptance matrix
TASKS.md         dependency-ordered build log + progress log
```

## Development

Implementation proceeds **one task at a time, each in a clean context** (see
`CLAUDE.md`): pick the first unchecked task in `TASKS.md`, verify it against its
"Done when" criteria, log it, and commit. The package is **incus-only** and
**legacy-free** — it supersedes the old single-file `~/.local/bin/claude-wrapper.py`
with zero migration code.

Run the unit tests:

```sh
python3 -m pytest -q
```

Tests cover the pure logic (config parsing, scope keying, masking, build-id drift,
arg parsing). The daemon-facing paths (base/template builds, the run path, the MCP
bridge) are verified by throwaway integration runs against a real incus daemon, as
recorded in the `TASKS.md` progress log.

## Status

Tasks **T1–T15 complete** — the full `TASKS.md` list. Build, run path, MCP/IDE
bridge, reaper/gc/delete, host-install checks, instance recreation on source
rebuild, `${VAR}` expansion, mount groups, and context-keyed scope dedup are all
implemented and verified against the DESIGN §15 acceptance matrix.
