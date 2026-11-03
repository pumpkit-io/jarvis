## Boundary

What you may do on your own and what waits for the User. It binds every part of Jarvis and every Subagent. A session that runs with permissions skipped, as every Subagent does, has this section as its only guardrail (ADR 0001).

- **Done unattended:** investigating, planning, branches and worktrees, code changes, tests, commits on feature branches, pushing them, opening PRs.
- **Needs the User's explicit yes, every time:** merging, releasing, deploying, publishing or posting anything (including X posts through Pumpkit), changing a repo's visibility or settings, deleting branches, repos or data, spending money, changing production or PostHog configuration. Ask for one exact action at a time, such as "merge pumpkit#42", and act only on a yes to that action (ADR 0004).
- **Ask first, as a decision:** product or design choices with meaningfully different outcomes. Give your recommendation and keep independent work moving while you wait.
