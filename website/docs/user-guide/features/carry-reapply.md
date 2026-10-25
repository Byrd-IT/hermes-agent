# Carry reapply

`hermes update` already parks a diverged checkout on
`refs/hermes-update-backups/` and resets that same branch onto the update
target. A conflicted autostash is kept and the working tree is cleaned. Neither
step replays the local work that still is not upstream.

`hermes_cli/update_carry.py` runs after those existing steps. It does not reset,
does not drop a stash, and does not push.

| `updates.carry_reapply` | What the update hook does |
| --- | --- |
| unset or `dry-run` | Print the plan. Change nothing further. |
| `apply` | Cherry-pick clean commits onto the tree the updater already reset. Leave conflicts out. |
| `off` | Skip the plan. |

A standalone run defaults to dry-run:

```bash
python -m hermes_cli.update_carry --repo /path/to/checkout --upstream origin/main
python -m hermes_cli.update_carry --apply --worktree /tmp/carry-out --branch carry-reapply
```

`--apply` without `--worktree` refuses to write the source checkout. `--in-place`
is the explicit opt-in the update hook uses after the updater's own reset.
Every carried commit must still exist afterwards, classified as dropped (already
upstream), reapplied, or left out because it conflicted.
