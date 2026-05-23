# claude-wrapper — Design

A wrapper that runs the `claude` CLI inside an isolated incus sandbox, passing
all unrecognized arguments through to `claude` while adding its own management
subcommands. Supersedes the single-file `~/.local/bin/claude-wrapper.py`.

## 1. Goals & non-goals

- Run `claude` against the **current working directory** plus a configurable
  **selection of host files/directories**, isolated from the rest of the host.
- Strong isolation suited to running `claude` on **untrusted repo content**
  (untrusted code, arbitrary `work`): a compromised session must not reach
  sibling projects or secrets material.
- Fast, lean hot path; heavy work confined to an explicit `setup`.
- Cross-machine portable (personal laptop + work laptop with an sssd `@`
  username and a non-1000 UID).
- **Non-goal:** VMs. We use incus **system containers** (shared kernel, no
  hypervisor, no reserved RAM).

## 2. Isolation mechanism

incus **system containers** only. (`lxc`/LXD support dropped — not installed
here; the dual-backend code goes away.) Containers share the host kernel; an
idle instance is a ~tens-of-MB userspace process tree, not a VM.

## 3. Identity & home model

- **Mirror `$USER` exactly**, including an `@` (e.g. sssd `you@corp`). Linux
  username validation (`NAME_REGEX`/`is_valid_name`) rejects `@`, so we rename
  the stock `ubuntu` user by **editing `/etc/passwd` + `/etc/group` directly**
  (bulletproof across shadow versions; `usermod --badname` used if present).
  sssd usernames work on the host only because they arrive via NSS, not
  `useradd` — a *home directory path* with `@` is always legal, only the
  *username string* is constrained, which is why direct editing is safe.
- **Home = the exact host `$HOME` path** (e.g. `/home/gianz`), via
  `usermod -d "$HOME" -m`. No `/home/$USER`→`/home/ubuntu` symlink (deleted).
  This gives byte-identical paths on both sides → `getcwd()`, IDE
  `workspaceFolders`, and MCP paths all agree.
- **UID stays 1000**; `raw.idmap` maps `host_uid → 1000` for file-ownership
  parity on bind mounts. `incus exec` keys off the numeric UID (`--user 1000`),
  so the (possibly weird) username never touches the exec path.
- **subuid/subgid prerequisite:** `raw.idmap` needs `root:<host_uid>:1` in
  `/etc/subuid` + `/etc/subgid`. `setup` detects a missing entry and **prints**
  the exact `sudo` command (never runs sudo itself):
  `echo 'root:<uid>:1' | sudo tee -a /etc/subuid /etc/subgid` + restart incus.

## 4. Container hierarchy (3 tiers)

| Tier | Name | Built by | Ever run? | Mutable? |
|---|---|---|---|---|
| 1 | `claude-base` | `setup` | never | no — frozen: OS, identity, idmap, apparmor, claude, packages, **global mounts**, global provision |
| 2 | `claude-sandbox-<ctx>` | `setup` (CoW of base + context mounts + per-context provision) | **never** | **no — pure CoW source** |
| 3 | `claude-sandbox-<ctx>-<hash8(scope)>` | on demand (CoW of tier 2) | yes | yes — work happens here; reaped by `gc` |

- Templates (tier 2) are **never started by the run path** — that is how they
  stay immutable; CoW-copying from them gives each instance the context's mount
  devices in a single `incus copy` (no per-instance device-add calls). The sole
  exception is `setup` itself: a template that has a per-context
  `provision_script` is **transiently started during `setup`** (the only way to
  `incus exec` the script), then stopped. A template with no provision script is
  never started at all. Either way the template's resting state is STOPPED and
  nothing outside `setup` ever starts it.
- Devices propagate down the CoW chain: global `[[mounts]]` live on `claude-base`
  → inherited by all; `[[contexts.mounts]]` live on the context template →
  inherited by that context's instances; the project/scope mount lives on the
  instance.
- `setup` rebuilds tiers 1 & 2 by **delete-and-recopy** (instances hold no
  unique state, so this is always safe), **prunes** templates whose context was
  removed from config, and **skips with a warning** any container currently
  running.

## 5. Per-cwd instances & scope keying

Each runnable container is keyed on a **scope** — the largest host subtree the
container exposes that contains the cwd:

> **scope = the broadest `[[contexts.mounts]]` host path that contains the cwd;
> else the project root (`git rev-parse --show-toplevel`); else the literal cwd.**

- Rationale: a context's mounts are baked into its template, so every instance
  exposes them regardless of its hash. If a context mounts all of `~/work`,
  keying on literal cwd would spawn one instance per leaf that each expose all
  of `~/work` anyway — pure duplication. Keying on the covering mount makes
  them share one instance; the broadest covering mount is the true blast radius.
- **Subsumption:** when the cwd *is* covered by a context mount, **no separate
  project mount is added** (it's already inside). Per-cwd isolation therefore
  kicks in exactly where it helps — contexts whose mounts don't contain the cwd
  (e.g. an ssh/gnupg-only context): each cwd gets its own isolated instance +
  project mount.
- Instance name: `claude-sandbox-<ctx>-<hash8(scope)>` (`<ctx>` = `default`
  when no context matches). The ctx prefix keeps `incus list` groupable.

## 6. Context resolution

- A context's `when` is a **list of host path prefixes** (implicit OR). cwd
  matches if it is at or under any entry.
- Across all contexts, **longest matching prefix wins**. Exact-length ties →
  config order (or a setup-time error).
- No implicit default: unmatched cwd → `default` context off `claude-base`
  (no ssh/gnupg). A catch-all `when = ["~"]` context can be added.

## 7. Config — `~/.config/claude-wrapper/config.toml`

Parsed with stdlib `tomllib`. Per-machine variation lives here (paths absent on
a given machine are silently skipped), so the package itself stays generic.

```toml
[setup]
packages = ["jq", "python3-pytest", "emacs", "build-essential", "pandoc"]
provision_script = "~/.config/claude-wrapper/provision.sh"   # optional; run on base as root

[reaper]
stop_idle_after     = "30m"   # running + idle this long → stop
delete_unused_after = "14d"   # not used this long → delete instance
max_instances       = 0       # 0 = unlimited; else LRU-delete beyond this

# Global persistent mounts → baked into claude-base, inherited everywhere.
# `path` = mount location (host & container identical). `from` = host backing
# (only when aliasing). rw default; creds default ro by convention.
[[mounts]]
path = "~/.claude"
[[mounts]]
path = "~/.claude.json"
[[mounts]]
path = "~/workspace/specs"
[[mounts]]
path = "~/.aws"
mode = "ro"

[[contexts]]
name = "api"                                  # explicit; container = claude-sandbox-api
when = ["~/work/acme-api"]     # list = OR; prefix; longest wins
provision_script = "~/.config/claude-wrapper/provision-api.sh"   # optional; run on api template
  [[contexts.mounts]]
  path = "~/.ssh"            # canonical container-side location
  from = "~/.ssh-api"        # backed by this host dir (alias)
  mode = "ro"
  [[contexts.mounts]]
  path = "~/.gnupg"
  from = "~/.gnupg-api"
  mode = "rw"                # gpg-agent must write
  [[contexts.mounts]]
  path    = "~/work"    # whole-tree mount (broad)
  exclude = ["secrets"]    # masked — see §8
```

- **`name` is required**, container is `claude-sandbox-<name>`; duplicate names →
  setup error.
- Mode: `path`-only = parity; `path`+`from` = alias. Credentials default `ro`.

## 8. Mount selection, masking, exclusions, refuse-guard

- **Two postures, both supported:**
  - **Whitelist (recommended for secrets trees):** mount each allowed repo as
    its own entry. Default-deny — new dirs hidden until explicitly added. Also
    makes each repo its own covering mount → its own isolated instance.
  - **Blacklist (`exclude`):** mount a tree, hide sub-paths. Convenient but
    default-expose — a newly-added sensitive dir leaks until excluded. **Do not
    use for secrets material.**
- **Masking (`exclude`)**: bind-mount a shared empty **read-only** dir
  (`~/.cache/claude-wrapper/empty`, mode 555) over each excluded path. `/dev/null`
  does **not** work (file-over-directory type mismatch). Result: the path
  appears as an **empty** dir (cannot be truly removed from a bind mount).
  Implemented as a nested incus disk device on the same template as its parent
  mount. *Caveat:* a hardlink from elsewhere in the tree into an excluded file
  still exposes that inode (symlinks are safe). *Verify at build:* incus applies
  the nested device after its parent so the mask lands on top.
- **Refuse-guard:** the wrapper refuses to launch if the cwd is at or under any
  **alias** (`from`/`path` of a `from`-bearing entry) — you should never use a
  remapped credential store as a workspace.
- **cwd boundary (denylist):** any cwd allowed *except* `$HOME` itself, the
  alias dirs above, and system roots (`/ /etc /usr /bin /boot /dev /proc /sys
  /run /var`). Out-of-home project dirs are permitted — the per-cwd isolation
  earns this flexibility.

## 9. CLI surface

- **Subcommands** (terminal; do their thing and exit, never invoke claude):
  - `claude-wrapper setup` — unconditional, idempotent full provision: build
    base + all templates, prune removed contexts, run gc, write stamp. Also the
    "refresh/repair" button.
  - `claude-wrapper delete [<name>]` — no name → base + all templates +
    instances (`[y/N]` confirm); `<name>` → that context's template + instances.
  - `claude-wrapper gc` — reap idle/orphan/stale instances across all containers.
- **Run path** (anything not a known subcommand): leading-block parse — leading
  `--mount PATH[:ro]` modifiers (ad-hoc per-session mounts) are consumed by the
  wrapper; the **first non-wrapper token ends the block** and everything from
  there passes to `claude` verbatim. An explicit `--` force-terminates wrapper
  parsing (future-proofs against any future claude flag collision).
- **No first-arg-as-project-dir** (`split_project_dir` dropped) — always cwd,
  matching `claude-code-ide` and driving context selection.

## 10. Lifecycle

- **Stamp:** local file holding `hash(schema_version + config.toml)`. A normal
  run compares it (a cheap local hash, no daemon calls); **mismatch → auto-run
  `setup`** (so editing config re-provisions on next launch). Match → fast path.
- **Normal run:** resolve context → compute scope → instance name → ensure it
  exists (CoW from template if missing) and is running (`start` if stopped) →
  bump `user.last-used` → ensure project mount (only if not subsumed; persistent,
  so added once at creation) → MCP/IDE bridge (per-session, §12) → `exec claude`
  → **leave running on exit** → amortized background reap.
- **Reaper:** instances left **running** for snappy re-exec. A pass runs (a) in
  the **background after claude has launched** when a local `last-reap` stamp is
  >1h old, and (b) inside explicit `gc`/`setup`. It stops instances idle past
  `stop_idle_after`, deletes those unused past `delete_unused_after`, and
  LRU-trims beyond `max_instances`. Always safe — instances hold no unique state.
- **Concurrency:** different scopes → different instances → no fixed-path
  (`~/.ssh`) collisions. The Q6 "detect-and-refuse" problem is gone structurally.
- **Shared global state (accepted):** `~/.claude` / `~/.claude.json` are
  bind-mounted from one host source into every instance, so auth/history/config
  are unified. Concurrent writers → last-writer-wins; rare, recoverable. We do
  **not** isolate it (that would fragment auth/history).

## 11. Provisioning

- Internal (mechanism, in the package): claude install + install-method
  detection, identity rename, idmap, `raw.apparmor`, DNS-wait.
- External (your policy, in config dir): `[setup].packages` (wrapper runs
  `apt-get install -y …`) + optional `[setup].provision_script` (run on
  `claude-base`, as root, `set -e`, output streamed, setup fails loudly on
  error) + optional per-context `provision_script` (run on that template — the
  template is transiently started for this during `setup` only, then stopped;
  see §4). Re-run on every `setup` (which rebuilds base/templates).

## 12. Preserved fixes (ported, parameterized by the selected instance)

| Fix | Placement |
|---|---|
| `raw.apparmor` ptrace+signal (Bun SIGPWR/GC crash) | `claude-base` → inherited |
| DNS-resolution wait after launch (no cloud-init on `images:`) | base bootstrap |
| `raw.idmap` host→1000 | base → inherited |
| MCP `--mcp-config` **file** staging + bind-mount (kept; ai-code-style) | per-session, on selected instance |
| Loopback **proxy devices** for MCP/SSE ports | per-session, on selected instance |
| IDE lockfile **pid** patch + uid-1000 **sentinel** process | sentinel in selected instance |
| IDE lockfile **trailing-slash** normalization (Emacs `default-directory`) | kept — independent of home-parity |
| `~/.local/bin` PATH prepend, install-method detect, packages | `setup` (base) |

`claude-code-ide` is the live integration (passes inline `--mcp-config '{…}'` +
`CLAUDE_CODE_SSE_PORT`, cwd = project root). All bridging targets the
context-selected per-cwd instance.

## 13. Packaging

- A real Python package (`claude_wrapper/`), installed with **pipx**, exposing a
  `claude-wrapper` entry point you symlink to `claude`.
- Module layout: `cli.py` (dispatch/arg-parse), `config.py` (tomllib load +
  validate), `incus.py` (cli_run helpers), `lifecycle.py` (tiers, CoW, stamp,
  reaper), `mounts.py` (scope-keying, masking, refuse-guard), `mcp.py`
  (staging/proxy/sentinel/lockfile), `provision.py` (packages + scripts).

## 14. One-time cleanup (do before first `setup`)

Current state: one **stopped** `claude-sandbox` container with 3 devices, no
LXD, no cached images. It holds no unique data (config lives in the host
bind-mount sources). Remove it so the new package starts on bare ground with
zero legacy-handling code:

```
incus delete --force claude-sandbox
```

## 15. Acceptance criteria (verifiable)

The rewrite is "done" when each of these passes:

1. **Identity/`@`:** on a host whose `$USER` contains `@` and UID ≠ 1000, `setup`
   builds `claude-base`; inside, `whoami` == `$USER`, `echo $HOME` == host
   `$HOME`, and a file created in a bind-mounted dir is owned by `$USER` on both
   sides. Missing subuid entry → setup prints the correct `sudo` line and exits.
2. **Fast hot path:** a second launch in an already-provisioned cwd performs no
   `apt`/install/`dpkg` work and ≤ ~3 daemon calls before `claude` starts
   (measured), and editing `config.toml` triggers exactly one auto-`setup`.
3. **Per-cwd isolation:** two sessions under an ssh-only context in *different*
   project dirs run in *different* instances; neither can see the other's
   project files.
4. **Scope-keying / covering mount:** with a context mounting `~/work`,
   launching from `~/work/A` and `~/work/B` lands in the *same*
   instance (`…-<hash(~/work)>`), no separate project mount added.
5. **Masking:** an `exclude`d sub-path appears as an empty dir inside; its real
   contents are unreadable.
6. **Whitelist:** with individual repo mounts (no parent), a sibling repo not in
   the list is absent inside.
7. **Refuse-guard / cwd boundary:** launching from inside an alias `from` dir,
   from `$HOME`, or from a system root is refused with a clear message.
8. **claude-code-ide end-to-end:** from an Emacs project buffer under a context,
   the IDE connects (MCP tools available, diagnostics flow), proving SSE proxy +
   sentinel pid + lockfile patches work against the per-cwd instance.
9. **Concurrency:** two contexts (e.g. `default` + `api`) run simultaneously
   without `~/.ssh` collision.
10. **Lifecycle:** instance stays running after exit; reaper stops it after
    `stop_idle_after` and deletes after `delete_unused_after`; `delete <name>`
    removes one context, `delete` removes all; `setup` re-syncs all from base and
    prunes a removed context.

## 16. Open / deferred

- Verify incus nested-device mount ordering for masking (§8).
- Optional `limits.memory` per instance as a hard guardrail (not default).
- Hardlink-into-excluded-path edge case (§8) — accepted as out of scope.
