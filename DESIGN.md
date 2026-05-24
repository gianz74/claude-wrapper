# claude-wrapper — Design

A wrapper that runs the `claude` CLI inside an isolated incus sandbox, passing
all unrecognized arguments through to `claude` while adding its own management
subcommands. Supersedes the single-file `~/.local/bin/claude-wrapper.py`.

## 1. Goals & non-goals

- Run `claude` against the **current working directory** plus a configurable
  **selection of host files/directories**, isolated from the rest of the host.
- Strong isolation suited to running `claude` on **untrusted repo content**
  (third-party code, security-sensitive trees): a compromised session must not
  reach sibling projects or private material.
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

Each runnable container is keyed on a **scope** — the host subtree that defines
its blast radius and therefore which cwds may share it:

> **scope = if the cwd is covered by *any* `[[contexts.mounts]]` host path → the
> **context itself** (a per-context constant); else the project root
> (`git rev-parse --show-toplevel`); else the literal cwd.**

- Rationale: a context's mounts are *all* baked into its template, so every
  instance of that context exposes the **union** of them regardless of which cwd
  it was launched from. The true blast radius is the context, not any one mount.
  So two *subsumed* launches of one context — e.g. `api` from `~/work` and
  from `~/workspace`, where both trees are context mounts — would otherwise spawn
  two instances that are byte-identical in what they expose: pure duplication
  with zero isolation benefit (each already mounts the other's tree). Keying the
  subsumed case on the context collapses them to one instance.
- **Why not the broadest covering mount?** That was the original rule, and it
  only deduplicated *within* a single covering mount: a context with two disjoint
  covering mounts (`~/work` **and** `~/workspace`) still forked one instance
  per mount. Keying on the context is the generalisation — the covering mount is
  just the degenerate single-mount case of "the context's mounts contain the cwd".
- **Subsumption:** when the cwd *is* covered by a context mount, **no separate
  project mount is added** (it's already inside) and the scope is the context
  constant — so *all* subsumed cwds of a context share one instance. Per-cwd
  isolation kicks in exactly where it helps — contexts whose mounts don't contain
  the cwd (e.g. an ssh/gnupg-only context): the scope falls through to the project
  root, so each cwd gets its own isolated instance + project mount.
- Instance name: `claude-sandbox-<ctx>-<hash8(scope)>` (`<ctx>` = `default`
  when no context matches). In the subsumed case the hashed scope is a constant
  per-context token (e.g. `"ctx:<name>"`), so the suffix is stable across all of
  that context's cwds and stays distinct from the bare `claude-sandbox-<ctx>`
  template name. The ctx prefix keeps `incus list` groupable.

## 6. Context resolution

- A context's `when` is a **list of host path prefixes** (implicit OR). cwd
  matches if it is at or under any entry.
- Across all contexts, **longest matching prefix wins**. Exact-length ties →
  config order (or a setup-time error).
- No implicit default: unmatched cwd → `default` context off `claude-base`
  (no ssh/gnupg). A catch-all `when = ["~"]` context can be added.
- A context's effective mounts are resolved at config-load time from its
  `include`d **mount groups** plus its own `[[contexts.mounts]]` (§7.2); mount
  groups are not contexts — they have no `when` and never participate in this
  resolution.

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

### 7.1 Variable expansion (`[vars]`)

TOML has **no native interpolation, references, or anchors/aliases** — these were
deliberately left out of the spec. So the wrapper supplies a small substitution
pass of its own, purely to keep per-machine configs DRY:

```toml
[vars]
WM = "~/.config/claude-wrapper/work-mappings"

  [[contexts.mounts]]
  path = "~/.gnupg"
  from = "${WM}/.gnupg"
```

- `[vars]` is a flat table of `name → string`. `${NAME}` (**brace form only** —
  a bare `$NAME` is left literal, so paths containing `$` are safe) is
  substituted into **every** other string value in the config (`path`, `from`,
  `provision_script`, `when`, package names, group mounts), as a verbatim
  pre-pass run **before** `~` expansion. Names match `[A-Za-z_][A-Za-z0-9_]*`.
- **Single level, no recursion:** a `${…}` appearing inside a `[vars]` value is
  *not* itself expanded — vars cannot reference vars. Predictable, no cycles.
- **Implicit `${HOME}` / `${USER}`** are always available, seeded from the host
  (`HOME` = `~`, `USER` = `$USER`); an explicit `[vars]` entry of the same name
  overrides them. They exist mainly for `[env]` values, which are literal (`~` is
  not expanded — §7.3): `${HOME}` lets you write a home-relative path the
  consuming tool won't expand itself (e.g. `GIT_CONFIG_GLOBAL`). Because host
  `HOME`/`USER` equal the container's (§3 identity), one value is correct on both
  sides. (A *bare* `$HOME` is still left literal — brace form only.)
- An undefined `${NAME}` is a `ConfigError` naming the key (never silently
  passed through).
- `[vars]` is consumed at parse time only; it has no runtime effect and is not
  part of the `Config` model.

### 7.2 Mount groups (`[mount_groups]` + context `include`)

A **mount group** is a reusable, named bundle of mounts spliced into one or more
contexts. It is **not a context**: no `name`-derived container, no `when`, no
template, no instance, never matched by resolution (§6) — it exists only to be
`include`d. This shares a set of mounts across contexts (e.g. one credential
bundle for several `~/work` sub-trees) without duplicating the entries or
giving the shared thing a `when` that would compete in prefix resolution.

```toml
[mount_groups.acme-creds]
mounts = [
  { path = "~/.ssh",       from = "${WM}/.ssh" },
  { path = "~/.gnupg",     from = "${WM}/.gnupg" },
  { path = "~/.gitconfig", from = "${WM}/.gitconfig" },
]

[[contexts]]
name    = "api"
when    = ["~/work/acme-api"]
include = ["acme-creds"]                 # list, or a bare string for one group
  [[contexts.mounts]]
  path = "~/work/acme-api"
  [[contexts.mounts]]
  path = "~/work/acme-cli"

[[contexts]]
name    = "web"
when    = ["~/work/acme-web"]
include = ["acme-creds"]
# no own mounts → cwd-only (per-cwd project mount via §5) + the shared creds
```

- Each entry under `mounts` is parsed **exactly like** a `[[contexts.mounts]]` /
  `[[mounts]]` table (same `path`/`from`/`mode`/`exclude`); inline tables or full
  tables both work.
- A context's `include` is a list of group names (a bare string is accepted for
  one). An unknown group name → `ConfigError`.
- **Flattening (at parse time):** a context's *effective* mounts = each included
  group's mounts in `include` order, **then** the context's own inline
  `[[contexts.mounts]]`. **Later wins on a container-side `path` collision** — an
  inline mount overrides an included one with the same `path`; among groups, a
  later-listed group overrides an earlier one. The merged list lands in
  `Context.mounts`, so **everything downstream is unchanged** (template build,
  build-id §4, scope-keying §5, masking/guards §8): none of it ever sees groups,
  only the flattened mount list.
- Like `[vars]`, `[mount_groups]` is parse-time-only — not part of the runtime
  `Config` surface. Adopting it changes a context template's baked mount set, so
  the `SCHEMA_VERSION` bump (folded into the §10 stamp) forces one re-`setup`,
  which rebuilds templates and — via T12 — recreates instances on the new rootfs.

### 7.3 Environment variables (`[env]` + context `env`)

Beyond the hardcoded baseline the wrapper always forwards (terminal/locale, IDE
hints, the `ANTHROPIC_`/`CLAUDE_` prefixes — §12), a user can declare
extra env in config. **Env is a run-path concern only:** it is applied at
`exec claude` time, *never* baked into `claude-base`/templates. So it touches no
rootfs, is **not** part of the §4 build-id, needs **no** `SCHEMA_VERSION` bump,
and must **not** trigger instance recreation. Per the build-relevant stamp (§10),
an env-only edit triggers **no** auto-`setup` either — the run path just reads the
new env at the next `exec`. (Implemented by T17; before that the byte-hash stamp
forced one harmless rebuild on any edit.)

```toml
[env]
EDITOR     = "vim"            # literal — set verbatim in every sandbox
PATH_EXTRA = "${WM}/bin"      # ${VAR} (§7.1) expands; NO ~ expansion (env ≠ path)
forward    = ["GH_TOKEN"]     # host vars passed through by name at launch

[[contexts]]
name = "api"
when = ["~/work/acme-api"]
env  = { DEPLOY_ENV = "work", forward = ["WORK_TOKEN"] }  # [contexts.env] sub-table also ok
```

- **Two mechanisms.** Literal `KEY = "value"` pairs set the value verbatim;
  `forward = [...]` is a list of host var *names* whose values are taken from the
  launching shell. A host var named in `forward` that is unset is **silently
  skipped** (same convention as a host path absent on this machine).
- **`forward` is a reserved key** inside an `[env]` table (lowercase; env names
  are conventionally UPPERCASE, so no real collision). Everything else in the
  table is a literal pair.
- **`${VAR}` (§7.1) expands in literal values** (the pre-pass walks all strings);
  `~` is **not** expanded (env values are not paths). For a home-relative value,
  use the implicit **`${HOME}`** (§7.1) — e.g.
  `GIT_CONFIG_GLOBAL = "${HOME}/.config/git/config"`. `forward` entries are bare
  names — no `${}`, untouched.
- **Validation:** env names must match `[A-Za-z_][A-Za-z0-9_]*`; values must be
  strings; `forward` must be a list of strings; `HOME`/`USER`/`PATH` are
  **reserved** (identity + the §11 launcher) and may not appear in `[env]` →
  `ConfigError`.
- **Merge + precedence** (assembled in `_exec_env`, broadest → narrowest, later
  wins): reserved `HOME`/`USER`/`PATH` → built-in forwarded baseline → user
  `forward` (global ∪ context) → user literals (global, then **context overrides
  global**; **literals override forwarded** — explicit beats implicit) →
  `HOME`/`USER`/`PATH` re-asserted last so nothing clobbers identity.
- Like `[vars]`/`[mount_groups]`, `[env]` and a context's `env` are flattened/read
  at parse time; the resulting per-context effective env is what the run path
  consumes.

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
- **claude-shadow guard (setup-time):** `setup` refuses if any configured mount's
  container-side `path` covers the in-container claude — i.e. is at or above
  `~/.local/share/claude` (the binary) or `/usr/local/lib/claude-wrapper/bin`
  (the private launcher, §11). Such a mount would replace the container's own
  claude with host content and silently break `exec claude`. Mounting
  `~/.local/bin` alone is fine — the launcher lives outside it.

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

- **Stamp:** local file holding a hash of the *build-relevant* config — the base
  build-id (§4: schema, global packages, provision-script **content**, global
  mounts) plus every context template's build-id. A normal run recomputes it from
  the loaded config (cheap local hashing + reading the small provision scripts —
  **no daemon calls**) and compares; **mismatch → auto-run `setup`**. Match → fast
  path. Keying on **build identity** (not raw `config.toml` bytes) is deliberate:
  a *runtime-only* edit (`[env]` §7.3, a `[reaper]` threshold) does **not** force a
  rebuild, while a provision-script *content* edit (which leaves `config.toml`
  byte-identical) now correctly does. The auto-`setup` decision and the §4
  instance-recreation decision thus share one definition of "build-relevant" and
  can never disagree.
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
  detection, a **container-private claude launcher symlink** (below), identity
  rename, idmap, `raw.apparmor`, DNS-wait.
- External (your policy, in config dir): `[setup].packages` (wrapper runs
  `apt-get install -y …`) + optional `[setup].provision_script` (run on
  `claude-base`, as root, `set -e`, output streamed, setup fails loudly on
  error) + optional per-context `provision_script` (run on that template — the
  template is transiently started for this during `setup` only, then stopped;
  see §4). Re-run on every `setup` (which rebuilds base/templates).
- **Container-private claude launcher (survives `~/.local/bin` being a mount).**
  The native installer puts the in-container claude at `~/.local/bin/claude`. A
  user may mount the host `~/.local/bin` into the container (global `[[mounts]]`);
  that bind mount would shadow the container's own `~/.local/bin/claude` with the
  host shim and break `exec claude` (the run path execs **bare `claude`** via the
  exec PATH). So `build_base`, **right after the claude install and before
  attaching the global mounts**, resolves the freshly-installed binary
  (`readlink -f ~/.local/bin/claude` → `~/.local/share/claude/versions/<v>`, a
  single self-contained ELF) and creates a launcher symlink in a directory
  **outside `$HOME`** — `/usr/local/lib/claude-wrapper/bin/claude` — which the
  exec PATH **prepends ahead of `~/.local/bin`** (§12). At run time the mounted
  host `~/.local/bin` is then shadowed and inert; bare `claude` resolves to the
  private launcher → the container's own binary (kept private as long as
  `~/.local/share/claude` itself is not mounted — see the §8 claude-shadow guard).
  Auto-update is off (`autoUpdates:false`, mirrored from the host `~/.claude.json`
  mount), so claude is refreshed only by `setup`, consistent with the frozen base.

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
| Container-private claude launcher (`/usr/local/lib/claude-wrapper/bin`) + PATH prepend — survives `~/.local/bin` being a mount (§11) | `setup` (base) → exec PATH |

`claude-code-ide` is the live integration (passes inline `--mcp-config '{…}'` +
`CLAUDE_CODE_SSE_PORT`, cwd = project root). All bridging targets the
context-selected per-cwd instance.

## 13. Packaging & host install

- A real Python package (`claude_wrapper/`), installed with **pipx** (`pipx
  install -e .` during development), exposing a `claude-wrapper` console entry
  point at `~/.local/bin/claude-wrapper`.
- **`claude` invokes the sandbox.** The user types `claude`, not
  `claude-wrapper`, for the run path. Realised as a **`claude` symlink to the
  wrapper placed anywhere on `$PATH` *ahead of* the real claude binary**
  (`~/.local/bin/claude` → `~/.local/share/claude/versions/<v>`). The package
  does **not** dictate the directory — any dir the user controls and orders
  before `~/.local/bin` works (`~/bin` is one natural choice; avoid
  `~/.local/bin/claude` itself, which the native installer owns and may clobber).
  What matters is the *outcome*: `claude` resolves to the wrapper. Management
  subcommands are still run as `claude-wrapper setup|delete|gc`; bootstrap works
  before any shim exists because pipx already puts `claude-wrapper` on `PATH`.
- **Detect-and-instruct, never mutate** — the `_check_subuid` idiom (§3). The
  package never creates the shim, edits a shell rc, or deletes legacy files.
  `setup` **resolves `claude` against the user's `$PATH`** (first-match lookup):
  if it already lands on the wrapper, nothing to do; otherwise it **prints
  suggested commands** (a symlink to `~/.local/bin/claude-wrapper` in a PATH dir
  of the user's choosing, ahead of the real binary) and flags any leftover
  `~/.local/bin/claude-wrapper.py`/`.sh` for removal. The user decides where and
  runs them. See §11 for the in-container half (why `~/.local/bin` being a mount
  doesn't break `exec claude`), §8 for the claude-shadow guard, and §15.11 for
  the criterion.
- Module layout: `cli.py` (dispatch/arg-parse), `config.py` (tomllib load +
  validate), `incus.py` (cli_run helpers), `lifecycle.py` (tiers, CoW, stamp,
  reaper, host-install checks), `mounts.py` (scope-keying, masking, refuse-guard),
  `mcp.py` (staging/proxy/sentinel/lockfile), `provision.py` (packages + scripts).

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
4. **Scope-keying / context dedup:** with a context mounting `~/work` *and*
   `~/workspace`, launching from `~/work/A`, `~/work/B` and
   `~/workspace/C` all land in the *same* instance (`…-<hash("ctx:<name>")>`),
   no separate project mount added. A context whose mounts do **not** cover the
   cwd still forks per project root (that is §15.3).
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
11. **Host install / claude-shadow (§13/§11/§8):** with `~/.local/bin` configured
    as a global mount, a launch still execs the *container's own* claude (the
    private launcher wins over the shadowed `~/.local/bin`); a config that mounts
    `~/.local/share/claude` (or `~`) is refused by `setup` with a clear message;
    and on a host where `claude` does not resolve to the wrapper (no shim, or it
    sits behind the real binary on `$PATH`), `setup` prints suggested fix commands
    (and flags a leftover `~/.local/bin/claude-wrapper.py`/`.sh`).
12. **Env (§7.3):** a global `[env]` literal and a `forward` host var both reach
    the sandbox at `exec` time; a per-context `env` overrides the global literal
    on a key collision; a literal overrides a same-named forwarded var; a `forward`
    name unset on the host is skipped; setting a reserved `HOME`/`USER`/`PATH` in
    `[env]` is rejected by config load; an env-only config edit does **not** change
    the §4 build-id (so no instance recreation; whether it also skips auto-`setup`
    is covered by §15.13/T17).
13. **Build-relevant stamp (§10):** a *runtime-only* `config.toml` edit (an `[env]`
    value, a `[reaper]` threshold) triggers **no** auto-`setup` and no instance
    recreation on the next run (stamp unchanged); a *build-relevant* edit — a
    `[setup].packages` entry, a mount, **or a provision-script's content with
    `config.toml` byte-identical** — triggers exactly one auto-`setup`. No daemon
    calls are added to the warm path versus §15.2.

## 16. Open / deferred

- ~~Verify incus nested-device mount ordering for masking (§8).~~ **Resolved (T7):**
  incus stacks the mask on top of its parent — confirmed against the daemon, and
  reinforced by `mnt-`/`msk-` device names that also sort parent-before-mask.
- Optional `limits.memory` per instance as a hard guardrail (not default).
- Hardlink-into-excluded-path edge case (§8) — accepted as out of scope.
