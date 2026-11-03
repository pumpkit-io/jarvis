# The Orchestrator's shell runs only through a Hub check

ADR 0002 made the Orchestrator read-only, but it needs `gh` and `git` to look things up, and a Claude Code allow rule such as `Bash(gh *)` would also allow `gh pr merge`. Prefix rules cannot tell `gh api repos/x` from `gh api -X DELETE repos/x` either. So the Orchestrator runs under `dontAsk` with Read, Grep and Glob on the allowlist and Bash left off it, and a `PreToolUse` hook on Bash allows a command only when a Hub function, `read_only_refusal`, finds one plain `gh` or `git` read on its list. Everything else is denied, including chaining, pipes, redirection, substitution and `gh api`.

## Considered Options

- Allow rules per read-only subcommand (`Bash(gh pr list:*)` and so on). Rejected: a prefix cannot see a flag that turns a read into a write, and `git -C <repo> log` matches no fixed prefix.
- `can_use_tool`. Rejected: under `dontAsk` it is never called, and the other modes would prompt.

## Consequences

- The check fails closed. Without the hook, or if it fails (`on_failure="block"`), every Bash call is denied.
- A new look-up the Orchestrator needs, such as a `gh` subcommand, means adding it to the list in `src/jarvis_hub/orchestrator.py` with a table test.
