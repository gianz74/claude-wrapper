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

- [x] **T9 — MCP/IDE bridge (`mcp.py`).** Port the preserved fixes (DESIGN §12)
  targeting the selected instance: `--mcp-config` file staging+mount, loopback
  proxy devices, uid-1000 sentinel, lockfile pid + trailing-slash patches.
  **Done when:** §15.8 (`claude-code-ide` connects end-to-end from an Emacs
  buffer under a context) and §15.9 (two contexts concurrently, no `~/.ssh`
  collision) pass.

- [x] **T10 — Reaper + gc + delete (`lifecycle.py`).** Leave-running + amortized
  background reap (local `last-reap` stamp, >1h → background pass), `[reaper]`
  thresholds (stop_idle/delete_unused/max_instances via `user.last-used`), `gc`
  subcommand, `delete [name]` (one context vs all, `[y/N]`). **Done when:**
  §15.10 passes.

- [x] **T11 — Host install shim + claude-discovery guard (`lifecycle.py`,
  setup-side).** Make `claude` (not `claude-wrapper`) the run-path command and
  make the in-container claude survive `~/.local/bin` being a global mount
  (DESIGN §13/§11/§12/§8). Two halves:
  - **In-container (mechanism):** in `build_base`, between `_install_claude`
    (`lifecycle.py:335`) and `_add_mount_devices` (`:340`) — while
    `~/.local/bin/claude` is still the container's own symlink — resolve the
    installed binary for the **native** method (`readlink -f ~/.local/bin/claude`
    → `~/.local/share/claude/versions/<v>`) and create
    `/usr/local/lib/claude-wrapper/bin/claude` → that binary; then prepend
    `/usr/local/lib/claude-wrapper/bin` to the exec PATH in `_exec_env`
    (`:585`), ahead of `~/.local/bin`. (Non-native install at `/usr/bin/claude`
    is not under a mount, so it needs no launcher; the prepend is harmless.) Bare
    `["claude", …]` exec stays.
  - **Host checks (detect → print → refuse, never mutate; the `_check_subuid`
    idiom):** add a setup-side host-readiness check that (a) **hard-refuses** if
    any configured mount's container `path` is at/above `~/.local/share/claude`
    or the private launcher dir (the §8 claude-shadow guard); and (b) **resolves
    `claude` against the user's `$PATH`** (replicate the shell's first-match
    lookup) and, if the winner is not the wrapper
    (`~/.local/bin/claude-wrapper`), **prints suggested commands** — a `claude`
    symlink to the wrapper in a PATH dir of the user's choosing, ordered ahead of
    the real binary (do **not** mandate `~/bin` or any specific dir) — plus a flag
    for any leftover `~/.local/bin/claude-wrapper.py`/`.sh` to remove. All checks
    live in `setup` only (the run path inherits the shadow-refuse via stamp-drift
    auto-setup). The package never creates the shim, edits the rc, or rm's legacy
    files.
  **Done when:** §15.11 passes — with `~/.local/bin` as a global mount, a launch
  execs the container's own claude (verify via a CoW throwaway: the private
  launcher resolves over the shadowed `~/.local/bin`); a config mounting
  `~/.local/share/claude` is refused by `setup` with a clear message; and on a
  host where `claude` doesn't resolve to the wrapper, `setup` prints suggested
  commands. Unit-test the pure pieces (shadow-guard path logic reusing
  `mounts._is_within`; the `claude`-resolves-to-wrapper `$PATH` check given an
  injected `$PATH` + filesystem facts).

- [x] **T12 — Recreate stale instances on source rebuild (`lifecycle.py`).**
  Close the gap where `setup` rebuilds tiers 1–2 but leaves existing tier-3
  instances on the old rootfs, so a `[setup].packages` (or `provision_script` /
  context-mount) change never reaches an already-created per-cwd instance until
  it is manually deleted (DESIGN §4/§9/§10). Stamp each source with a build
  identity and recreate drifted instances:
  - **Stamp the source:** in `build_base` and `_build_template`, set a
    `user.cw-build` key (epoch seconds, or a content hash of the inputs that
    define the rootfs — packages + provision script + relevant config) on
    `claude-base` / each `claude-sandbox-<ctx>`. Instances inherit it through
    `incus copy` as their "built-from" marker; `_ensure_instance` must not
    overwrite it (it already only sets role/context).
  - **Detect + recreate on the warm path:** in `_ensure_instance`
    (`lifecycle.py:799`), before reusing an existing instance, compare its
    inherited `user.cw-build` against the *current* `user.cw-build` of its
    source (`BASE`, or the context template). On mismatch, delete and fall
    through to the cold CoW path. **Respect the T10 liveness guard** — never
    yank an instance out from under a live `claude` session
    (`_has_live_session`); if a stale instance is live, warn + reuse it this run
    (it recreates on the next idle run).
  - **Decide lazy vs. eager** (surface if it needs a design call): lazy
    recreation in `_ensure_instance` only rebuilds instances you actually use;
    the alternative is `setup`/`reap` proactively deleting every instance whose
    `cw-build` predates its source. Lazy keeps the §15.2 hot-path budget
    untouched on the common (non-drifted) case — a single extra tag read folded
    into the existing `instance_info`/`device_show` calls.
  **Done when:** after `setup`, adding a package to `[setup].packages` and
  re-running the wrapper in a dir whose instance **already exists** recreates
  that instance and the new package is present inside (the 2026-05-24 scenario:
  `git` absent from a pre-existing `claude-sandbox-personal-<hash>`); an
  unchanged-config re-run does **not** recreate (no `cw-build` drift) and stays
  within the §15.2 daemon-call budget; a stale instance with a live session is
  left running and reused. Unit-test the pure drift decision (instance build-id
  vs. source build-id → recreate?) with injected tag values.

- [x] **T13 — `${VAR}` config expansion (`config.py`, DESIGN §7.1).** Add a
  `[vars]` table + `${NAME}` substitution so per-machine configs stop repeating
  long path prefixes (TOML has no native interpolation — this is our loader's
  own pre-pass). Implement as a **single pre-pass over the raw parsed-TOML dict**
  *before* the existing section parsers run, so those parsers (and their `~`
  `_expand`) are untouched:
  - Parse `[vars]` first into a `name → str` map (flat table; values used
    verbatim — a `${…}` inside a var value is **not** resolved, no recursion).
  - Recursively walk every other string in the dict and replace `${NAME}`
    (brace form only — leave bare `$NAME` literal so `$`-paths survive). Names
    match `[A-Za-z_][A-Za-z0-9_]*`. Undefined `${NAME}` → `ConfigError` naming
    the key. **Do not** walk the `[vars]` table itself.
  - Order: `${VAR}` substitution happens before `~` expansion (so `${WM}` may
    itself start with `~`). Strip `[vars]` from the dict before the section
    parsers see it. Bump `SCHEMA_VERSION` → 2 (folds into the §10 stamp).
  **Done when:** a config with `[vars] WM = "~/x"` and `from = "${WM}/.gnupg"`
  loads with `from_` == `/home/<user>/x/.gnupg`; an undefined `${NOPE}` raises a
  clear `ConfigError` naming `NOPE`; a literal `$HOME`-style string with no
  braces is left untouched; existing var-less configs parse identically. Unit
  tests in `tests/` cover all four.

- [x] **T14 — Mount groups + context `include` (`config.py`, DESIGN §7.2).**
  Add reusable named mount bundles so several contexts can share one set of
  mounts (e.g. credential mounts across `~/work` sub-tree contexts) without
  duplicating entries or inventing a `when`-bearing parent. Confine the whole
  change to `parse_config` — flatten at parse time so **nothing downstream
  changes**:
  - Parse `[mount_groups.<name>]` (each has a `mounts` array parsed with the
    existing `_parse_mount`, inline or full tables). Build a `name → tuple[MountSpec]`
    map. Groups are parse-time-only — **not** stored on `Config`.
  - Add an optional `include` to a context (`_str_list`, single string ok).
    Unknown group name → `ConfigError` naming it (and the context).
  - **Flatten into `Context.mounts`:** included groups in `include` order, then
    the context's own inline mounts; **dedupe by container-side `path` with
    later-wins** (inline overrides included; later group overrides earlier). The
    resulting `Context.mounts` is the only thing downstream sees — `build_templates`,
    `_template_build_id` (T12), scope-keying (§5) and masking/guards (§8) need
    **no change** because they already operate on `Context.mounts`. Verify the
    build-id picks up the flattened set (so changing a group recreates the
    instances of every context that includes it).
  - Refresh `_DEFAULT_CONFIG_TOML` to show `[vars]` + a `[mount_groups]` +
    `include` example (mirroring DESIGN §7.1/§7.2). `SCHEMA_VERSION` is already
    at 2 from T13 (bump only if T14 lands first).
  **Done when:** two contexts each `include = ["creds"]` (a group of three
  `from`-aliased credential mounts) both resolve to those three mounts plus their
  own; an inline mount with the same `path` as a group mount **overrides** it
  (later-wins, asserted on `mode`/`from_`); an unknown `include` name raises a
  clear `ConfigError`; `_template_build_id` differs when a group's mounts change.
  Unit tests cover include-order, inline override, unknown-group, and the
  build-id sensitivity.

- [x] **T15 — Context-keyed scope dedup (`mounts.py`, DESIGN §5).** Fix the
  multi-covering-mount duplication: a context with two *disjoint* covering mounts
  (e.g. `api` mounting both `~/work` and `~/workspace`) currently forks one
  instance **per covering mount** even though all of that context's instances CoW
  from the same template and expose the **union** of its mounts — i.e. they are
  byte-identical in blast radius (pure waste, no isolation gained). Key the
  *subsumed* case on the context, not the covering mount. Confine the change to
  `mounts.py` so **nothing downstream changes** (lifecycle just hashes whatever
  `Resolution.scope` it gets):
  - In `compute_scope`, when the cwd is covered by *any* context mount (today's
    "`cover is not None`" branch), return `(f"ctx:{context.name}", False)` instead
    of `(_norm(cover.path), False)`. The scope string is **only ever hashed** in
    the subsumed branch (`add_project_mount` is `False`, so T8 never uses it as a
    project-mount path — see the T6/T8 notes), which is what makes a non-path
    token safe here. The `ctx:` prefix keeps it disjoint from real path scopes.
  - `_broadest_covering_mount` no longer needs to pick the *broadest* — only
    whether *a* covering mount exists. Either keep it (its result is now only
    truth-tested) or simplify it to an `_is_subsumed(cwd, context) -> bool`
    predicate. The non-subsumed fall-through (project root → cwd, with
    `add_project_mount=True`) is **unchanged** — that is the §15.3 isolation path.
  - **No `SCHEMA_VERSION` bump:** templates and the config *shape* are untouched
    (this is instance-naming logic, not a config or template change), so the §10
    stamp does not drift. Existing old-named duplicate instances (e.g.
    `…-<hash(~/work)>` and `…-<hash(~/workspace)>`) are simply orphaned by
    the new name and reaped naturally by `gc`/the reaper (they hold no unique
    state); note this in the progress log so the user knows a one-off
    `claude-wrapper gc` clears them immediately if desired.
  - Update the `mounts.py` docstrings (module + `compute_scope`) and the existing
    T6 scope tests (the §15.4 "A & B same hash" assertions now key on the
    `ctx:<name>` constant, and `~/workspace/C` joins the same instance).
  **Done when:** unit tests show (1) a context with disjoint covering mounts
  `~/work`+`~/workspace` yields the *same* scope/hash and `add_project_mount
  =False` for cwds `~/work/A`, `~/work/B`, **and** `~/workspace/C`
  (revised §15.4); (2) the nested-mounts case (`~/work` + `~/work/foo`)
  still collapses to one instance; (3) an ssh-only context whose mounts don't
  cover the cwd still produces *distinct* project-root scopes/hashes with
  `add_project_mount=True` per project (§15.3 preserved); (4) the subsumed scope
  token is constant per context and `!=` the bare template name. `pytest -q`
  green with no regression.

- [x] **T16 — User-declared env (`config.py` + `lifecycle._exec_env`, DESIGN §7.3).**
  Let the config pass extra environment into the sandbox, both global and
  per-context, as literal values **and** host pass-through — beyond the hardcoded
  `_FORWARD_ENV`/prefix baseline. **Env is run-path-only** (applied at `exec
  claude`, never baked), so it must touch **no** rootfs: **no `SCHEMA_VERSION`
  bump**, **not** part of the T12 build-id, **never** recreates instances. Two
  halves:
  - **`config.py` (parse + validate + flatten):** parse a global `[env]` table and
    an optional per-context `env` (inline table or `[contexts.env]` sub-table).
    In each table, the reserved lowercase key `forward` is a `list[str]` of host
    var names; every other pair is a literal `KEY = "value"`. Validate: env names
    match `[A-Za-z_][A-Za-z0-9_]*`; values are strings (reject non-string with a
    clear message); `forward` is a list of strings; `HOME`/`USER`/`PATH` in any
    `[env]` → `ConfigError` (reserved — identity + §11 launcher). `${VAR}` (§7.1)
    already expands in literal *values* via the existing pre-pass (a `forward`
    name has no braces, so it's untouched) — **do not** `~`-expand env values
    (they aren't paths; keep them out of the `_expand` path fields). Store on the
    frozen models: add `env: Mapping[str,str]` + `forward: tuple[str,...]` to
    `Config` (global) and to `Context` (per-context). Flatten nothing across
    levels at parse time — keep global and per-context separate; the run path
    merges them (it already has both `cfg` and `res.context`).
  - **`lifecycle._exec_env` (merge + apply):** change the signature to take the
    config + resolved context (it's called once in `run` at `lifecycle.py:1024`,
    which already holds `cfg` and `res`). Assemble broadest→narrowest, later-wins:
    (1) reserved `HOME`/`USER`/`PATH`; (2) the existing built-in forwarded
    baseline (`_FORWARD_ENV` + `_FORWARD_PREFIXES`, `setdefault`); (3) user
    `forward` = global ∪ context names, pulled from `os.environ`, **skipped if
    unset** (same convention as an absent mount); (4) user literals — global,
    then **context overrides global**; **literals override forwarded** (explicit
    beats implicit); (5) re-assert `HOME`/`USER`/`PATH` last so nothing clobbers
    identity. Reserved-key validation in `config.py` already prevents (4) from
    setting them, so (5) is belt-and-suspenders.
  - **Refresh `_DEFAULT_CONFIG_TOML`** with a commented `[env]` example (one
    literal + one `forward` + a per-context `env`), mirroring DESIGN §7.3.
  - **Confirm the build-id is untouched:** `_template_build_id` (T12) must **not**
    fold env in — add a unit assertion that changing `[env]` leaves the build-id
    equal (so an env-only edit never recreates instances; only the generic stamp
    auto-`setup` fires).
  **Done when:** DESIGN §15.12 passes — a global `[env]` literal and a `forward`
  host var both reach the sandbox at exec time; a per-context `env` overrides the
  global literal on a key collision; a literal overrides a same-named forwarded
  var; a `forward` name unset on the host is skipped (not set empty); a reserved
  `HOME`/`USER`/`PATH` in `[env]` is rejected at load with a clear message; the
  T12 build-id is unchanged by an env edit. Unit tests cover: literal+forward
  parse, name/value/`forward`-shape validation, reserved-key rejection, `${VAR}`
  expansion in a literal value (and no `~` expansion), the full merge/precedence
  order (built-in < forward < global literal < context literal), unset-forward
  skip, and build-id insensitivity. Verify the merged env on a real `exec claude`
  (a throwaway `printenv` is enough — no full TUI).

- [x] **T17 — Build-relevant config stamp (`lifecycle._config_stamp`, DESIGN §10).**
  Stop runtime-only config edits from forcing a full rebuild, and start catching
  provision-script content edits that are currently silently ignored, by keying
  the auto-`setup` stamp on the config's **build identity** instead of raw
  `config.toml` bytes. Independent of T16 (whichever lands second reconciles the
  shared §7.3 wording). Confine to `lifecycle.py`:
  - **Re-key the stamp.** Today `_config_stamp(config_path)` (`lifecycle.py:776`)
    is `md5(SCHEMA_VERSION + config.toml bytes)`. Change it to a hash over the
    build identities the config produces: `base_id = _base_build_id(cfg)`
    (`:388`) plus `_template_build_id(base_id, ctx)` (`:410`) for every
    `ctx in cfg.contexts`, sorted for stability. **Signature change** to
    `_config_stamp(cfg: Config) -> str` — both callers already hold `cfg` (`run`
    at `:975`, `setup` at `:745`). `SCHEMA_VERSION` stays covered because
    `_base_build_id` already folds it in (`:400`), so drop the separate prepend.
  - **Why this is the whole fix (no new partition to maintain).** The build-ids
    already define "what touches the rootfs" (schema, global packages,
    provision-script **content**, global mounts, per-context name/provision/mounts).
    Anything *not* in them is runtime-only — `[env]` (T16), `[reaper]` thresholds —
    and so will not drift the stamp. `[vars]`/`[mount_groups]` are already flattened
    into mounts/paths before the build-id sees them, so a `${VAR}` used in a mount
    still drifts (correct) while one used only in an `[env]` literal does not.
  - **Two payoffs, one change.** (a) runtime-only edits no longer rebuild;
    (b) editing a `provision.sh` with `config.toml` byte-identical now drifts the
    stamp (the build-id reads `_read_provision` content) — currently that edit is
    **inert until a manual `setup`** (a footgun). Adding/removing/renaming a
    context still drifts (the set of template build-ids changes), so auto-`setup`
    builds/prunes as today.
  - **Hot-path cost.** The only new work is local file reads (the provision
    scripts) — **no daemon calls** — so the §15.2 budget is untouched. Re-measure
    the warm path with the existing harness to confirm 3 calls / 2-before-claude.
  - **Single source of truth.** After this, the auto-`setup` decision and the T12
    instance-recreation decision are both answered by the same build-id functions
    and can't disagree — note this in the `_config_stamp` docstring/comment.
  - **No `SCHEMA_VERSION` bump** (local-stamp logic; templates/config shape
    unchanged). On-disk stamps written by the old byte-hash scheme simply mismatch
    once → one harmless auto-`setup` on the first run after upgrade, then stable.
    Note in the progress log. DESIGN §10's stamp bullet, §7.3's env caveat, and
    §15.13 are already amended (this task's design commit); reconcile T16's note if
    T16 is still open.
  **Done when:** §15.13 passes — a `[reaper]`/`[env]` edit triggers no
  auto-`setup` and no recreation (stamp unchanged across the edit); a
  `[setup].packages`/mount edit, **and** a provision-script content edit with
  `config.toml` unchanged, each trigger exactly one auto-`setup`; the warm-path
  daemon-call budget matches §15.2. Unit tests (pure, no daemon): `_config_stamp`
  is **stable** across a runtime-only mutation (two `Config`s differing only in
  `env`/`reaper` → equal stamp), **drifts** on each build-relevant mutation
  (packages, a mount field, ctx add/remove), and **drifts on provision-content
  change** (point `provision_script` at a temp file, rewrite its bytes → different
  stamp with the same `Config` shape).

- [x] **T18 — HOME-relative paths in `[env]` values (`config.py` + DESIGN §7.1/§7.3).**
  Close the usability footgun found on 2026-05-24: there is **no ergonomic way to
  put a `$HOME`-relative path into an `[env]` value.** `[env]` values are literal
  by design (§7.3: "`~` is **not** expanded — env values are not paths"), the
  `${VAR}` pre-pass (§7.1) only resolves names declared in `[vars]`, and the
  consuming tool usually won't expand `~` either — so a natural
  `GIT_CONFIG_GLOBAL = "~/.config/git/config"` is passed through verbatim and
  breaks. This is also **inconsistent with mounts**, where `~` *is* expanded.
  - **Trigger (real case):** fixing `git config` against a bind-mounted
    `~/.gitconfig` (single-file mounts can't be `rename()`d over → `EBUSY`)
    requires pointing git at a config inside a *directory* mount via
    `GIT_CONFIG_GLOBAL`. Setting it with a leading `~` failed with
    `could not lock config file ~/.config/git/config: No such file or directory`
    — git treated `~` as a literal dir. An absolute path works; the gap is that
    nothing lets the user write it portably.
  - **This needs a DESIGN call first** (DESIGN.md is authoritative; §7.3 currently
    *documents* the no-expansion behavior, so changing it is a design edit, not a
    bugfix). Options:
    - **(a) Doc-only / accept.** Keep literal semantics; just teach the footgun:
      note in §7.3 + the `_DEFAULT_CONFIG_TOML` env example that values are literal
      and a HOME-relative path must be written absolute or via a `[vars]` entry.
      Lowest risk; user hardcodes `/home/<user>` (acceptable — config is
      per-machine). No code change.
    - **(b) Predefine `${HOME}`/`${USER}` as implicit `[vars]` (recommended).**
      Seed the §7.1 pre-pass with `HOME`/`USER` (still overridable by an explicit
      `[vars]` entry) so `GIT_CONFIG_GLOBAL = "${HOME}/.config/git/config"`
      resolves. Stays inside the existing brace mechanism — **preserves the
      "env ≠ path / no `~`" stance** — and, because host HOME == container HOME
      (§3 identity), one value is correct on both sides. Bonus: works in mount
      paths too (consistent). Watch: §7.1 raises on undefined `${NAME}`, so the
      seeds must be injected *before* the undefined-name check; decide whether
      `${HOME}` is also allowed in mount `from`/`path` (likely yes, harmless).
    - **(c) `~`/`$HOME` expansion in env values.** Rejected unless scoped — it
      contradicts §7.3 and would wrongly rewrite non-path values
      (e.g. `PROMPT = "~/x"`).
  - **Scope note:** this is a post-T17 enhancement, not a blocker — the wrapper
    works today with an absolute `GIT_CONFIG_GLOBAL`. The `.gitconfig`
    directory-mount pattern itself is the user's `config.toml` concern, not a
    package change.
  **Done when:** the design call is recorded in DESIGN §7.1/§7.3; if (b), a config
  with `GIT_CONFIG_GLOBAL = "${HOME}/.config/git/config"` loads with `${HOME}`
  resolved to the real home and an undefined `${NOPE}` still raises (seeds don't
  swallow real errors), with a unit test covering both and the `_DEFAULT_CONFIG_TOML`
  example refreshed; if (a), §7.3 + the default-config comment state the literal
  rule and the absolute/`[vars]` workaround. No `SCHEMA_VERSION`/build-id impact
  either way (env + `[vars]` are already runtime-only / pre-flattened per T17).

- [x] **T19 — Surface deployment-specific forwarded env into config (`[env].forward`,
  DESIGN §7.3/§12).** The hardcoded `_FORWARD_ENV` baseline in `lifecycle.py` bakes
  machine/deployment-specific vars into the otherwise-generic package. Relocate the
  deployment-specific forwards into the **shipped example config's global
  `[env].forward`** so they are explicit and auditable per-machine, keeping only the
  universal render/auth baseline hardcoded. (Decision recorded 2026-05-24 alongside
  the repo scrub; a company-specific tooling var and the `AWS_` prefix were already
  removed in that scrub.)
  - **Stays hardcoded (universal, nothing to configure):** terminal/locale (`TERM`,
    `COLORTERM`, `LANG`, `LANGUAGE`, `LC_*`), IDE hints (`TERM_PROGRAM`,
    `FORCE_CODE_TERMINAL`), and the `ANTHROPIC_*`/`CLAUDE_*` **prefixes** (a prefix
    can't be expressed as a `forward = [...]` name list, so it must stay in code).
  - **Moves to example `[env].forward`:** `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`
    (+ lowercase), `NODE_EXTRA_CA_CERTS`, `CLOUD_ML_REGION`,
    `GOOGLE_APPLICATION_CREDENTIALS`.
  - **Behavioral consequence to document:** the shipped example config is
    *documentation, not an auto-loaded default*, so relocating means these vars are
    **no longer forwarded by default on any machine** until that machine's real
    `config.toml` lists them. Intentional — it keeps the package generic (no baked-in
    proxy/cloud assumptions); a Bedrock user likewise re-adds AWS creds **by name**.
  - **Needs a DESIGN edit first:** §7.3 (line ~255) documents the baseline as
    terminal/locale + IDE hints + prefixes; narrow it to the universal set and point
    the deployment knobs at the example `[env].forward`. Update §12 if it echoes the
    prefix list.
  **Done when:** `_FORWARD_ENV` is trimmed to the universal baseline; DESIGN §7.3/§12
  reflect the narrower hardcoded set + the example `[env].forward`; `config.py`
  `_DEFAULT_CONFIG_TOML` shows the relocated vars in a global `[env] forward = [...]`;
  the README env note matches; `tests/test_exec_env.py` keeps the `TERM` baseline test
  and adds coverage that a relocated var is **not** forwarded by default but **is**
  when named in `[env].forward`. No `SCHEMA_VERSION`/build-id impact (env is
  runtime-only per T17).

- [x] **T20 — Preflight incus host-readiness in `setup` (`lifecycle.py`, DESIGN §3).**
  Close the gap where `setup` passes the existing subuid check but then dies at
  `incus.launch(IMAGE, BASE)` (`lifecycle.py:470`) with incus's opaque
  *"No uid/gid allocation configured. In this mode, only privileged containers
  are supported"*. Discovered 2026-05-25 on Ubuntu Noble host `barney`: root owned
  only the single `root:<uid>:1` idmap entry (which `_check_subuid` requires and
  which `_subid_covered` happily confirms — count-1 covers the target) but **no
  base unprivileged range**, so incus cannot build a normal unprivileged container.
  A count-1 allocation is far too small for a container's ~65536-id span. Extend
  the §3 "detect-and-instruct, never mutate" idiom (the `_check_subuid` pattern) to
  catch this *before* launch and print the fix, instead of letting the raw incus
  error through. Two detections, both setup-only (the run path inherits via
  stamp-drift auto-setup; setup is not the §15.2 hot path, so a daemon call here is
  fine — same licence as the T11 host checks):
  - **(a) Missing base root subid range (the immediate bug — must-have).** Add a
    pure predicate alongside `_subid_covered` (`lifecycle.py:98`) — e.g.
    `_subid_range_present(path, *, min_count=65536) -> bool` — that returns True iff
    some `root:start:count` line has `count >= min_count` (one container's idmap
    span). This is **distinct** from `_subid_covered`: the latter asks "is *this*
    uid mappable" (idmap prereq, count 1 ok); the former asks "can incus allocate a
    *container's worth* of ids" (unprivileged-container prereq). If absent on either
    `/etc/subuid` or `/etc/subgid`, instruct the incus-documented range and a
    restart: `echo 'root:1000000:1000000000' | sudo tee -a /etc/subuid /etc/subgid`
    + `sudo systemctl restart incus`. **Coexists with the single `root:<uid>:1`
    entry** — `1000000..1001000000` is disjoint from a normal uid (1000) and from a
    large LDAP uid (e.g. 1529911346 > 1.001e9), so both lines are independently
    needed and neither overlaps; the idmap still resolves. Keep the two prereqs as
    clearly-separate messages (or one consolidated "run these on the host" block
    covering whatever is missing) so the user doesn't conflate the idmap entry with
    the base range.
  - **(b) Uninitialised incus (the *next* wall — should-have).** Observed on the
    corp laptop the same day: `incus storage list` empty, `default` profile has no
    devices, no managed network — i.e. `incus admin init` was never run, so a launch
    would instead fail with *"No storage pool found for instance"*. Detect via a
    cheap probe (reuse `incus query` per the T3 zero-dep rule, not YAML — e.g. an
    empty storage-pool list or a device-less `default` profile) and, if
    uninitialised, print `incus admin init --minimal` (or plain `incus admin init`)
    + re-run. Refuse, do not mutate.
  - **Wiring:** call the new preflight in `build_base` right after the existing
    `_check_subuid(host_uid, host_gid)` (`lifecycle.py:463`) and before
    `incus.launch` (`:470`). Either extend `_check_subuid` into a broader
    `_check_incus_ready` or add a sibling — implementer's call; keep the idmap
    single-entry check and the range check as separate predicates so each emits its
    own targeted fix.
  - **Needs a small DESIGN edit first:** §3 (lines ~42-45) currently documents only
    the single-entry idmap prerequisite (`root:<uid>:1`) and assumes incus's base
    root range exists from installation. Add the base-range prerequisite (and,
    optionally, the `incus admin init` readiness note) to §3 so the authoritative
    doc matches; add/extend a §15 acceptance row if one fits.
  **Done when:** on a host with the single idmap entry but no base range, `setup`
  prints the `root:1000000:1000000000` + restart instructions and exits cleanly
  (no opaque incus error); on an uninitialised incus, `setup` prints the
  `incus admin init` instruction and exits; a properly-configured host is
  unaffected (preflight passes silently). DESIGN §3 records the base-range
  prerequisite. Unit tests (pure, no daemon) cover `_subid_range_present`: a file
  with only `root:<uid>:1` → False, a file with `root:1000000:1000000000` → True,
  a file with both lines → True, a missing/unreadable file → False, and the
  `count` boundary (`root:x:65535` → False, `root:x:65536` → True). The (b)
  storage/profile probe is daemon I/O — verify by a throwaway/manual run per the
  T3/T4 convention, not a unit test. No `SCHEMA_VERSION`/build-id impact (host
  preflight, touches no rootfs or config shape).

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

### 2026-05-23 — T9: MCP/IDE bridge (`mcp.py`)

**Changed:**
- `mcp.py`: ported the §12 preserved fixes from the legacy single-file wrapper,
  re-architected for the **per-cwd instance** model. `Bridge(instance, home=…)`
  is a **context manager** (was module-global session state + atexit in legacy):
  `prepare(args)` → rewritten claude args; `cleanup()` (on `__exit__`) tears down.
  Does: `--mcp-config` **file staging** (copy into a per-session `/tmp/claude-mcp-*`
  dir bind-mounted at the same path; rewrite args to staged paths; inline JSON &
  missing files pass through), **loopback proxy devices** (`mcp-proxy-<port>`,
  `bind=container`) for every port found in config files/inline JSON +
  `CLAUDE_CODE_SSE_PORT`, the uid-1000 **sentinel**, and the IDE **lockfile patch**
  (pid → sentinel; `workspaceFolders` trailing slash stripped). Pure helpers
  `extract_loopback_ports_from_text` + `normalize_workspace_folders` are unit-tested.
- `incus.py`: extracted `_exec_argv` (shared) and added **`exec_popen`** — a
  non-blocking `incus exec` returning a `subprocess.Popen`, needed because the
  sentinel must stay alive while we read its pid from stdout (`exec_` blocks).
- `lifecycle.py`: wired the bridge into `run` (`with mcp.Bridge(...) as bridge:`
  around `prepare` + `exec claude`), and **expanded `_exec_env`** to forward the
  IDE hints (`TERM_PROGRAM`, `FORCE_CODE_TERMINAL`) + cloud/proxy/cert knobs +
  the `AWS_` prefix. Installed SIGTERM/SIGHUP→SystemExit handlers (restored in
  `finally`) so the bridge's cleanup fires on those too.
- `tests/test_mcp.py`: 14 unit tests (port extraction, folder normalisation,
  daemon-free arg-rewrite branches, lockfile patch).

**Decisions / gotchas for next tasks:**
- **Sentinel is `sh -c 'echo $$; exec sleep infinity'`** (was `python3 -c` in
  legacy). `echo $$` prints the container-ns pid; `exec` preserves that pid →
  no python3 dependency in the base image. Verified: real live pid, owned by
  uid 1000, killed on stop.
- **§15.2 budget preserved (re-measured):** with the bridge in the run path, a
  warm no-MCP launch is still **3 daemon calls** (`query`/`config`/`exec`), 2
  before claude. `Bridge.prepare`/`cleanup` touch the daemon **zero** times when
  there's no `--mcp-config` and no `CLAUDE_CODE_SSE_PORT` — **keep this invariant**
  if T10's reaper adds run-path work.
- **Proxy `bind=container`** ⇒ the listener lives in the *container* netns, so two
  instances can both listen on the *same* `127.0.0.1:PORT` with no host
  collision (verified concurrently). This is the structural basis for §15.9 — and
  why the legacy "detect-and-refuse" ~/.ssh problem is gone: different scope →
  different instance → independent devices/sentinels (proven: tearing down A left
  B's proxy + sentinel intact).
- **Credential *file* mounts are NOT ported** (legacy hardcoded `aws-dir`,
  `node-ca-certs`, `gcp-app-creds`, `workspace-specs`). Per DESIGN §7 these are
  now user `[[mounts]]` in config.toml; T9 only forwards the matching env vars.
- **Lockfile lives under the globally-mounted `~/.claude`**, so patching the host
  file is what container claude reads (same inode via the bind mount). The patch
  uses atomic `os.replace`, leaving Emacs's own bookkeeping (cleanup-by-name) intact.
- **`split_project_dir`/`--delete-container` from legacy are intentionally gone**
  (DESIGN §9: always cwd; delete is the `delete` subcommand — T10).

**Verified:** `pytest -q` → **89 passed** (75 prior + 14 mcp). Throwaway
integration run against the real daemon off `claude-base` (two scratch instances;
**28/28 checks**): loopback proxy actually forwards container→host (`bash
/dev/tcp`, host listener received the marker); sentinel is a real live uid-1000
pid that dies on stop; `--mcp-config` file is staged + bind-mounted + visible
inside + its port proxied, then fully cleaned up (device + host dir gone);
no-MCP `prepare` adds zero devices; the SSE path adds both SSE + inline-JSON
proxies, patches the lockfile pid → sentinel and strips the workspace trailing
slash; **§15.9** two instances ran concurrently with same-port proxies + live
sentinels each, and tearing down A left B intact. Cleaned up — only
`claude-base` (STOPPED) remains. **NOT automatable here (needs the user,
interactively):** §15.8's actual Emacs + claude-code-ide WebSocket *handshake*
(MCP tools listed, diagnostics flowing). Every mechanism it depends on is
verified above; to confirm the live handshake, open an Emacs project buffer
under a context and check `claude-code-ide` connects. §15.9 likewise verified
mechanically; a true dual-IDE session would be the final human check.

### 2026-05-23 — T10: Reaper + gc + delete (`lifecycle.py`)

**Design decision (user-approved):** the reaper keys off `user.last-used`, which
is bumped only **at launch** (§10). A long live session therefore has a stale
last-used, so a concurrent `gc`/background-reap would `stop --force` it
mid-work. "Always safe — instances hold no unique state" justifies *deletion*
(files live on host bind-mounts) but **not** killing a live TUI. Resolution
(user chose "skip live sessions"): a **liveness guard** — the reaper never
stops/deletes a *running* instance that still has a live `claude` process; the
session's own next run re-stamps last-used, so it ages out only once actually
idle. Off the hot path, so the extra probe cost is fine; doesn't affect §15.10
(which exercises an *exited* session).

**Changed (`lifecycle.py`):**
- Pure decision core `plan_reap(instances, reaper, now) -> ReapPlan(stop, delete)`
  (no I/O — unit-tested): phase 1 delete unused past `delete_unused_after`;
  phase 2 stop *running* survivors idle past `stop_idle_after`; phase 3 LRU-trim
  survivors beyond `max_instances` (oldest by last-used first). A **0 threshold
  disables its phase** (matches `max_instances=0`=unlimited; avoids the
  always-true-age footgun). Delete wins over stop on overlap.
- Executor `reap(cfg) -> ReapResult(stopped, deleted, skipped_live)`: one
  `list_instances()` call → `_tier3_instances()` (filters `user.cw-role=instance`)
  → `plan_reap` → execute, applying the **liveness guard** (`_has_live_session`)
  to running candidates only.
- `_has_live_session(name)`: dependency-free `/proc` scan for a process whose
  `comm` is `claude` (`sh -c 'for c in /proc/[0-9]*/comm; …'`, run as uid 0) —
  **no pgrep/procps needed** in the base image. Verified: a process exec'd from
  a binary named `claude` reports comm `claude`.
- `gc(cfg=None)`: foreground pass + writes the reap stamp + prints a summary.
- `delete_containers(name=None, *, assume_yes)`: no name → base + all
  role-tagged templates/instances (`[y/N]`), then **clears the config stamp** so
  the next run auto-`setup`s; a name → that context's `claude-sandbox-<name>`
  template + its `cw-context`-tagged instances only (base/other contexts
  untouched, **no** stamp clear). Confirmation on both modes; `-y/--yes` skips.
- Amortized background reap: `_reap_due()`/`_read`/`_write_reap_stamp` (stamp =
  `last-reap` in the same `_state_dir()` as T8's config stamp). `run()` calls
  `_maybe_background_reap()` right after the last-used bump — it **claims the
  slot** (writes the stamp) then spawns a **detached** `python -c
  "…_reap_main()"` (`start_new_session`, std streams → DEVNULL). `_reap_main()`
  loads config + reaps, swallowing all errors (silent best-effort).
- `setup()` now runs a closing `reap(cfg)` + writes the reap stamp (§9 "run gc").

**Changed (`cli.py`):** `cmd_gc` → `lifecycle.gc()`; `cmd_delete` parses optional
`<name>` + `-y/--yes` → `lifecycle.delete_containers(...)`; both catch
`ConfigError`/`SetupError`/`IncusError` → stderr + rc 1 (bad option/too many args
→ rc 2). Added `tests/test_lifecycle_reaper.py` (20 tests).

**Decisions / gotchas (project is now feature-complete — T1–T10 done):**
- **§15.2 budget preserved:** `_maybe_background_reap` does **zero** daemon calls
  on the hot path — only a local stamp read, and (when stale) a stamp write +
  `Popen` (neither is a daemon call). When the stamp is fresh (the §15.2 "2nd
  launch" case) it's a pure no-op, so the warm path is still exactly 3 daemon
  calls. The detached child's calls run in a separate process. **Keep this** if
  anything else is ever added to the run path.
- **`-y/--yes` on `delete` is an addition** beyond the §9 surface (the design
  only specifies `[y/N]`). Justified for scripting/cleanup and it's the
  conventional flag; the interactive default is still confirm. Named-mode delete
  also confirms (design only mandated it for delete-all) — cheap safety, and a
  delete is reversible via `setup` anyway.
- **`max_instances` LRU + a skipped live session:** if the oldest-beyond-cap
  instance is live, the guard skips it, so a pass may not fully reach the cap;
  the next pass retries. Accepted (can't kill a live session to satisfy a count).
- **Orphan handling:** a tier-3 instance with a missing/garbage `user.last-used`
  reads as epoch 0 → ancient → deleted by the `delete_unused_after` phase. This
  is the intended "stale/orphan" cleanup `gc` is for.
- The CLI subcommand set (`setup`/`delete`/`gc`) and run path are unchanged from
  T1's dispatch; T10 only filled the `delete`/`gc` stubs.

**Verified:** `pytest -q` → **109 passed** (89 prior + 20 new). Throwaway
integration run against the real daemon (hermetic temp `XDG_STATE_HOME`, all
instances CoW'd from the existing `claude-base`; **25/25 checks**): liveness
probe true/false correct; an idle running instance was **stopped** while a
**live-claude instance was left Running** (guard) and reported `skipped_live`;
an unused instance past `delete_unused_after` was **deleted**; LRU trim with
`max_instances=2` deleted **exactly the oldest** of three and kept the two
newest; `delete t10ctx` removed that context's template + instance and **left a
different context's instance untouched**; `delete` (all, BASE pointed at a
throwaway copy) removed base + templates + instances, **cleared the config
stamp**, and **left the real `claude-base` intact**; reap-due gate false when
fresh / true when stale. All throwaways cleaned up; `incus list` shows only
`claude-base` STOPPED. **Not exercised here:** the live background-reap *spawn*
during a real interactive session (the gate + `reap()` it would call are both
verified; the `Popen` is trivial) and a wall-clock-aged reap (last-used is
seeded directly, which is equivalent).

### 2026-05-24 — T11 added (design amendment; NOT yet implemented)

**Context:** Grill on "make the installed `claude-wrapper` executable replace the
legacy `~/.local/bin/claude-wrapper.py`." Outcome is **design only** — folded into
DESIGN §13 (rewritten "Packaging & host install"), §11 (container-private claude
launcher), §12 (table row), §8 (claude-shadow guard), §15.11 (new criterion) —
and a new **T11** task above. No code changed.

**Findings / decisions (so the T11 session has full context):**
- **The `claude-wrapper` entry point already exists** — pipx materialised it from
  `[project.scripts]` in T1 (`~/.local/bin/claude-wrapper` → pipx venv). So "is it
  possible" was already done; the real work is the *replacement* + host wiring.
- **Invocation = `claude`** (not `claude-wrapper`) for the run path, via a
  `claude` symlink to the wrapper. **User declined a mandated location:** the
  package must not force `~/bin` — it only requires the *outcome* that `claude`
  resolves to the wrapper, ahead of the real binary, in whatever PATH dir the
  user picks. So the setup check is **PATH-resolution-based** (replicate the
  shell's first-match lookup; is the winner the wrapper?), not a `~/bin/claude`
  file check. (Avoid repointing `~/.local/bin/claude` — the native installer owns
  it and may clobber.) The printed guidance suggests a symlink + PATH ordering
  but leaves the directory to the user.
- **The real run-path gap the user surfaced:** `run` execs **bare `["claude"]`**
  (`lifecycle.py:707`) and `_exec_env` prepends `$HOME/.local/bin` (`:585`), where
  the native install lives. The user intends to **mount host `~/.local/bin` into
  the container** once they customise config; that bind mount shadows the
  container's own claude, so bare `claude` would resolve to the host shim →
  recursion/breakage. Fix (T11): a container-private launcher at
  `/usr/local/lib/claude-wrapper/bin/claude` (**outside `$HOME`**, so no home
  mount can shadow the dir), created in `build_base` between install (`:335`) and
  mount-attach (`:340`), prepended to the exec PATH. Native install + bare-name
  exec are otherwise unchanged.
- **Derisking facts (verified during the grill):** the native claude is a single
  self-contained ELF at `~/.local/share/claude/versions/<v>` (launcher points
  straight at it; no support files), and host `~/.claude.json` has
  **`autoUpdates:false` + `autoUpdatesProtectedForNative:true`** — mirrored into
  the container via the existing `~/.claude.json` global mount — so no
  in-container self-update will try to rewrite `~/.local/bin/claude` *through* the
  host mount. Claude is refreshed only by `setup` (fits the frozen base).
- **Mechanism = detect → print → refuse, never mutate** (the `_check_subuid`
  idiom, §3). Package never creates the shim / edits the rc / deletes legacy. **All
  host checks live in `setup` only** (user choice); the run path inherits the
  shadow-refuse because editing config drifts the stamp → auto-setup → refuse.
- **Two severities:** hard-refuse a mount covering `~/.local/share/claude` or the
  launcher dir (silent breakage); advisory-print when `claude` doesn't resolve to
  the wrapper, plus a note about leftover legacy `.py`/`.sh`.
- **Private launcher location** chosen **outside `$HOME`** over the user's
  original `~/.<hash>` idea — categorically immune to home mounts shadowing the
  *dir*; the symlink *target* under `~/.local/share/claude` still needs the §8
  guard regardless. Implementer may adjust the exact path if there's a reason.
- **Legacy on host (for the migration the printed guidance covers):** `claude` →
  real binary `versions/2.1.150`; `claude-wrapper.py` (27 KB) + `claude-wrapper.sh`
  (older) still in `~/.local/bin`; no shell alias redirects `claude`. The legacy
  `.py` header used `ln -s … ~/bin/claude` as *one* example shim location — T11
  suggests but does not mandate any particular dir.

**Verified:** none (no code). DESIGN + TASKS edits only; implement T11 in a clean
context per the workflow.

### 2026-05-24 — T11: Host install shim + claude-discovery guard

**Changed (`lifecycle.py`):**
- **In-container launcher (mechanism).** Added `LAUNCHER_DIR =
  "/usr/local/lib/claude-wrapper/bin"` (outside `$HOME`), `_LAUNCHER_SCRIPT`, and
  `_install_private_launcher(home, method)`. Wired into `build_base` **right after
  `_install_claude`** (capturing `method` once): for the **native** method it runs
  the script as root while `~/.local/bin/claude` is still the container's own
  symlink — `readlink -f` → the self-contained ELF under
  `~/.local/share/claude/versions/<v>` → `ln -sfn` it at `LAUNCHER_DIR/claude`.
  Non-native (`/usr/bin/claude`) is a no-op (not under a home mount). `_exec_env`
  PATH now **prepends `LAUNCHER_DIR` ahead of `~/.local/bin`**, so bare `claude`
  resolves to the private launcher even when host `~/.local/bin` is mounted over
  the container's. Bare `["claude", …]` exec unchanged.
- **Host checks (detect → print → refuse, never mutate).**
  `_check_no_claude_shadow(cfg, home)` — **hard-refuse** (raises `SetupError`) if
  any global *or* context mount's container `path` is at/above
  `~/.local/share/claude` or `LAUNCHER_DIR`, via
  `_is_within(protected, spec.path)` (imported `mounts._is_within`). Called in
  `setup()` **before** `build_base` (no daemon work wasted on refusal).
  `_claude_resolves_to_wrapper(path_env, wrapper_path, *, is_exec, realpath)` —
  pure first-match `$PATH` lookup (injectable fs facts). `_check_claude_on_path(home)`
  — advisory: if `claude` on `$PATH` doesn't canonicalise to the wrapper, **prints**
  a suggested `ln -s <wrapper> <DIR>/claude` (no mandated dir) + flags leftover
  `~/.local/bin/claude-wrapper.py`/`.sh`. Called at the **end** of `setup()`.
- Added `import shutil`. `tests/test_host_install.py`: 13 unit tests.

**Decisions / gotchas:**
- **`bash -lc` is NOT a faithful PATH test** — a login shell re-sources
  `/etc/profile`/`~/.profile`, which re-prepends `~/.local/bin` and masks the
  launcher. The real run path is `incus exec --env PATH=<launcher first> -- claude`
  → `execvpe` against the *provided* env (no shell, no profile). Verify with
  `sh -c 'command -v claude'` + the env, which mirrors `execvpe` exactly. (This
  cost one failed check before I switched to `sh -c`.)
- **Run path inherits the shadow-refuse for free** — editing config drifts the
  stamp → `run()` calls `setup(cfg)` → `_check_no_claude_shadow` raises
  `SetupError` (caught by `cli.run_passthrough`). No run-path code added, so the
  §15.2 ≤3-daemon-call budget is untouched. Both checks live in `setup` only.
- **`~/.local/bin` alone is allowed** (`_is_within(claude_share, ~/.local/bin)` is
  false); only mounts at/above `~/.local/share/claude` / `LAUNCHER_DIR` / `~` are
  refused. Default config (`~/.claude`, `~/.claude.json`) passes.
- **Launcher target is an absolute path** (`~/.local/share/claude/versions/<v>`,
  home-parity), so after a `~/.local/bin` mount it still points at the
  *container's* binary (`~/.local/share/claude` is not mounted — that's exactly
  what the §8 guard protects). If the user ever also mounts
  `~/.local/share/claude`, the guard refuses it.
- **claude-base predates T11** (built in T4, no launcher). A real `setup` rebuild
  would add it; I verified part 1 design-faithfully via a CoW throwaway instead
  (ran the actual `_LAUNCHER_SCRIPT` + `_exec_env` PATH on it), avoiding a full
  network rebuild. **Next real `setup` will bake the launcher into base.**

**Verified:** `pytest -q` → **122 passed** (109 prior + 13 new). Host-side
(real `$HOME`): `setup()` with a config mounting `~/.local/share/claude` **refused**
with a clear message; mounting `~` **refused**; `_check_claude_on_path` on this
host (where `claude` → real binary, not the wrapper) **printed** the suggested
`ln -s` command **and flagged** the leftover `~/.local/bin/claude-wrapper.py`+`.sh`.
Throwaway integration off `claude-base` (**4/4**): with host `~/.local/bin`
mounted over the container's, bare `claude` resolved to the **private launcher**
(`/usr/local/lib/claude-wrapper/bin/claude`); the **contrast** (old `~/.local/bin`-first
PATH) hit the shadowed host shim, proving the prepend is the fix; the launcher
target was the **container's own** binary; bare `claude --version` ran (2.1.150).
Cleaned up — only `claude-base` STOPPED remains. **Project is now feature-complete
(T1–T11 done).** Not exercised here: the §15.1 `@`-username leg (host is
gianz/1000) and an end-to-end run after a real `setup`-with-launcher + a
user-created `claude` shim (mechanism fully verified above).

### 2026-05-24 — T12 added (stale-instance recreation gap; NOT yet implemented)

**Context:** User added `git` to `[setup].packages`, ran `setup` (which rebuilt
base + templates *with* git), then launched the wrapper in `~/Devel/claude-wrapper`
(the `personal` context) — `git` was absent inside. Investigation confirmed the
per-cwd instance `claude-sandbox-personal-4ed1aa79` was created 2026-05-24 11:17,
**before** the base rebuild (11:29) and template rebuild (11:32), and is a CoW of
the *older* (no-git) base. Both `claude-base` and `claude-sandbox-personal` have
git (verified via `incus file pull` of the 4 MB binary — note `… /dev/null` as the
pull target gives false "missing" results on real files; pull to a temp file);
only the tier-3 instance was stale (had `jq` from the earlier base, not `git`).

**Root cause:** `setup` rebuilds tiers 1–2 (delete+recreate) and runs `reap`, but
`reap`/`plan_reap` only stop-idle / delete-by-age|LRU — there is no "instance is
older than its source" notion. `_ensure_instance` (`lifecycle.py:799`) only
CoW-copies when the instance is *missing*; an existing instance is reused with no
staleness check, and instances carry no build-version tag (only `cw-role` /
`cw-context` / `last-used`). So a config edit updates base+templates via the
stamp-drift auto-setup, but the new rootfs **never propagates to already-created
instances**. → T12 fixes this (stamp the source `user.cw-build`, recreate drifted
instances on the warm path).

**Immediate remediation done (host, not code):** deleted all four pre-rebuild
tier-3 instances — `claude-sandbox-{default-74e5c3d8, personal-4ed1aa79,
api-8f46f7c4, api-cba3cd75}` (all created before the 11:29 base rebuild). They
recreate fresh (with git) on next use; `claude-base` + templates
`claude-sandbox-{personal,api}` left intact and STOPPED.

**Open design call for the T12 session:** lazy recreation in `_ensure_instance`
vs. eager deletion in `setup`/`reap`; what to stamp into `user.cw-build` (epoch
vs. content hash); the liveness-guard interaction (don't yank a live session).
May warrant a short design amendment (§4/§9/§10) before coding, à la T11.

**Verified:** none (investigation + task draft only; no code/DESIGN changed).

### 2026-05-24 — T12: Recreate stale instances on source rebuild

**Design calls made this session (the two open ones from the draft):**
- **Lazy recreation** in `_ensure_instance` (not eager delete in `setup`/`reap`).
  Only instances you actually launch are rebuilt; the §15.2 hot-path budget is
  untouched on the common non-drifted case (a single `list_instances` substitutes
  for the per-instance `instance_info` query — still 1 daemon call).
- **`user.cw-build` = content hash of rootfs inputs** (user-chosen via
  AskUserQuestion, over epoch / config-file-hash). `_base_build_id(cfg)` hashes
  global packages + global-provision *content* + global mounts; `_template_build_id(base_id, ctx)`
  folds in base_id (a base rebuild cascades to all templates/instances) + the
  context's mounts + provision content (so editing one context recreates only
  *its* instances). claude's own version is intentionally **not** an input (frozen
  base model, §11) → a no-op `setup` doesn't churn instances.

**Changed (`lifecycle.py`):**
- Added `BUILD_KEY = "user.cw-build"`; `_read_provision`, `_mount_inputs`,
  `_base_build_id`, `_template_build_id`, and the pure `_instance_is_stale`.
- `build_base` takes `build_id=` and stamps `claude-base` after the mounts,
  before stop. `build_templates`/`_build_template` take `base_id` and stamp each
  template with `_template_build_id` (overwriting the id inherited from base via
  the CoW). `setup` computes `base_id = _base_build_id(cfg)` once and threads it
  through both.
- `_ensure_instance` now reads instance + source tags from **one
  `list_instances()`** call (instead of `instance_info(instance)`), and before
  reusing an existing instance compares its inherited `cw-build` to the source's
  *current* one: stale + not-live → delete + fall to the cold CoW path; stale +
  **live `claude` session** (`_has_live_session`, T10 guard) → warn + reuse this
  run (recreates next idle run); current → unchanged warm path.
- `tests/test_lifecycle_build_id.py`: 15 unit tests (the drift decision incl.
  the None-source/pre-T12-instance cases; build-id determinism + sensitivity to
  packages/mounts/mode/provision-content; template base-id cascade + per-context
  isolation).

**Decisions / gotchas for the future:**
- **CRITICAL — why read the source's *stamped* tag, never recompute on the run
  path:** a provision-script *content* edit changes the content hash but does
  **not** drift the config stamp (`_config_stamp` hashes config.toml only), so it
  triggers no auto-setup. If the warm path recomputed the hash locally it would
  flag every run as stale → **infinite recreation**. Reading what `setup`
  actually stamped means the tag changes only when `setup` rebuilds the source.
  Corollary: a provision.sh content change still needs a **manual `setup`** to
  take effect (same as the pre-existing T8 stamp limitation — config.toml is the
  only auto-setup trigger); after that setup, instances recreate and pick it up.
- **§15.2 budget preserved — RE-MEASURED.** Warm, non-drifted, running instance:
  `_ensure_instance` makes **exactly 1 daemon call** (the `list_instances`
  `query`); the run path is then still `query` + `config_set`(last-used) +
  `exec` = 3 total, 2 before claude. `_has_live_session` (an extra `exec`) fires
  **only** on the rare stale-and-running path, never on the hot path.
- **Migration of pre-T12 sources/instances is automatic + safe.** `claude-base` +
  templates built before T12 carry no `cw-build`; `_instance_is_stale(inst, None)`
  returns False (unknown source → never recreate), so nothing churns until the
  next `setup` stamps the sources. A pre-T12 *instance* (no tag) against a
  freshly-stamped source reads as stale → recreates once. (The user already
  deleted their stale instances in the 2026-05-24 remediation, so there are none
  to migrate right now; their next `setup` stamps base + `personal`/`api`.)
- **`list_instances` (recursion=1) payload is heavier than a single-instance
  `query`** but it's still one call and returns the `status` + `config` tags the
  warm path needs (already relied on by T5 prune / T10 reaper). The instance's
  local `devices` (the only thing the old `instance_info` had that the list view
  arguably differs on) are unused by `_ensure_instance` — session/project mounts
  go through `device_show`'s own cached call.
- Project is now **T1–T12 complete.** DESIGN unchanged this session — the build
  identity + recreation slot cleanly into the existing §4/§9/§10 model (no
  amendment needed, unlike T11).

**Verified:** `pytest -q` → **137 passed** (122 prior + 15 new). Throwaway
integration run against the real daemon (a `t12-src` CoW of `claude-base` standing
in for a rebuilt source; **11/11 checks**; the real base/templates never touched,
all t12-* cleaned up): **(A)** a stale instance (`oldbuild`) against a source
mutated + bumped to `newbuild` was **recreated** — the source's new content (a
pushed marker = the stand-in "new package") is present, the old-instance sentinel
is **gone** (proving a fresh CoW, not a mutation), and it's Running; **(B)** a
**current** instance (`newbuild` == source) was **not recreated** (its sentinel
survived) and warm `_ensure_instance` made **exactly 1 daemon call**; **(C)** a
**stale + live-`claude`** instance was **reused** (warned, not recreated, still
Running, kept its old id) — the T10 liveness guard. `incus list` shows only
`claude-base` + `claude-sandbox-{personal,api}`, all STOPPED. **Not exercised
here:** a full real `setup` rebuild + relaunch (the build-id stamping is covered
by unit tests; the recreation mechanism by the integration run) and the
`@`-username leg (host is gianz/1000).

### 2026-05-24 — T13 + T14 added (config-DRY design amendment; NOT yet implemented)

**Context:** User wants to shrink a fast-growing `config.toml` (two new
`~/work` sub-tree contexts sharing the same `.ssh`/`.gnupg`/`.gitconfig`
credential mounts) along two axes — (1) stop repeating the long
`~/.config/claude-wrapper/work-mappings/` prefix in every `from`, and
(2) stop duplicating the three credential mount blocks across contexts.

**Design decisions made this session (with the user) → DESIGN §6 + new §7.1/§7.2:**
- **TOML has no native interpolation/anchors** (spec deliberately omits them), so
  Change 1 is our own loader pre-pass: a `[vars]` table + `${NAME}` substitution
  (brace form only; bare `$NAME` left literal). User chose `${VAR}` (1a) over a
  narrow `from_base` relative-resolution or `expandvars`-on-real-env. → **T13**.
- Change 2 → **mount groups (Design B)**, chosen over abstract-context-`+extends`
  (Design A) and explicitly over "child overrides parent's `when`". Rationale: the
  shared thing is *a set of mounts*, not a context — a group has **no `when`, no
  template, no instance** by construction, so it never competes in the §6
  longest-prefix match (which is exactly the user's stated worry, sidestepped
  rather than patched with override/tie-break rules). → **T14**.
- **Conflict rule: inline-overrides-included, later-wins by container-side `path`**
  (user's call). **Inheritance is mounts-only** (was a Design-A question; moot
  under B, but recorded).
- **Both are parse-time-only sugar that flatten into the existing model.** `[vars]`
  expands into all path strings before `~` expansion; `[mount_groups]`+`include`
  flatten into `Context.mounts`. Neither appears on the runtime `Config` surface,
  and **nothing downstream changes** — template build, build-id (T12),
  scope-keying (§5), masking/guards (§8) all already operate on `Context.mounts`.
  This keeps the §4 3-tier CoW hierarchy untouched (a CLAUDE.md hard constraint).
- **`SCHEMA_VERSION` → 2** (T13): the config *shape* changes, and adopting groups
  changes a template's baked mount set, so the §10 stamp drift forces one
  re-`setup` (rebuild templates → T12 recreates instances on the new rootfs).

**Implementation notes for the T13/T14 sessions:**
- T13 recommended shape: parse `[vars]`, then a **recursive pre-pass over the raw
  TOML dict** substituting `${NAME}` in every string *except* inside `[vars]`
  itself, strip `[vars]`, then run the existing section parsers unchanged (their
  `_expand`/`expanduser` still does the `~` step second). Undefined `${NAME}` →
  `ConfigError` naming the key.
- T14 confines entirely to `parse_config`: build a `name → tuple[MountSpec]` map
  from `[mount_groups.*]` (reuse `_parse_mount`), add optional context `include`
  (`_str_list`), flatten included-then-inline into `Context.mounts` with
  later-wins dedupe by `path`. Refresh `_DEFAULT_CONFIG_TOML` with a worked
  `[vars]`+group+`include` example. Don't store groups on `Config`.
- Ordering: T13 before T14 so the §7.2 group example's `${WM}` already works, but
  they're independent — if T14 lands first, it bumps `SCHEMA_VERSION` instead.

**Verified:** none — design + task draft only. No code changed; `DESIGN.md`
(§6 bullet + §7.1/§7.2) and `TASKS.md` (T13, T14, this log entry) only.

### 2026-05-24 — T13: `${VAR}` config expansion (`config.py`)

**Changed (`config.py` only — host-only pure logic, no sandbox needed):**
- Bumped `SCHEMA_VERSION` 1 → 2 (folds into the §10 stamp → forces one
  re-`setup`; T14 reuses 2, no further bump).
- Added `_VAR_RE = r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}"` (brace form only), plus
  `_parse_vars(raw) -> dict[str,str]` (flat `[vars]` table; non-string value →
  `ConfigError`) and `_substitute_vars(value, variables, where)` (recursive walk
  over str/dict/list; `re.sub` callback raises `ConfigError` on an undefined
  `${NAME}`, naming the var + where it appeared; non-string scalars pass through).
- Wired a **single pre-pass at the top of `parse_config`**, *before* the section
  parsers: parse `[vars]`, rebuild `data` with `${NAME}` substituted into every
  value **except** the `vars` key, and drop `vars` from the dict. The existing
  parsers (and their `_expand`/`expanduser`) are **untouched** — `${VAR}` is
  resolved first, `~` second.
- Added 6 tests to `tests/test_config.py`.

**Decisions / gotchas for T14:**
- **`[vars]` is dropped from `data` in the pre-pass** (dict comprehension skips
  the `vars` key), so it never reaches a section parser and isn't on `Config`.
  **T14's `[mount_groups]` must do the same** — flatten into `Context.mounts` at
  parse time, don't store groups on `Config` (DESIGN §7.2). The §7.2 group
  example's `${WM}` already works because substitution runs over the whole dict
  including `mount_groups` before any parsing.
- **No recursion is automatic, not special-cased:** `re.sub` does a single pass,
  so a `${...}` *inside* a substituted value (e.g. a var defined as `"${B}/x"`)
  is inserted verbatim and never re-scanned — `${B}` survives literally and does
  **not** raise undefined (test `test_vars_value_not_recursively_expanded`). The
  `vars` key is excluded from the walk too, so var *values* are never scanned.
- **Bare `$NAME` is left literal** (brace-only regex) so `$`-paths survive — the
  reason `expandvars` was rejected in the design (test
  `test_bare_dollar_name_left_literal`).
- **`SCHEMA_VERSION` is now 2** — the stamp tests don't hardcode it (they hash
  whatever it is), so the bump is transparent. A real user with an existing
  stamp will auto-`setup` once on next run (intended, per the design note).
- **`_DEFAULT_CONFIG_TOML` intentionally NOT touched** — T14 owns refreshing it
  with the worked `[vars]` + `[mount_groups]` + `include` example.

**Verified:** `python3 -m pytest -q` → **143 passed** (137 prior + 6 new), no
regression. New tests cover all four Done-when cases — (1) `[vars] WM = "~/x"` +
`from = "${WM}/.gnupg"` → `host_path` == `~/x/.gnupg` expanded; (2) undefined
`${NOPE}` → `ConfigError` matching `undefined variable ${NOPE}`; (3) bare
`$HOME`-style string with no braces left untouched; (4) the var-less `SAMPLE`
config parses identically — plus cross-section expansion (packages/when/
provision_script/mounts) and the no-recursion case. End-to-end sanity via
`parse_config` confirmed `SCHEMA_VERSION == 2` and the expansion+error paths.

### 2026-05-24 — T14: Mount groups + context `include` (`config.py`)

**Changed (`config.py` only — host-only, confined to `parse_config` per the
plan; nothing downstream touched):**
- Added `_parse_mount_groups(raw) -> dict[str, tuple[MountSpec, ...]]` — parses
  `[mount_groups.<name>]` tables (each a `mounts` array parsed by the **existing
  `_parse_mount`**, so inline *and* full tables both work). Parse-time-only; not
  stored on `Config`.
- Added `_flatten_context_mounts(included, inline)` — merges included-group
  mounts (in `include` order) **then** the context's own inline mounts into a
  dict keyed by container-side `path`, **later-wins** (so a plain dict assign:
  position = first occurrence, value = winner). Returns the deduped tuple.
- `_parse_context` now takes the `groups` map, reads optional `include`
  (`_str_list`, bare string ok), validates each name (unknown → `ConfigError`
  naming the group **and** the context), and sets `Context.mounts` to the
  flattened result. `parse_config` parses groups once and threads them in.
- Refreshed `_DEFAULT_CONFIG_TOML` with commented `[vars]`, `[mount_groups]` and
  `include` examples mirroring DESIGN §7.1/§7.2.

**Decisions / gotchas:**
- **Build-id sensitivity is automatic, not special-cased** — `_template_build_id`
  (T12) hashes `_mount_inputs(ctx.mounts)` *in order*, and the flattened group
  mounts land in `ctx.mounts`, so changing a group's mounts changes the build-id
  of every context that includes it (→ T12 recreates those instances). Verified
  by a unit test that flips a group mount's `mode` ro↔rw and asserts the ids
  differ. **No code outside `parse_config` changed**, exactly as the design
  intended (template build, scope-keying §5, masking/guards §8 all already
  operate on `Context.mounts`).
- **Dedup order = first-occurrence position, winner value** (plain-dict idiom).
  Deterministic, so build-ids are stable. The DESIGN only fixes the *value*
  (later-wins); position among the group block is first-seen — a unit test pins
  `[a1, a2, b1, own]` order so this can't silently drift.
- **`SCHEMA_VERSION` already 2** from T13 — not bumped again (the design said to
  bump only if T14 landed first; it didn't).
- **`${VAR}` reaches group mounts for free** — the T13 pre-pass walks the whole
  dict including `mount_groups` before any parsing, so `from = "${WM}/.ssh"` in a
  group resolves. Confirmed with the worked example.
- **`_DEFAULT_CONFIG_TOML` examples are valid** — verified by uncommenting the
  §7.1/§7.2 block and parsing it (group included, `${WM}` resolved, exclude kept).
- **Mount groups carry no `when`/template/instance** (DESIGN §7.2) — they never
  reach `build_templates`/resolution; only the flattened `Context.mounts` does.
  Project is now **T1–T14 complete** (the full TASKS.md list).

**Verified:** `python3 -m pytest -q` → **149 passed** (143 prior + 6 new), no
regression. New tests cover all four Done-when cases — (1) two contexts each
`include`ing a 3-mount `from`-aliased creds group both carry those three (one
via list, one via bare-string `include`) plus their own; (2) inline `~/.ssh`
overrides the group's (asserted `mode`==rw + `from_` None, single deduped
entry); (3) unknown `include` → `ConfigError` matching `context 'x': unknown
mount group 'nope'`; (4) `_template_build_id` differs when a group mount's mode
changes — plus include-order (`[a1,a2,b1,own]`) and later-group-overrides-earlier.
The shipped default config still loads cleanly (existing test) and its new
commented example parses when uncommented.

### 2026-05-24 — T15 added (multi-covering-mount scope dedup; NOT yet implemented)

**Context:** User noticed that the real `api` context — `when = ["~/work",
"~/workspace"]` with **both** trees as `[[contexts.mounts]]` — spawns *two*
instances: launching under `~/work` keys on `hash8(~/work)` and under
`~/workspace` keys on `hash8(~/workspace)`. Both CoW from the one
`claude-sandbox-api` template, which bakes in **both** mounts, so the two
instances are byte-identical in blast radius (each already mounts the other's
tree). Wasted disk, zero isolation gained.

**Root cause:** §5's original "scope = broadest covering mount" only dedups
*within* a single covering mount. A context with two **disjoint** covering
mounts forks one instance per mount. The earlier rule's "broadest covering mount
is the true blast radius" claim only held for the single-covering-mount case the
design considered; the actual blast radius of any instance is the **union** of
its context's mounts — i.e. the context.

**Design decision (with the user) → DESIGN §5 + §15.4 amended:**
- **Key the *subsumed* case on the context, not the covering mount.** scope =
  `if cwd covered by any context mount → "ctx:<name>" (constant) ; else project
  root ; else cwd`. All subsumed cwds of a context now share one instance. The
  covering mount becomes the degenerate single-mount case of "context mounts
  contain the cwd".
- **Per-cwd isolation (§15.3) is untouched** — that lives entirely in the
  *non*-subsumed fall-through (project root → cwd, `add_project_mount=True`),
  which the change does not touch. We only ever gave up per-cwd isolation when
  subsumed (old §15.4); this extends that "subsumed cwds share" rule from
  per-mount to per-context. No isolation regression: the merged instances already
  exposed an identical mount set.
- **Confined to `mounts.py`** (`compute_scope` returns the constant in the
  subsumed branch; the token is only ever *hashed* there — `add_project_mount` is
  `False`, so lifecycle never treats it as a path). Nothing downstream changes,
  so it parallels T13/T14's "parse-time-only, model unchanged" shape but for the
  scope layer. §15.4 reworded to assert the disjoint-mount collapse.
- **No `SCHEMA_VERSION` bump** — instance-naming logic only; templates and config
  shape unchanged, so the §10 stamp does not drift and no rebuild is forced.
  Old-named duplicate instances are orphaned by the new name and reaped naturally
  (they hold no unique state); a one-off `claude-wrapper gc` clears them at once.

**Rejected alternative:** keep per-mount instances but strip each one's template
to only its covering mount (real work/workspace isolation). Breaks the §4
one-immutable-template-per-context CoW model (per-instance device sets, no
single-`incus copy` mount inheritance) — far more machinery for isolation the
user didn't ask for.

**Verified:** none — design + task draft only. No code changed; `DESIGN.md`
(§5 rewrite + §15.4) and `TASKS.md` (T15 + this entry) only.

### 2026-05-24 — T15: Context-keyed scope dedup (`mounts.py`)

**Changed (`mounts.py` only — pure logic, confined to the scope layer; nothing
downstream touched, exactly the design intent):**
- Replaced `_broadest_covering_mount(cwd, ctx) -> MountSpec | None` with the
  predicate `_is_subsumed(cwd, ctx) -> bool` (`any(_is_within(cwd, m.path) …)`).
  "Broadest" no longer matters — only *whether* a covering mount exists.
- `compute_scope`: the subsumed branch now returns `(f"ctx:{context.name}",
  False)` instead of `(_norm(cover.path), False)`. The non-subsumed fall-through
  (git project root → cwd, `add_project_mount=True`) is **unchanged** — that is
  the §15.3 per-cwd isolation path, untouched.
- Updated the module + `compute_scope` docstrings to the context-keying rule.

**Decisions / gotchas for the future:**
- **Why a non-path scope token is safe here:** the `ctx:<name>` string is **only
  ever hashed** — it's returned only in the subsumed branch where
  `add_project_mount=False`, so T8 never uses the scope as a project-mount host
  path (it only `scope_hash`es it into the instance-name suffix). The `ctx:`
  prefix keeps it disjoint from the absolute-path scopes of the non-subsumed
  branch (all `/…`), so a subsumed instance can never collide-by-hash with a
  path-scoped one of the same context (the mixed case: a context whose `when`
  covers a dir its mounts don't, e.g. a parity mount + an extra `when` prefix).
- **No `SCHEMA_VERSION` bump** (per design): this is instance-naming logic, not a
  config-shape or template change, so the §10 stamp does **not** drift and no
  rebuild/auto-setup is forced. `mounts.py` doesn't even reference the schema.
- **Orphaned old-named instances:** any existing `…-<hash(~/work)>` /
  `…-<hash(~/workspace)>` duplicates from before this change are simply not
  matched by the new `…-<hash(ctx:<name>)>` name. They hold no unique state
  (files live on host bind-mounts), so the reaper/`gc` clears them on age/LRU; a
  one-off **`claude-wrapper gc`** removes them immediately if the user wants the
  disk back now. The real `api` context (the motivating case: `~/work` +
  `~/workspace`) will, on next launch under either tree, key on `ctx:api` and use
  a single shared instance.
- **No integration run needed** (and none done): the change is pure naming logic
  with no daemon interaction; the instance-creation path (T8) that consumes the
  scope is unchanged and already verified. Done-when is unit-test-only.

**Verified:** `python3 -m pytest -q` → **150 passed** (149 prior − 2 replaced
covering-mount tests + 3 new). The four Done-when cases: (1)
`test_disjoint_covering_mounts_share_one_instance` — disjoint `~/work` +
`~/workspace`, cwds `A`/`B`/`C` all → `ctx:api`, same hash, `add_project_mount`
False; (2) `test_nested_covering_mounts_collapse_to_one_instance` — nested
`~/work` + `~/work/foo` still one instance; (3)
`test_per_cwd_isolation_distinct_instances` (kept) — ssh-only non-covering
context still yields distinct project-root scopes/hashes with `add_project_mount`
True per project (§15.3 preserved); (4)
`test_subsumed_scope_token_is_context_constant_not_template_name` — token
constant per context, `ctx:`-prefixed (not a path), and `!= _template_name(ctx)`,
with a hash disjoint from a real-path scope. **Project is now T1–T15 complete
(the full TASKS.md list).**

### 2026-05-24 — T16: User-declared env (`config.py` + `lifecycle._exec_env`)

**Changed (`config.py` — parse/validate/store, no rootfs touched):**
- Added `env: Mapping[str, str]` + `forward: tuple[str, ...]` to both `Config`
  (global) and `Context` (per-context), defaulting empty (`field(default_factory
  =dict)` / `()`), so every existing `Config(...)`/`Context(...)` construction is
  unchanged. Imported `collections.abc.Mapping`.
- Added `_parse_env(raw, where) -> (literals, forward)` + the `_check_env_name`
  helper. The reserved lowercase key `forward` → `_str_list` (host var names);
  every other pair is a literal `KEY = "value"`. Validates: env-name shape
  (`_ENV_NAME_RE`), string values, and rejects `HOME`/`USER`/`PATH` (`_RESERVED_ENV`)
  in **both** literals and `forward`. Wired into `parse_config` (`data["env"]`,
  `where="[env]"`) and `_parse_context` (`raw["env"]`).
- Refreshed `_DEFAULT_CONFIG_TOML` with a commented `[env]` block (one literal +
  one `forward`) + a per-context `env = { … }` line on the example context.

**Changed (`lifecycle.py` — merge/apply at exec, run-path-only):**
- `_exec_env` signature `(host_user, home)` → `(cfg, context, host_user, home)`
  (called once in `run` at the `exec claude`, which already holds `cfg`/`res`).
  Merge is broadest→narrowest, later-wins: (1) identity `HOME/USER/PATH`;
  (2) built-in forwarded baseline (`_FORWARD_ENV`/prefixes, **setdefault** so it
  never clobbers identity); (3) user `forward` = global ∪ context, pulled from
  `os.environ`, **skipped if unset**; (4) user literals — global, then context
  overrides global (literals beat forwarded); (5) identity re-asserted last.
- Call site now passes `_exec_env(cfg, res.context, host_user, home)`.

**Decisions / gotchas:**
- **`forward` accepts a bare string** (coerced via `_str_list`, like
  `when`/`include`/`packages`); "must be a list of strings" is read as
  *elements* must be strings (a non-string element or a non-list/non-string
  value → `ConfigError`). Consistent with the rest of the loader. User informed,
  no objection.
- **Build-id untouched, by construction** — `_base_build_id`/`_template_build_id`
  never reference `cfg.env`/`ctx.env`, so an env-only edit leaves both ids equal
  (unit-asserted in `test_exec_env.py`): no instance recreation. **NB for T17:**
  the *config stamp* (`_config_stamp`) is still the raw-byte hash, so right now an
  `[env]` edit DOES drift the stamp → one auto-`setup` (harmless, no rebuild
  churn since base/templates are byte-identical). T17 re-keys the stamp to the
  build-id and removes even that auto-`setup` (DESIGN §7.3/§10/§15.13). Until T17
  lands, §15.12's "an env-only edit does **not** change the §4 build-id" holds
  (verified) but the stamp-skip half is explicitly deferred to §15.13/T17.
- **No `SCHEMA_VERSION` bump** (still 2): env is run-path-only, the config
  *shape* parsers tolerate the new optional tables, and env is not in the rootfs.
- **A `dict` field on a frozen dataclass** makes `Config`/`Context` effectively
  unhashable — confirmed nothing hashes them (only `scope_hash`/`_config_stamp`,
  unrelated). Stored as a plain dict via `default_factory` per the design's
  `Mapping[str,str]`; immutability is by convention (frozen binding), matching
  how the rest of the model treats its tuples.
- **`${VAR}` reaches env literals for free** — the T13 pre-pass walks the whole
  dict (incl. `env`) before parsing; `_parse_env` does **not** `_expand`, so env
  values are never `~`-expanded (env ≠ path). Verified: `${WM}/bin` with
  `WM="~/proj"` → `"~/proj/bin"` (tilde survives).

**Verified:** `python3 -m pytest -q` → **173 passed** (150 prior + 23 new: 11
config-parse in `test_config.py`, 12 in new `test_exec_env.py`). Config tests
cover literal+forward parse (global, inline-table + `[contexts.env]` sub-table),
name/value/forward-shape/-element validation, reserved-key rejection (global +
context), `${VAR}` expansion with no `~` expansion, and empty defaults.
`test_exec_env.py` covers identity always-set/re-asserted, both mechanisms,
literal-beats-forwarded, context-beats-global, full precedence, global∪context
forward union, unset-forward skip, and build-id insensitivity (base + template).
**Real `exec` (host throwaway `printenv` off `claude-base`, 9/9):** global
literal, forwarded host var, literal>forwarded (`FOO`), context>global
(`DEPLOY`), context forward, built-in `TERM`, unset-forward skipped, `HOME`/`USER`
preserved — all reached the sandbox at exec time. Throwaway deleted; `claude-base`
untouched. **Project is now T1–T16 complete.** Not exercised: a full interactive
TUI (printenv proves env passing; T8 already proved claude launches) and the
`@`-username leg (host is gianz/1000).

### 2026-05-24 — T17: Build-relevant config stamp (`lifecycle._config_stamp`)

**Changed (`lifecycle.py` only — pure local logic, no daemon/rootfs touched):**
- Re-keyed `_config_stamp` from `hash(SCHEMA_VERSION + config.toml bytes)` to a
  hash of the config's **build identity**: `_base_build_id(cfg)` plus each
  context's `_template_build_id(base_id, ctx)`, sorted for stability, hashed
  together (`md5` of a `sort_keys` JSON `{base, templates}`). **Signature change**
  `_config_stamp(config_path)` → `_config_stamp(cfg: Config)`. `SCHEMA_VERSION`
  stays covered (folded in by `_base_build_id`) so the separate prepend is gone.
- Updated both callers (each already held `cfg`): `setup` (`:745`) writes
  `_config_stamp(cfg)`; `run`'s drift gate (`:1011`) compares against
  `_config_stamp(cfg)`. `config_path` is still used by `ensure_user_config`/
  `load_config` in both — not orphaned.
- Rewrote `tests/test_lifecycle_stamp.py` for the new `Config`-based signature
  (the old tests passed `Path`s): 13 tests (was 5).

**Decisions / gotchas:**
- **This is now the single source of truth** for both the auto-`setup` trigger
  and T12's instance-recreation decision — they call the *same* build-id
  functions, so they can no longer disagree (a config edit that recreates
  instances also auto-`setup`s, and vice versa). Noted in the docstring.
- **Two payoffs, one change:** (a) runtime-only edits — `[env]` (T16),
  `[reaper]` thresholds — are absent from every build-id, so they no longer
  drift the stamp / force a rebuild (this *completes* §15.12's deferred
  stamp-skip half that T16 flagged); (b) a provision-script *content* edit with
  `config.toml` byte-identical now drifts the stamp (the build-ids read the
  script via `_read_provision`), where before it was inert until a manual
  `setup`. Context add/remove/rename still drifts (the set of template ids
  changes). `[vars]`/`[mount_groups]` are already flattened into mounts/paths
  before the build-id sees them, so a `${VAR}` in a *mount* still drifts
  (correct) while one used only in an `[env]` literal does not.
- **§15.2 budget preserved by construction (no integration run needed):** the
  stamp check runs before any daemon interaction in `run`; the new
  `_config_stamp` does only **local file reads** (the provision scripts) +
  hashing — **zero daemon calls**, same as the old byte-read. The warm path is
  still `list_instances` (T12) + `config_set`(last-used) + `exec` = 3 calls, 2
  before claude. Like T15, this is pure logic with no daemon-side change, so
  Done-when is unit-test-only.
- **One-time migration:** on-disk stamps written by the old byte-hash scheme
  mismatch the new build-id hash once → **one harmless auto-`setup`** on the
  first run after this upgrade (base/templates are content-identical if nothing
  build-relevant changed, so T12 won't churn instances either — only the stamp
  is rewritten), then stable. No `SCHEMA_VERSION` bump (local-stamp logic;
  template/config shape unchanged).
- **No DESIGN change** — §10's stamp bullet, §7.3's env caveat, and §15.13 were
  already amended in the T17 design commit (`54e6215`); this session only
  implements them.

**Verified:** `python3 -m pytest -q` → **181 passed** (173 prior − 5 replaced
stamp tests + 13 new). New tests cover the §15.13 cases: **stable** across an
`[env]`/`[reaper]`/context-`env` edit (equal stamp); **drifts** on a package, a
global-mount field (`mode`), context add/remove, a context-mount change; and
**drifts on provision-content change** (global + per-context, `config.toml` shape
identical, just rewriting the script's bytes) — plus schema-bump coverage and the
read/write + drift-cycle round-trips (incl. a runtime-only `[env]` edit staying a
match). Clean import; no daemon interaction so no throwaway run (mirrors T15).
**Project is now T1–T17 complete (the full TASKS.md list).**

### 2026-05-24 — T18: HOME-relative paths in `[env]` values (option (b))

**Design call (user-approved):** option **(b)** — predefine implicit `${HOME}` /
`${USER}` in the §7.1 `${VAR}` pre-pass — over (a) doc-only and (c) `~`-in-env.
Keeps the "env ≠ path / no `~`" stance intact (it rides the existing brace
mechanism), works on both sides of every mount (host HOME/USER == container's,
§3), and is purely additive sugar.

**Changed:**
- `config.py`: added `_implicit_vars()` (`HOME = os.path.expanduser("~")`,
  `USER = os.environ.get("USER") or basename(home)`); seeded it in `parse_config`
  as `variables = {**_implicit_vars(), **_parse_vars(...)}` so an explicit
  `[vars]` entry of the same name still wins. Updated module docstring, the
  pre-pass comment, and `_DEFAULT_CONFIG_TOML` (the `[vars]` note + a
  `GIT_CONFIG_GLOBAL = "${HOME}/.config/git/config"` example in `[env]`).
- `DESIGN.md`: §7.1 gained an implicit-`${HOME}`/`${USER}` bullet; §7.3's
  "`~` not expanded" bullet now points at `${HOME}` for home-relative values.
- `tests/test_config.py`: +4 tests (home var in an `[env]` value; `${USER}`;
  `[vars]` override of implicit `HOME`; undefined `${NOPE}` still raises even with
  the seeds present).

**Decisions / gotchas:**
- **No `SCHEMA_VERSION` bump, no build-id impact** (as the task predicted): a
  config that doesn't reference `${HOME}`/`${USER}` parses byte-identically
  (`test_varless_config_parses_identically` still green), and a config that *does*
  use them in a mount path resolves to the same string the build-id already
  hashes — just like any `${VAR}`. Env values are runtime-only regardless.
- **Bare `$HOME` is still literal** — only the brace form `${HOME}` resolves
  (`test_bare_dollar_name_left_literal` unchanged). This is the documented
  distinction; don't "fix" bare `$` to expand.
- **`parse_config` now reads `os.environ`/`~`** for the seeds. It already did via
  `_expand` (`~` expansion), so this isn't new impurity; tests that don't
  reference the vars are unaffected (the seed dict is built but unused).
- **Reserved-key check unaffected:** `HOME`/`USER`/`PATH` are still rejected as
  `[env]` *keys* (`_check_env_name`); `${HOME}` is a *value* substitution — no
  overlap, no contradiction.
- **The original `.gitconfig` trigger** (the reason this task exists): a
  single-file `~/.gitconfig` bind mount can't be `rename()`d over (EBUSY), so
  `git config` needs a *directory* mount + `GIT_CONFIG_GLOBAL` pointing inside it.
  Users now express that portably as `${HOME}/.config/git/config` in `[env]`.
  The mount/`GIT_CONFIG_GLOBAL` setup itself is the user's `config.toml` concern,
  not package code.

**Verified:** `pytest -q` → **185 passed** (was 181; +4). Real parse from the
repo root confirmed `GIT_CONFIG_GLOBAL = "${HOME}/.config/git/config"` →
`/home/gianz/.config/git/config`. No daemon interaction, so no throwaway run.
**Project is now T1–T18 complete.**

### 2026-05-24 — T19: Surface deployment-specific forwarded env into config

**Design call (already recorded 2026-05-24 with the repo scrub):** the hardcoded
`_FORWARD_ENV` baked deployment-specific forwards into a package meant to be
generic. Trim it to the *universal* baseline; relocate the deployment knobs to
the shipped example config's global `[env].forward` (T16 already provides the
mechanism). Behavioral consequence is intentional: the shipped config is
**documentation, not an auto-loaded default**, so the relocated vars are no
longer forwarded on *any* machine until its real `config.toml` names them.

**Changed:**
- `lifecycle.py`: trimmed `_FORWARD_ENV` to the universal set —
  terminal/locale (`TERM`, `COLORTERM`, `LANG`, `LANGUAGE`, `LC_*`) + IDE hints
  (`TERM_PROGRAM`, `FORCE_CODE_TERMINAL`). `_FORWARD_PREFIXES`
  (`ANTHROPIC_`/`CLAUDE_`) unchanged (a prefix can't be a `forward` name, so it
  must stay in code). **Removed** (now config-only): `HTTP_PROXY`/`HTTPS_PROXY`/
  `NO_PROXY` (+ lowercase), `NODE_EXTRA_CA_CERTS`, `CLOUD_ML_REGION`,
  `GOOGLE_APPLICATION_CREDENTIALS`. Comment rewritten to explain the split.
- `config.py` `_DEFAULT_CONFIG_TOML`: the commented `[env]` example now carries a
  multi-line `forward = [...]` listing the relocated proxy/cloud/cert vars, with
  a note that deployment knobs aren't baked in (Bedrock host adds `AWS_*` by name).
- `DESIGN.md` §7.3: added a "Hardcoded baseline is universal only" bullet naming
  the relocated vars + the documentation-not-default consequence. §7.3's opening
  already described the narrow baseline (terminal/locale, IDE hints, prefixes), so
  it was already correct. **§12 unchanged** — its table never echoed the forward
  var list (only the `~/.local/bin` PATH/launcher rows + the §7.3 prefix cross-ref).
- `README.md`: added an `[env]` block to the config example and a `[env]` bullet
  to "Key config concepts" stating only the universal baseline is hardcoded and
  deployment knobs must be listed in `[env].forward`.
- `tests/test_exec_env.py`: kept the `TERM` baseline test (annotated that the
  baseline must stay narrow); +2 tests — a relocated var (`HTTPS_PROXY` +
  `CLOUD_ML_REGION` + `GOOGLE_APPLICATION_CREDENTIALS`) set on the host is **not**
  forwarded with a default `Config`, but **is** when named in `[env].forward`.

**Decisions / gotchas:**
- **No `SCHEMA_VERSION`/build-id impact** — env is run-path-only (T16/T17). The
  pre-existing `test_base_build_id_ignores_env`/`test_template_build_id_ignores_env`
  still pin this; nothing in this task touched a build-id or the schema.
- **Unit-test-only verification** (like T15/T17): this is a forward-list refactor
  + docs with no daemon-side change. T16 already verified real env passing via a
  `printenv` exec; removing names from the always-forward list is fully covered by
  the not-forwarded-by-default / forwarded-when-named unit pair. No throwaway run.
- **Migration note for existing machines:** anyone relying on the old implicit
  proxy/cloud forwarding must now add those names to their real `config.toml`'s
  `[env].forward` — they stop being forwarded silently after this upgrade. This is
  the intended generic-package behavior, but it's a behavior change for live hosts.

**Verified:** `python3 -m pytest -q` → **187 passed** (was 185; +2). Shipped
default config re-parses cleanly (the `[env]` example stays commented →
`forward=()`, `env={}`). No residual relocated-var references in `lifecycle.py`.
**Project is now T1–T19 complete — the full TASKS.md list.**

### 2026-05-25 — T20: Preflight incus host-readiness in `setup`

**Design (recorded first, DESIGN §3 + §15.14):** §3 gained two new prerequisite
bullets beyond the existing single-entry idmap check — a **base root sub-id
*range*** (incus needs ~65536 ids for an unprivileged container; a lone
`root:<uid>:1` idmap entry passes the entry check but fails this) and an
**initialised incus** (`incus admin init` → a storage pool + a device-less
`default` profile). Both follow the detect→instruct→never-mutate `_check_subuid`
idiom. Added §15.14 acceptance row.

**Changed (`lifecycle.py`):**
- `_subid_range_present(path, *, min_count=65536) -> bool` — pure predicate
  alongside `_subid_covered`; True iff some `root:start:count` line has
  `count >= min_count`. Distinct from `_subid_covered` (count-1 idmap prereq).
  Mirrors its owner filter (`root`/`0`) and OSError→False.
- `_check_subid_range()` (a) — raises `SetupError` with
  `echo 'root:1000000:1000000000' | sudo tee -a /etc/subuid /etc/subgid` +
  restart if the range is absent on **either** file.
- `_check_incus_initialised()` (b) — raises `SetupError` printing
  `incus admin init --minimal` if there's no storage pool **or** a device-less
  `default` profile (one daemon call each).
- `_check_incus_ready()` — wraps (a)+(b); called in `build_base` **right after
  `_check_subuid(host_uid, host_gid)` and before `incus.launch`** (line ~558).
  Constants `_SUBID_BASE_RANGE` / `_CONTAINER_ID_SPAN=65536`.

**Changed (`incus.py`):** added two zero-dep `incus query` probes —
`storage_pools() -> list` (`/1.0/storage-pools`, `[]` on none/error) and
`profile(name) -> dict|None` (`/1.0/profiles/<name>`). A missing `incus` binary
still raises `IncusError` from `_run` (propagates to the CLI), a daemon error →
empty/None (so the readiness check fails closed with our message, not a crash).

**Decisions / gotchas:**
- **Range `1000000:1000000000` coexists with the idmap entry** — disjoint from a
  normal uid (1000, below start) and a large LDAP uid (e.g. 1529911346, above
  the `1_001_000_000` end), so both `/etc/subuid` lines are independently needed
  and the idmap still resolves. Documented in §3 + the `_SUBID_BASE_RANGE`
  comment.
- **Two separate predicates, two separate messages** (per the task) so the user
  never conflates the count-1 idmap entry with the base range. `_check_subuid`
  is untouched; `_check_incus_ready` is a sibling, not an extension.
- **Daemon cost is setup-only.** `build_base` is called **only** from `setup()`
  (confirmed: the warm run path never calls it), so the two new probe calls add
  **zero** calls to the §15.2 hot path. `setup` is not the hot path, so a daemon
  call there is fine (same licence as the T11 host checks).
- **(b) fails closed:** if `storage_pools()`/`profile()` hit a daemon error they
  return `[]`/`None`, so `_check_incus_initialised` raises our clear message
  rather than letting an opaque incus error through later. A missing binary is
  the one exception — it raises `IncusError` (also caught by the CLI).
- **No `SCHEMA_VERSION`/build-id impact** — host preflight, touches no rootfs or
  config shape (as the task predicted). `_config_stamp`/build-ids unchanged.

**Verified:** `python3 -m pytest -q` → **195 passed** (was 187; +8 new in
`tests/test_subid_range.py`): single idmap entry → False, base range → True, both
lines → True, missing file → False, the count boundary (65535 → False,
65536 → True), non-root owner ignored (the stock `ubuntu:100000:65536` case),
`0` owner alias accepted, malformed lines skipped. **Daemon-side (b) probe not
run here:** this session executes *inside* a sandbox container
(`claude-sandbox-default-30edbc7d`, no `incus` on PATH), so the live preflight
must be validated on the host — unit-test-only verification, like T15/T17/T19.
Demonstrated the rendered messages directly: `_check_subid_range()` fires on this
container's real `/etc/subuid` (`ubuntu:100000:65536`, no root range → the exact
barney symptom), `_check_incus_initialised()` renders correctly for a mocked
empty-pool/device-less-profile incus and passes silently for a healthy one.
**Project is now T1–T20 complete — the full TASKS.md list.**

### 2026-05-25 — Post-T20: claude-shadow cwd guard (`mounts.check_cwd_allowed`)

Ad-hoc fix (not a numbered task; user-requested, implemented in one context).
**Gap:** the setup-time claude-shadow guard (`lifecycle._check_no_claude_shadow`)
only inspects *configured* `[[mounts]]`. The **per-cwd project mount** is
home-parity, so launching from a cwd at/above the in-container claude tree would
shadow it identically — and `check_cwd_allowed` never covered `~/.local*`. This
is the runtime twin of that guard. Surfaced from the user's recollection that we
"wanted to prevent running under `~/.local`"; the design as written never did.
- **Rule (DESIGN §8 cwd-boundary, amended):** deny `~/.local` and `~/.local/share`
  **exact**, `~/.local/share/claude` **at/under**. Only mounting the two parents
  *whole* shadows claude, so their other children stay legal cwds — `~/.local/bin`
  (launcher PATH owns it), `~/.local/state`, `~/.local/share/<other>`. Inside the
  install dir is denied too (a parity mount there masks the version files — the
  user's explicit choice over pure exact-match).
- **Why exact for the parents:** at/under would needlessly forbid `~/.local/bin`
  etc.; the shadow only happens when the *whole* parent is the mount root.
- `LAUNCHER_DIR` (`/usr/local/lib/claude-wrapper/bin`) needs no entry — already
  under the `/usr` system-root denial.
- **Done when:** new `test_mounts.py` cases — 3 deny groups (incl. the
  `claude/versions/<v>` inner case) + an allow group covering `~/.local/bin[/sub]`,
  `~/.local/state/x`, `~/.local/share/other`, and the `~/.localx` non-prefix.

**Verified:** `python3 -m pytest -q` → **206 passed** (was 195; +11 new). No
rootfs/config-shape change → no `SCHEMA_VERSION`/build-id impact; pure host-side
launch guard.
