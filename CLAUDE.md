# claude-wrapper

A wrapper that runs the `claude` CLI inside an isolated incus sandbox, passing
unrecognized args through to `claude` and adding management subcommands. This is
a **ground-up rewrite** of the old single-file `~/.local/bin/claude-wrapper.py`.

## Source of truth

**`DESIGN.md`** is authoritative — read it before implementing or changing
anything. Its **§15 acceptance matrix is the definition of done**; treat each
item as a verifiable success criterion to loop against.

## Workflow — one task per context (IMPORTANT)

Implementation is executed **one task at a time, each in a clean context**:

1. Open **`TASKS.md`** and find the first unchecked `[ ]` task.
2. Do **only that one task**. Verify it against its **Done when** criteria.
3. Check the box, append a dated entry to the `TASKS.md` **Progress log**
   (decisions/gotchas the next task needs), and `git commit`.
4. **STOP. Do not start the next task.** Tell the user the task is complete and
   to `/clear`. Each task begins fresh with no prior conversation context, so
   leave everything the next session needs in `TASKS.md` + the commit.

If a task reveals a missing dependency or design gap, stop and surface it rather
than guessing or pulling work forward from later tasks.

## Build & test

- Implement as the **`claude_wrapper` pipx package** (module layout in DESIGN §13).
- **Build and test inside the sandbox**, working through the §15 acceptance matrix.
- Ask before any irreversible step (deleting containers, editing system files).

## Hard constraints (don't violate without re-opening the design)

- **incus only** — LXD/`lxc` support is intentionally dropped.
- **3-tier CoW hierarchy** (DESIGN §4): `claude-base` and the per-context
  templates `claude-sandbox-<ctx>` are **immutable and never started** — only
  `setup` (re)builds them; only per-cwd instances are run.
- **Identity** (DESIGN §3): mirror `$USER` exactly (incl. `@`) via direct
  `/etc/passwd` edit, home = real `$HOME`, UID 1000 + idmap, no symlink.
- **Clean slate:** the legacy `claude-sandbox` container is already deleted; the
  package contains **zero legacy-migration code**.

## Conventions

- Sandbox apt packages go in **`config.toml` `[setup].packages`** (and
  per-context `provision_script`s) — *not* in a script tuple. This supersedes
  the global CLAUDE.md note about a `PACKAGES` tuple, which referred to the old
  single-file script.
- Per-machine config (paths, contexts, the `@`-username work laptop) lives in
  `~/.config/claude-wrapper/config.toml`; the package stays generic.
