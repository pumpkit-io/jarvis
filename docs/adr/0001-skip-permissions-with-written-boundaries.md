# Skip permissions and rely on written boundaries

Jarvis runs with `--dangerously-skip-permissions` so the User can talk to it by voice without approving tool prompts on screen, and so it can carry long tasks through unattended. The only guardrail is therefore the boundary written in AGENTS.md: Jarvis and its Subagents may go as far as pushing a feature branch and opening a PR, and anything irreversible or public (merging, releasing, deploying, posting, deleting, spending money, changing production or analytics config) waits for the User's spoken yes.

## Considered Options

- Keep permission prompts on. Rejected: a voice session stalls on every prompt the User has to read and click.
- Allowlist commands in `.claude/settings.json`. Rejected for now: Subagents work across several Org repos with different toolchains, and a list broad enough to cover them would protect little more than the written boundary does.

## Consequences

A wrong or ambiguous sentence in the AGENTS.md boundary is a real risk, not a style problem. Changes to that section deserve the same care as changes to production config.
