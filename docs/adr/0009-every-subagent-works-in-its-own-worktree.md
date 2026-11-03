# Every Subagent works in a worktree of its own

A Subagent runs with permissions skipped (ADR 0001), so whatever folder it starts in, it can change. A Subagent that only reads used to start in the User's own clone of the repo, where the User may have a branch checked out and uncommitted work. A mistaken `git checkout`, a formatter run or a stray edit there would land on the User's work.

So every Subagent gets a worktree the Hub makes for it, locked under its id with the sdlc `worktree.sh lock`. A writing Subagent gets one on a fresh `feat/`, `fix/` or `chore/` branch from `worktree.sh create`, kept when it ends. A reading Subagent gets one with no branch, detached at `origin/<default>`, in the repo's `.claude/worktrees/read-<title>`, and the Hub removes it when the Subagent ends. Removal uses `git worktree remove` without `--force`, so a read-only worktree the Subagent changed after all is kept, and its report says so.

## Considered Options

- Keep reading Subagents in the User's clone and take away the write tools (Edit, Write, NotebookEdit). Rejected: Bash stays, and a Subagent needs Bash to run `git log`, `gh` and the tests, so it can still write.
- Read-only Subagents under the Hub's own runtime folder. Rejected: Claude Code reads every `CLAUDE.md` from the working folder upward, so a Subagent there would load Jarvis's own instructions instead of only the target repo's.

## Consequences

- Starting a reading Subagent costs a `git fetch` and a `git worktree add`, a second or two.
- A repo whose `.gitignore` misses `.claude/worktrees/` shows the folder as untracked in the User's clone while a reading Subagent runs. Writing worktrees already add that entry through the sdlc bootstrap.
- `worktree.sh list` shows a reading Subagent's worktree as `(detached)` with its lock, and `sweep` leaves it alone while the lock is held.
