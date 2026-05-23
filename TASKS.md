# Implementation tasks

Ordered, dependency-aware breakdown of the `DESIGN.md` build. **One task per
session, executed with a clear context** (see the workflow in `CLAUDE.md`).

## How to use this file (every session)

1. Read `CLAUDE.md` + `DESIGN.md` (the latter is authoritative).
2. Find the **first unchecked `[ ]` task** below and do **only that one**.
3. Verify it against its **Done when** criteria.
4. Check the box, append a dated entry to the **Progress log** (capture anything
   non-obvious the next task needs — decisions, gotchas, deviations), and
   `git commit`.
5. **STOP.** Tell the user the task is done and to `/clear`. Do **not** start the
   next task.

If a task turns out to depend on something unbuilt or a design gap, stop and
surface it rather than guessing.

---

## Tasks

- [x] **T1 — Repo + package skeleton.** `git init`; commit existing
  `DESIGN.md`/`CLAUDE.md`. Create `pyproject.toml` (pipx-installable,
  `claude-wrapper` console entry point) and the `claude_wrapper/` package with
  empty modules per DESIGN §13. Implement `cli.py` dispatch only: subcommands
  `setup`/`delete [name]`/`gc` (stubs that print "not implemented"), run-path
  leading-block parse (`--mount` modifiers, `--` escape), everything else
  forwarded to a stub. **Done when:** `pipx install -e .` succeeds;
  `claude-wrapper gc` prints the stub; `claude-wrapper -p hi` routes to the
  passthrough stub with `-p hi`; `claude-wrapper --mount /x -- --foo` parses
  `/x` as a mount and `--foo` as passthrough.

- [x] **T2 — Config loader + validation (`config.py`).** Load
  `~/.config/claude-wrapper/config.toml` via `tomllib`; model `[setup]`,
  `[reaper]`, `[[mounts]]` (path/from/mode/exclude), `[[contexts]]`
  (name/when[list]/provision_script/mounts) per DESIGN §7. Validate: required
  `name`, duplicate names → error, `~` expansion, ro/rw, sane errors on malformed
  TOML. Ship a documented default `config.toml` + `provision.sh` stub written on
  first run if absent. **Done when:** unit tests load a sample config and reject
  a duplicate-name and a malformed file with clear messages.

- [x] **T3 — incus helpers (`incus.py`).** `cli_run`/`cli_quiet`,
  `container_exists`, device add/remove/show (single cached `device show`),
  `copy`, `launch`, `start`, idmap/apparmor/config set. incus-only (no LXD).
  **Done when:** a throwaway script can launch+delete a container and add/remove
  a disk device through these helpers.

- [x] **T4 — Base build (`lifecycle.py`: `build_base`).** `setup` builds
  `claude-base` per DESIGN §3/§11/§12: launch `images:ubuntu/24.04`, rename user
  to `$USER` via `/etc/passwd`/`/etc/group` edit, home = `$HOME` (`usermod -d -m`),
  `raw.idmap` host→1000, subuid detection + **print** sudo line, `raw.apparmor`
  ptrace+signal, DNS wait, claude install + install-method detect, `[setup]`
  packages, global `provision_script`, global `[[mounts]]`. **Done when:**
  acceptance §15.1 passes (whoami==$USER, $HOME correct, bind-mount ownership
  parity; missing subuid → printed sudo line + exit).

- [x] **T5 — Context templates (`lifecycle.py`: `build_templates`).** CoW
  `claude-base` → `claude-sandbox-<ctx>` per context, add context mounts +
  per-context `provision_script`; prune templates for removed contexts; skip +
  warn on running containers; never start a template. **Done when:** templates
  exist with correct devices, are STOPPED, and removing a context from config +
  `setup` prunes its template.

- [x] **T6 — Scope keying + resolution + guards (`mounts.py`, pure logic).**
  Context resolution (longest-prefix over `when` lists, OR semantics), scope =
  broadest covering context mount → project root (`git rev-parse --show-toplevel`)
  → cwd; subsumption rule; refuse-guard (cwd in any alias `from`/`path`); cwd
  denylist (DESIGN §5/§6/§8). **Done when:** unit tests cover §15.3/§15.4/§15.7
  cases (incl. the `~/work/A` vs `/B` same-instance result).

- [x] **T7 — Masking + whitelist (`mounts.py`).** `exclude` → nested empty-RO
  overmount device; **verify incus applies nested device after parent**.
  **Done when:** §15.5 (excluded path empty inside) and §15.6 (whitelisted
  sibling absent) pass.

- [x] **T8 — Run path + instance lifecycle (`lifecycle.py`: `run`).** Stamp
  check (`hash(version+config)`) → auto-`setup` on drift; resolve→scope→instance
  name; ensure instance exists (CoW from template) + running (`start`); bump
  `user.last-used`; add project mount if not subsumed; `exec claude` with env
  forwarding + PATH; leave running on exit. **Done when:** §15.2 (no install work
  on 2nd launch, ≤~3 daemon calls, config edit → one auto-setup) and a real
  `claude` session launches in the right instance.

- [ ] **T9 — MCP/IDE bridge (`mcp.py`).** Port the preserved fixes (DESIGN §12)
  targeting the selected instance: `--mcp-config` file staging+mount, loopback
  proxy devices, uid-1000 sentinel, lockfile pid + trailing-slash patches.
  **Done when:** §15.8 (`claude-code-ide` connects end-to-end from an Emacs
  buffer under a context) and §15.9 (two contexts concurrently, no `~/.ssh`
  collision) pass.

- [ ] **T10 — Reaper + gc + delete (`lifecycle.py`).** Leave-running + amortized
  background reap (local `last-reap` stamp, >1h → background pass), `[reaper]`
  thresholds (stop_idle/delete_unused/max_instances via `user.last-used`), `gc`
  subcommand, `delete [name]` (one context vs all, `[y/N]`). **Done when:**
  §15.10 passes.

---

## Progress log

_(Append one entry per completed task: date, what changed, decisions/gotchas for
the next task, verification result.)_

### 2026-05-23 — T1: Repo + package skeleton

**Changed:** `git init` (host `guybrush`, not a sandbox — there is no sandbox
until `setup` exists, so all bootstrap/skeleton work runs on the host).
Added `pyproject.toml` (setuptools backend, `claude-wrapper` console entry →
`claude_wrapper.cli:main`, `requires-python >=3.11` for stdlib `tomllib`,
zero runtime deps, `[test]` extra = pytest), `.gitignore`, and the
`claude_wrapper/` package with all §13 modules. `cli.py` has the real
dispatch; `config/incus/lifecycle/mounts/mcp/provision.py` are docstring-only
stubs annotated with the task that fills them.

**Decisions / gotchas for next tasks:**
- Dispatch rule: `args[0] in {setup,delete,gc}` → subcommand; else run path.
  So `--mount …` (not a subcommand) correctly falls through to the run path.
- `parse_run_args` (in `cli.py`) returns `(list[Mount], passthrough)`. `Mount`
  is a `NamedTuple(path, mode)`; `--mount` accepts `PATH`, `PATH:ro`, `PATH:rw`
  and `--mount=…`; default mode `rw`. `--` force-terminates; first non-wrapper
  token ends the leading block.
- Subcommand stubs print `… not implemented` and return 0. Run path forwards
  to `run_passthrough()` which (for now) prints the parsed mounts/passthrough
  so the leading-block behaviour is observable — replace its body in T8.
- `requires-python >=3.11`; host is 3.14.4, sandbox (ubuntu 24.04) will be 3.12.
- Editable install: `pipx install -e .` (re-run after dependency/entry changes;
  source edits are live). Console script lands at `~/.local/bin/claude-wrapper`.

**Verified:** `pipx install -e .` ✓; `claude-wrapper gc` → stub ✓;
`claude-wrapper -p hi` → passthrough `['-p','hi']` ✓;
`claude-wrapper --mount /x -- --foo` → mount `('/x','rw')`, passthrough
`['--foo']` ✓. Bonus: `--mount /y:ro chat --resume` → mount `('/y','ro')`,
passthrough `['chat','--resume']` ✓.

### 2026-05-23 — T2: Config loader + validation (`config.py`)

**Changed:** Implemented `config.py` (host-only; pure stdlib `tomllib`, no
sandbox needed). Frozen dataclasses: `Config{setup,reaper,mounts,contexts}`,
`SetupConfig{packages,provision_script}`, `ReaperConfig{stop_idle_after,
delete_unused_after,max_instances}` (durations parsed → **int seconds**),
`MountSpec{path,from_,mode,exclude}`, `Context{name,when,provision_script,
mounts}`. Public API: `parse_config(dict)` (pure, testable), `load_config(path)`
(file → Config), `ensure_user_config(dir=None)` (writes default config.toml +
provision.sh on first run, idempotent, never clobbers), `load_user_config()`
(= load_config(ensure_user_config())). All errors raise `ConfigError` with a
locatable message. Added `tests/test_config.py` (20 tests).

**Decisions / gotchas for next tasks:**
- **Naming:** config mount type is `MountSpec` (path/from_/mode/exclude) — do
  NOT confuse with `cli.Mount` (the ad-hoc `--mount` NamedTuple, path/mode only).
- **`from` is a Python keyword** → field is `from_`; read from TOML via
  `raw.get("from")`. Helpers: `m.host_path` (= `from_` or `path`, the host
  backing) and `m.is_alias`. T6 refuse-guard keys off alias `from_`/`path`.
- **All host paths are `~`-expanded at load time** (`path`, `from`, `when`,
  `provision_script`). `exclude` entries are **left relative** (sub-paths under
  the mount `path`) — T7 masking must join them onto `path`.
- **Durations** stored as **int seconds** (`"30m"`→1800, `"14d"`→1209600, bare
  int = seconds). T10 reaper consumes seconds directly.
- **Validation enforced:** required context `name` + non-empty `when` (a bare
  string is coerced to a 1-element list); duplicate names → error; `default` is
  **reserved** (it's the no-context fallback per §6, so a config can't claim it);
  mode ∈ {ro,rw}; mount needs `path`; `max_instances` ≥ 0; malformed
  TOML/duration → clear `ConfigError`.
- `SCHEMA_VERSION = 1` is exported — fold it into the T8/T10 stamp hash (§10).
- Default config ships `~/.claude` + `~/.claude.json` as global mounts
  (essential shared auth/history per §10); everything else commented as examples.
- Not yet wired into `cli.py` (subcommands are still T1 stubs); integration
  happens in T4/T8 when setup/run actually load config.

**Verified:** `python3 -m pytest -q` → **20 passed**. Covers: sample-config
load (incl. ~ expansion, alias host_path, duration parsing, string→list `when`
coercion), duplicate-name reject, malformed-TOML reject, missing name/when,
reserved `default`, invalid mode, missing path, invalid/negative durations,
missing file, and `ensure_user_config` writes-defaults + idempotent-no-clobber
(shipped default parses cleanly). Package import via editable install ✓.

### 2026-05-23 — T3: incus helpers (`incus.py`)

**Changed:** Implemented `incus.py` — a pure *mechanism* layer over the `incus`
binary (no policy: callers supply image/mappings/devices). Public surface:
`IncusError`; `cli_run` (streamed, returns rc) / `cli_quiet` (captured, returns
stdout) with `check`/`stdin_text`; `instance_info` / `container_exists` /
`is_running`; `launch` / `start` / `stop` / `delete` / `copy`; `exec_`;
`device_show` / `device_exists` / `device_add` / `device_remove` /
`invalidate_cache`; `config_set` / `config_get` / `set_idmap` / `set_apparmor`.

**Decisions / gotchas for next tasks:**
- **State queries go through `incus query /1.0/instances/<name>`** (REST → JSON),
  *not* `incus config device show` (YAML). Rationale: stdlib `json` only — the
  package is zero-dep by design (pyproject `dependencies = []`), so no PyYAML.
  `instance_info(name)` returns the parsed dict or `None` if absent; it uses the
  instance's **local** `devices` (not `expanded_devices`), which is what
  idempotent per-instance device-adds need to check. T4/T8: build on this.
- **`device_show` is process-cached** per container name (one daemon call),
  invalidated by `device_add`/`device_remove`/`delete` (and `invalidate_cache`).
  This is the §15.2 "≤ ~3 daemon calls" lever — the run path can check several
  candidate devices for one query. Pass `refresh=True` to force a re-query.
- **`exec_` keys off the numeric UID** via `--user` (DESIGN §3 — the possibly-`@`
  username never touches the exec path). Signature:
  `exec_(name, [argv...], uid=, cwd=, env=, capture=, check=, stdin_text=)`.
  `command` is a **list** (argv). T4 provisioning will lean on this heavily
  (e.g. `exec_(base, ["bash","-c", script], uid=0, stdin_text=...)`).
- **I added `exec_`, `stop`, `is_running` beyond the T3 enum** (cli_run/quiet,
  exists, device add/remove/show, copy, launch, start, idmap/apparmor/config
  set). They're pure mechanism squarely in incus.py's charter and the next tasks
  need them; no policy pulled forward.
- **bool→`"true"/"false"`** conversion in `_prop` for config/device values
  (e.g. `device_add(..., readonly=True)`). Numbers → `str`.
- **`set_idmap`/`set_apparmor`** are thin `config_set` wrappers; the *exact*
  mapping string (`"both <uid> 1000"` etc.) and apparmor rules are T4's policy.
- **Missing binary** → `IncusError` with the Ubuntu package hint. `delete`/
  `device_remove` accept `check=False` for best-effort cleanup (used by reaper/
  rebuild). `delete --force` and `stop --force` by default.
- **No unit tests for incus.py** — it's all I/O against the daemon; verified by a
  throwaway script per the Done-when (not committed). Real unit-testable logic
  lives in `mounts.py` (T6/T7).

**Verified:** Threw a real `images:alpine/3.21` container through every helper
(launch → exists/running → instance_info → config set/get → exec capture →
device add/show/exists/remove with verified bind-mount visibility *inside* the
container + cache-hit then refresh-requery → stop → CoW `copy` (confirmed the
copy is **stopped**, never started) → delete copy → delete primary → confirm
gone): **20/20 checks passed, 0 leftover containers**. `pytest -q` → 20 passed
(no regression). Note: `images:alpine/3.20` doesn't exist on the remote; the
current alias is `alpine/3.21` (irrelevant to T4, which uses `images:ubuntu/24.04`
= the `ubuntu/noble` container variant, confirmed present in the remote).

### 2026-05-23 — T4: Base build (`lifecycle.py`: `build_base`)

**Changed:** Implemented the tier-1 base build end-to-end.
- `lifecycle.py`: `build_base(cfg, *, host_user, host_uid, host_gid, home)` +
  `setup(cfg=None)` (the `setup` entry point — gathers identity from
  `os.environ["USER"]`/`getuid`/`getgid`/`$HOME`, loads user config, builds
  base; **templates are T5**, stamp/gc are T8/T10). Sequence: subuid check →
  delete-and-recreate → `launch images:ubuntu/24.04` → `set raw.idmap`
  (`uid/gid <host> 1000`) + `raw.apparmor` (`ptrace,\nsignal,\n`) →
  `restart --force` → wait-agent (sentinel `echo ok`) → wait-DNS
  (`getent hosts claude.ai`) → identity → claude install → packages →
  global provision script → global mounts → **stop** (frozen CoW source).
- `provision.py`: `install_packages` (dpkg-probe → apt only if missing) +
  `run_provision_script` (root, prepends `set -euo pipefail`; absent file →
  warn+skip, runtime error → fails setup loudly).
- `cli.py`: `cmd_setup` now calls `lifecycle.setup()`, catching
  `ConfigError`/`SetupError`/`IncusError` → stderr + rc 1.
- `incus.py`: **added `gid` param to `exec_`** (`--group`). Justified
  mechanism completion — incus defaults gid to 0 when only `--user` is given,
  and every uid-1000 exec (install now, `exec claude` in T8) needs gid 1000.

**Decisions / gotchas for next tasks:**
- **Identity rename is a direct field-exact edit** (`_IDENTITY_SCRIPT`, run as
  root): `usermod -d "$HOME" -m ubuntu` does the **home move while the login is
  still the valid `ubuntu`** (usermod -l rejects `@`), then awk rewrites field-1
  of passwd/shadow and field-1 + member-list (`$NF`) of group/gshadow,
  `ubuntu`→`$USER`. `cat >file` (not `mv`) preserves shadow perms. Verified on
  this host: `ubuntu`→`gianz`, group renamed too (`id` → `1000(gianz)`).
  **The `@`/UID≠1000 path is implemented but untested here** (host is
  gianz/1000); the mechanism mirrors the legacy approach that worked on the
  work laptop. T5+ inherit this via CoW so no re-test needed per instance.
- **sudoers is keyed by `#1000`** (`/etc/sudoers.d/claude-wrapper`), not the
  name — `@` is netgroup syntax in sudoers. T8 can rely on passwordless sudo.
- **`raw.idmap`/`raw.apparmor` + all devices propagate via `incus copy`** —
  confirmed: a CoW copy of the stopped base inherited both raw.* keys and the
  `mnt-*` devices and came up STOPPED. So T5 templates / T8 instances get
  identity + apparmor + global mounts for free from the copy.
- **Mount device naming:** `_mount_device_name(spec)` = `mnt-<md5(path)[:8]>`
  (deterministic, keyed on the *container* path). T5 reuses `_add_mount_devices`
  for context mounts; **`spec.exclude` masking is deferred to T7** (the helper
  skips it for now). Absent host sources are silently skipped (§7).
- **claude install runs as uid 1000/gid 1000** with `HOME`/`USER` env + cwd=home
  (no `su`), native method → `$HOME/.local/bin/claude`. `installMethod` read
  from host `~/.claude.json` (here: `native`, claude 2.1.150). Note the
  installer warns `~/.local/bin not in PATH` — **T8 must prepend
  `$HOME/.local/bin` to PATH** at exec time (the §12 PATH item; not baked into
  base).
- **Base is left STOPPED**; only `setup` ever builds/touches it. `setup` is
  unconditional delete-and-recreate (base holds no unique state).
- Verified `setup` is **idempotent re: packages** — `install_packages` probes
  with `dpkg-query` and only runs apt on a miss (a re-`setup` rebuilds base from
  scratch though, so it reinstalls; the probe matters for the §15.2 hot path).

**Verified:** `python3 -m pytest -q` → 20 passed (no regression).
`python3 -m claude_wrapper.cli setup` → exit 0, builds `claude-base` (claude
native 2.1.150, jq, both global mounts `~/.claude`+`~/.claude.json`), base left
STOPPED with correct `raw.idmap`/`raw.apparmor`. **§15.1** verified via a CoW
throwaway instance (the design-faithful way to inspect a never-run base):
`whoami`==`gianz`, `$HOME`==`/home/gianz` (passwd + `$HOME` + `cd ~` agree),
`id`==`uid=1000(gianz) gid=1000(gianz)`, claude binary present, **bind-mount
ownership parity both directions** (host-made & container-made files both
`gianz:gianz` on both sides), jq present, uid-keyed sudoers. Throwaway instance
+ parity temp dir cleaned up; only `claude-base` (STOPPED) remains.
**Not testable on this host:** the `@`-username / UID≠1000 leg of §15.1 and the
missing-subuid printed-sudo-line path (this host has `root:1000:1`).

### 2026-05-23 — T5: Context templates (`lifecycle.build_templates`)

**Design clarification (user-approved, folded into DESIGN §4 + §11):** a
per-context `provision_script` must run *inside* its template, but `incus exec`
needs a **running** container — which collides with "templates are never
started." Resolution chosen: **transient start during `setup` only**. A template
*with* a provision script is briefly started to run it, then stopped; a template
*without* one is never started at all. Either way the resting state is STOPPED
and the run path never starts a template (mirrors how `build_base` works). The
two other readings (build-container indirection; defer provision to instance
time) were rejected. DESIGN §4/§11 now state this exception explicitly.

**Changed:**
- `incus.py`: added `list_instances()` — all instances as REST objects in **one**
  `query /1.0/instances?recursion=1` call (name + `config` tags + `status`).
  Used by prune now; T10 reaper will reuse it.
- `lifecycle.py`: `build_templates(cfg)` (validate names → prune removed → build
  each), `_build_template` (delete+recopy from base, tag, add context mounts,
  optional provision), `_prune_templates`, `_provision_template` (transient
  start/stop, `finally: stop`), `_template_name`, `_check_template_name`, and
  constants `TEMPLATE_PREFIX`/`ROLE_KEY`/`CONTEXT_KEY`/`_NAME_RE`. Wired into
  `setup()` after `build_base`.
- `tests/test_lifecycle_names.py`: 11 unit tests for the pure name logic.

**Decisions / gotchas for next tasks:**
- **Tier tagging via incus `user.*` config, not name-parsing.** Templates carry
  `user.cw-role=template` + `user.cw-context=<ctx>`. **T8 must tag tier-3
  instances `user.cw-role=instance`** (+ `user.cw-context`) so prune/gc can tell
  the three tiers apart — context names may contain dashes, so the
  `claude-sandbox-<ctx>` vs `claude-sandbox-<ctx>-<hash8>` split is **not** safely
  name-parseable. Constants live in `lifecycle.py` (`ROLE_KEY`, `CONTEXT_KEY`).
  Follows the design's existing `user.last-used` convention (hyphens in
  `user.*` keys are valid — confirmed).
- **Prune keys off the `template` role tag** (one `list_instances()` call), so it
  never touches base (untagged) or future instances (role=instance). A template
  whose `cw-context` is absent from the loaded config is deleted; a **running**
  one is skipped with a warning (verified). Configured templates are skipped by
  prune and rebuilt by the build loop.
- **`build_templates(cfg)` takes only cfg** — no identity args. Identity / idmap
  / apparmor / **global mounts** all propagate from base through `incus copy`
  (re-confirmed: copied template carried base's `mnt-*` global-mount devices +
  raw.idmap/apparmor and came up STOPPED). `cfg.mounts` is **not** re-applied
  here — only `build_base` consumes it; templates only add `ctx.mounts`.
- **`_add_mount_devices` reused as-is** for context mounts (incl. `mode=ro` →
  `readonly=true`, verified). `spec.exclude` masking is still **deferred to T7**
  (the helper skips it). **Known edge (documented, not handled):** a context
  mount whose container `path` equals a global mount's path collides on the
  deterministic `mnt-<md5(path)[:8]>` device name (the global one is inherited),
  so `device_add` would error. Unusual config; revisit if it bites.
- **Name validation** (`_check_template_name`): `claude-sandbox-<ctx>` must match
  incus's instance-name rules (ASCII letters/digits/dashes, 2-63 chars, no
  trailing dash). Underscores/spaces/non-ASCII/over-long → `SetupError` *before*
  any destructive op. This is the first task that turns a context name into a
  container name, so the check lives here.
- **`setup()` now does base + templates**; final line notes stamp/reaper are
  T8/T10. The `user.cw-role` tagging means T10's gc can enumerate by tier.
- **Provision failure** in a template: `run_provision_script` raises (check=True)
  → setup aborts loudly; `finally: stop` still returns the half-built template to
  STOPPED (next `setup` delete+recopies it since it's still configured).

**Verified:** `pytest -q` → **31 passed** (20 config + 11 new name tests).
Throwaway integration run against the real incus daemon (two synthetic contexts —
`t5a` plain, `t5b` with a provision script + an `ro` mount; **14/14 checks**):
both templates exist + **STOPPED**; tags set; `t5a` carries its context mount +
the 2 inherited base global mounts; `t5b`'s context mount is `readonly=true`;
the `t5b` provision script ran (marker verified via a CoW throwaway, since the
template is never run in prod); removing `t5b` from config + re-`build_templates`
**pruned `t5b` and kept `t5a`**. Separate run verified the **skip-running**
branches of both `_prune_templates` and `_build_template` (warn + leave intact).
All test containers + temp dirs cleaned up; `incus list` shows only
`claude-base` STOPPED.

### 2026-05-23 — T6: Scope keying + resolution + guards (`mounts.py`)

**Changed:** Implemented `mounts.py` (host-only pure logic; the sole I/O is one
injectable `git` call). Public surface: `RefuseError`; `Resolution`
dataclass (`context`, `context_name`, `scope`, `add_project_mount`);
`resolve_context` (§6 longest-prefix), `compute_scope` (§5 covering-mount →
project root → cwd + subsumption flag), `check_cwd_allowed` (§8 refuse-guard +
denylist), `resolve` (orchestrator: guard → context → scope), `scope_hash`
(stable `md5[:8]`), `git_project_root` (the one impure helper). Added
`tests/test_mounts.py` (33 tests).

**Decisions / gotchas for next tasks:**
- **Instance-name assembly is deferred to T8** (not in mounts.py). mounts.py
  yields `scope` + `scope_hash(scope)`; **T8 forms the tier-3 name as**
  `f"{lifecycle._template_name(ctx_name)}-{mounts.scope_hash(scope)}"`. Rationale:
  the `claude-sandbox-` prefix + `_template_name` live in `lifecycle.py`; having
  mounts.py build the full name would duplicate the prefix or risk a circular
  import, and instance creation/tagging is squarely T8's charter. **Note for T8:**
  `context_name` is `"default"` (= `mounts.DEFAULT_CONTEXT`) when no context
  matches — and per §4/§5/§6 a `default` instance has **no tier-2 template**, so
  T8 must CoW it from **`claude-base`** directly (every configured context CoWs
  from its `claude-sandbox-<ctx>` template).
- **`scope_hash` = `md5(normpath(scope))[:8]`** — same algorithm as
  `lifecycle._mount_device_name` but keyed on the scope path. Equal scopes ⇒
  equal hash ⇒ shared instance (the §15.4 mechanism).
- **`Resolution.scope` doubles as the project-mount host path** when
  `add_project_mount` is True (a parity rw mount of the scope dir). When False
  the cwd is subsumed by a context mount, so **T8 adds no project mount** (§5).
- **Run-path order for T8 (matches `resolve`):** guard **first**
  (`check_cwd_allowed` raises `RefuseError` before any resolution), then context,
  then scope. T8/cli must catch `mounts.RefuseError` → stderr + rc 1 (alongside
  the existing `ConfigError`/`SetupError`/`IncusError` handling in `cmd_setup`).
- **`compute_scope`/`resolve` take an injectable `project_root_fn`** (defaults to
  `git_project_root`). Tests pass a stub to stay hermetic; **T8 just calls
  `resolve(cwd, cfg, home=…)` and lets it shell out to git** (only when the cwd
  isn't subsumed — `compute_scope` skips the git call when a covering mount hits).
- **Covering mount = broadest (shortest `path`) context mount containing the
  cwd**, over `context.mounts` only (global mounts are auth/config, never a
  workspace). Keyed on `spec.path` (container-side); the refuse-guard guarantees
  a valid cwd is never under an *alias* path, so the covering mount is always
  parity (`path` == host backing) and the choice is unambiguous.
- **Tie-break = config order** for equal-length `when` prefixes (§6 allowed
  "config order *or* a setup-time error"; chose config order — simpler, and this
  is the run path, not setup). A true tie requires two contexts to list the
  *identical* prefix; `resolve_context` uses strict `>` so the earlier config
  entry wins.
- **Denylist semantics (§8):** `$HOME` and `/` are **exact**-match denials
  (subdirs are fine); system roots (`/etc /usr /bin /boot /dev /proc /sys /run
  /var`) and alias dirs deny **at-or-under**. Out-of-home dirs (`/tmp/…`,
  `/opt/…`) are intentionally allowed. `_is_within` is **component-wise** (so
  `/a/bc` is not within `/a/b`). Paths are `normpath`-compared; symlinks are
  **not** resolved (DESIGN relies on literal host/container path identity).
- **Refuse-guard** forbids cwd at/under **either side** of any `from`-bearing
  mount (container `path` and host backing), scanned across global + all context
  mounts independent of which context the cwd resolves to.

**Verified:** `pytest -q` → **64 passed** (31 prior + 33 new), no regression.
New tests cover: `_is_within` boundary (incl. `/a/bc` ∉ `/a/b`); §6 resolution
(no-match→default, simple, longest-prefix-wins both orders, OR semantics,
exact-length tie→config order); §15.4 covering-mount (A & B same scope/hash, no
project mount) + broadest-of-nested; §15.3 per-cwd isolation (distinct project
roots → distinct scopes/hashes, each with a project mount) + cwd fallback when
no repo; §15.7 guards ($HOME exact refused but subdir allowed, `/` refused,
system roots refused, alias `from`/`path` refused, parity mount + out-of-home
allowed); and the `resolve` orchestrator (covering, default+project-mount,
guard-first). `git_project_root` smoke-tested against real git: repo root +
subdir → toplevel; `/tmp` → None.

### 2026-05-23 — T7: Masking + whitelist (`mounts.py` + `lifecycle._add_mount_devices`)

**Changed:**
- `mounts.py`: added the masking primitives — `MASK_DIR`
  (`~/.cache/claude-wrapper/empty`), `mask_container_paths(spec)` (pure: joins
  each relative `exclude` entry onto the container-side `path`, normpath'd;
  strips a leading `/` so an entry can't escape the mount), and
  `ensure_mask_dir(path=MASK_DIR)` (idempotent host I/O: create the shared empty
  dir at **mode 555**, return its path; `path` injectable for tests).
- `lifecycle.py`: added `_mask_device_name(container_path)` = `msk-<md5[:8]>`
  and wired masking into `_add_mount_devices` — after each parent `mnt-*` disk
  device, one nested `msk-*` disk device per excluded sub-path
  (`source=<empty dir>`, `path=<excluded container path>`, `readonly=true`),
  ensuring the empty dir lazily (only when something is actually excluded).
  Imports `ensure_mask_dir, mask_container_paths` **by name** from `.mounts`
  (not the module) so they don't shadow the `mounts` parameter.
- `tests/test_mounts.py`: +6 unit tests (mask path join / nested / leading-slash
  / normalise / empty; `ensure_mask_dir` creates-empty-555 + idempotent).

**Decisions / gotchas for next tasks:**
- **Device-ordering — VERIFIED against the real daemon (resolves the §16 open
  item).** incus applies the mask *on top of* its parent: in the throwaway run
  the excluded dir was empty and the real `secret.txt` unreadable. Two
  independent guarantees stack: (a) incus mounts disk devices in target-path
  depth order (parent shorter → first), and (b) the `mnt-`/`msk-` prefix split
  makes **every** `msk-*` sort after **every** `mnt-*` by device name (verified:
  `min(msks) > max(mnts)`). Either ordering rule alone suffices; both hold.
- **Whitelist (§15.6) needed zero new code** — it's just "mount each allowed
  path as its own entry"; the existing `_add_mount_devices` already does this and
  the unmounted parent means a non-listed sibling is absent inside. Verified.
- **`mask_container_paths` is keyed on the container-side `path`** (= `spec.path`,
  not `host_path`); `exclude` entries are sub-paths *of the mount location*, so an
  aliased mount masks relative to its container path. The mask `source` is always
  the shared empty dir, never the host backing.
- **Masking is unconditional on the excluded location** — added even if the host
  sub-path doesn't currently exist (the overmount is a static default-deny on
  that container path; cheap and future-proof). Masks are skipped only when the
  *parent* source is absent (the whole mount is skipped, so there's nothing to
  mask).
- **`MASK_DIR` is host-shared across all instances/templates** — one dir, many
  read-only bind mounts. mode 555 + `readonly=true` device are belt-and-suspenders
  (verified: the masked path is read-only inside). T8/T10 don't need to special-
  case mask devices — they propagate down the CoW chain by name like any `mnt-*`.

**Verified:** `pytest -q` → **70 passed** (64 prior + 6 new), no regression;
clean imports (no circular import from lifecycle→mounts). Throwaway integration
run off `claude-base` (CoW → attach mounts via the real `_add_mount_devices` →
start → assert as uid/gid 1000 → delete): **10/10 checks** — mask device created;
`msk-*` sorts after `mnt-*`; public file readable; **excluded dir empty inside**;
**real secret unreadable** (mask on top); masked path is a (read-only) dir;
whitelisted A & B present + readable; **non-whitelisted sibling C absent**. Only
`claude-base` (STOPPED) remains; temp dirs cleaned up.

### 2026-05-23 — T8: Run path + instance lifecycle (`lifecycle.run`)

**Changed:**
- `lifecycle.py`: added the run path + stamp. `run(session_mounts, passthrough)`
  = stamp drift check → `resolve` → instance name → `_ensure_instance` →
  ad-hoc `--mount` → bump `user.last-used` → `exec claude` (returns claude's rc,
  instance left running). Stamp helpers: `_state_dir` (`$XDG_STATE_HOME` →
  `~/.local/state/claude-wrapper`), `_stamp_path`, `_config_stamp` (=
  `md5(SCHEMA_VERSION + config.toml bytes)`), `_read_stamp`/`_write_stamp`.
  `_ensure_instance` (cold: CoW from template/base, tag role+context, project
  mount unless subsumed, start, wait agent+DNS; warm: start if stopped).
  `_exec_env` (HOME/USER + `~/.local/bin` PATH prepend + forwarded
  TERM/locale/`ANTHROPIC_*`/`CLAUDE_*`). `_add_session_mounts` (ad-hoc `--mount`
  → idempotent disk devices). Added `LAST_USED_KEY = "user.last-used"`.
- **`setup()` now writes the stamp** (and uses `ensure_user_config` +
  `load_config` so it has the path to fingerprint). So both manual `setup` and
  the auto-setup path leave a matching stamp → next run is the fast path.
- `cli.py`: `run_passthrough` now calls `lifecycle.run`, catching
  `RefuseError`/`ConfigError`/`SetupError`/`IncusError` → stderr + rc 1.
- `tests/test_lifecycle_stamp.py`: 5 unit tests for the pure stamp logic.

**Decisions / gotchas for next tasks:**
- **Instance name** = `f"{_template_name(ctx_name)}-{scope_hash(scope)}"`; the
  CoW **source** is `claude-base` for the `default` context (no template) and
  `claude-sandbox-<ctx>` otherwise (the T6 note, now implemented). Tier-3
  instances are tagged `user.cw-role=instance` + `user.cw-context=<ctx>` — **T10
  gc/reaper enumerates by these tags** (base untagged, templates `=template`).
- **Daemon-call budget (§15.2) — MEASURED.** Warm 2nd launch = exactly **3**
  daemon calls: `query` (`instance_info`) + `config` (`config_set` last-used) +
  `exec` (= claude starting). Only **2 before claude starts**. The hot path adds
  **no** project-mount/device check (project mount is persistent, added once at
  creation) and `_add_session_mounts([])` returns before any `device_show`, so
  an empty `--mount` costs zero calls. **Keep this lever in mind for T9/T10** —
  any per-run MCP/reaper work must not blow the ≤3 budget on the no-op case.
- **PATH resolves bare `claude`.** `exec_` with `--env PATH=$HOME/.local/bin:…`
  lets incus find the native-install `claude` by name (verified: rc 0, version
  printed). T9's MCP bridge can rely on the same env path.
- **Stamp lives at `~/.local/state/claude-wrapper/stamp`** (XDG_STATE_HOME).
  **T10's `last-reap` stamp belongs in the same `_state_dir()`** — reuse it.
- **Auto-setup is "exactly once":** `setup()` writes the stamp at the end, so a
  drift run setups + stamps, and the next run matches → no second setup
  (verified with a counter). A schema bump *or* any config edit flips the stamp.
- **Ad-hoc `--mount` is wired but persists** (added as idempotent disk devices
  on the scope-shared instance, so it lingers for later same-scope sessions).
  Accepted/flagged — not an acceptance criterion (§15 never exercises `--mount`).
  If true per-session semantics are ever wanted, that needs per-session
  teardown (not built). Surface to the user if it bites.
- **T8 does NOT reap.** Instances are left running; the amortized background
  reap that §10 lists as the run path's tail is **T10** (with `gc`/`delete`).

**Verified:** `pytest -q` → **75 passed** (70 prior + 5 stamp). Throwaway
integration run against the real daemon (stamp pre-seeded to reuse the existing
`claude-base`, temp `XDG_STATE_HOME` so real state is untouched; **17/17
checks**): cold run created `claude-sandbox-default-<hash>`, ran `claude
--version` (rc 0, 2.1.150) as uid 1000, `whoami`==`gianz`, `$HOME` correct,
project mount = parity scope, role/last-used tags set, cwd visible inside; warm
run = 3 daemon calls (`query`/`config`/`exec`), no apt/dpkg, no copy/launch;
stamp drift → exactly one auto-setup + rewrite, no re-setup on the next run.
Cleaned up — only `claude-base` (STOPPED) remains, real `~/.local/state`
untouched. **Not exercised here:** an interactive TUI session (verified via
`claude --version`, which proves claude launches in the right instance) and the
`@`-username leg (this host is gianz/1000, same as T4).
