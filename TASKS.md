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

- [ ] **T4 — Base build (`lifecycle.py`: `build_base`).** `setup` builds
  `claude-base` per DESIGN §3/§11/§12: launch `images:ubuntu/24.04`, rename user
  to `$USER` via `/etc/passwd`/`/etc/group` edit, home = `$HOME` (`usermod -d -m`),
  `raw.idmap` host→1000, subuid detection + **print** sudo line, `raw.apparmor`
  ptrace+signal, DNS wait, claude install + install-method detect, `[setup]`
  packages, global `provision_script`, global `[[mounts]]`. **Done when:**
  acceptance §15.1 passes (whoami==$USER, $HOME correct, bind-mount ownership
  parity; missing subuid → printed sudo line + exit).

- [ ] **T5 — Context templates (`lifecycle.py`: `build_templates`).** CoW
  `claude-base` → `claude-sandbox-<ctx>` per context, add context mounts +
  per-context `provision_script`; prune templates for removed contexts; skip +
  warn on running containers; never start a template. **Done when:** templates
  exist with correct devices, are STOPPED, and removing a context from config +
  `setup` prunes its template.

- [ ] **T6 — Scope keying + resolution + guards (`mounts.py`, pure logic).**
  Context resolution (longest-prefix over `when` lists, OR semantics), scope =
  broadest covering context mount → project root (`git rev-parse --show-toplevel`)
  → cwd; subsumption rule; refuse-guard (cwd in any alias `from`/`path`); cwd
  denylist (DESIGN §5/§6/§8). **Done when:** unit tests cover §15.3/§15.4/§15.7
  cases (incl. the `~/work/A` vs `/B` same-instance result).

- [ ] **T7 — Masking + whitelist (`mounts.py`).** `exclude` → nested empty-RO
  overmount device; **verify incus applies nested device after parent**.
  **Done when:** §15.5 (excluded path empty inside) and §15.6 (whitelisted
  sibling absent) pass.

- [ ] **T8 — Run path + instance lifecycle (`lifecycle.py`: `run`).** Stamp
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
