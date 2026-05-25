#!/usr/bin/env bash
# List the claude-wrapper containers and flag likely-stale per-cwd instances.
#
# Run on the HOST (where `incus` lives), read-only — it never stops or deletes
# anything. Shows, in one table per tier:
#   * base + templates  : the immutable CoW sources (always Stopped by design).
#   * instances (tier 3) : name, state, human last-used, idle age, and the
#     PROJECT MOUNT — the host cwd the instance was created for (device
#     `mnt-<hash>`, the same hash as the instance-name suffix; lifecycle.py:397).
#
# A NOTE column flags instances worth a closer look:
#   ⚠ source missing  — the project-mount cwd no longer exists (orphan).
#   ⚠ shadow cwd      — cwd is ~/.local[/share[/claude]]; the §8 guard now
#                        refuses these, so it predates the guard / a stale binary.
#
# `gc` reaps by AGE only (stop idle >stop_idle_after, delete unused
# >delete_unused_after, default 30m/14d), so freshly-bad instances won't go
# until they age out — use this to spot them, then: incus delete --force <name>

set -euo pipefail

need() { command -v "$1" >/dev/null 2>&1 || { echo "list_sandboxes: needs '$1'${2:+ ($2)}" >&2; exit 1; }; }
need incus
need jq "apt-get install -y jq"

# One aligned-table formatter; degrade to raw TSV if `column` is absent
# (Ubuntu: `apt-get install -y bsdextrautils`).
if command -v column >/dev/null 2>&1; then
  fmt() { column -t -s $'\t'; }
else
  fmt() { cat; echo "(install bsdextrautils for aligned columns)" >&2; }
fi

home="${HOME:?}"
json="$(incus list --format json)"

echo "=== base + templates (immutable CoW sources; expected Stopped) ==="
{
  printf 'NAME\tSTATE\tROLE\n'
  jq -r '.[]
    | select(.name=="claude-base" or (.config["user.cw-role"]=="template"))
    | [.name, .status, (.config["user.cw-role"] // "base")] | @tsv' <<<"$json" \
    | sort
} | fmt

echo
echo "=== per-cwd instances (oldest-used first) ==="

# jq emits one TSV row per instance (no header); bash adds the NOTE column so it
# can stat the source path and compare against the shadow zone on the host.
rows="$(jq -r '
  def human(s): if s<0 then "?" elif s<60 then "\(s|floor)s"
    elif s<3600 then "\((s/60)|floor)m" elif s<86400 then "\((s/3600)|floor)h"
    else "\((s/86400)|floor)d" end;
  [ .[] | select(.config["user.cw-role"]=="instance") ]
  | sort_by(.config["user.last-used"] // "0" | tonumber)
  | .[]
  | (.name|split("-")|last) as $h
  | (.config["user.last-used"] // "0" | tonumber) as $lu
  | ((.devices // {})["mnt-\($h)"].source
     // (.expanded_devices // {})["mnt-\($h)"].source
     // "(none — context-shared)") as $src
  | [ .name, .status,
      (if $lu>0 then ($lu|localtime|strftime("%Y-%m-%d %H:%M")) else "never" end),
      (if $lu>0 then human((now-$lu)) else "—" end),
      $src ] | @tsv
' <<<"$json")"

if [ -z "$rows" ]; then
  echo "(no per-cwd instances)"
else
  {
    printf 'NAME\tSTATE\tLAST USED\tIDLE\tPROJECT MOUNT (cwd)\tNOTE\n'
    while IFS=$'\t' read -r name state lu idle src; do
      note=""
      case "$src" in
        "(none"*) ;;  # context-shared: no project mount to check
        "$home/.local"|"$home/.local/share"|"$home/.local/share/claude") note="⚠ shadow cwd" ;;
        *) [ -d "$src" ] || note="⚠ source missing" ;;
      esac
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$name" "$state" "$lu" "$idle" "$src" "$note"
    done <<<"$rows"
  } | fmt
  echo
  echo "Remove one with:  incus delete --force <NAME>"
fi
