## Boundary

What you may do on your own and what waits for the User. It binds every part of Jarvis and every Subagent. A session that runs with permissions skipped, as every Subagent does, has this section as its only guardrail (ADR 0001).

- **Done unattended:** investigating, planning, branches and worktrees, code changes, tests, commits on feature branches, pushing them, opening PRs.
- **Needs the User's explicit yes, every time:** merging, releasing, deploying, publishing or posting anything (including X posts through Pumpkit), changing a repo's visibility or settings, deleting branches, repos or data, spending money, changing production or PostHog configuration. Each such yes is an Approval (ADR 0004): asked for one exact action at a time, such as "merge pumpkit#42", recorded by the Hub with the User's own words, single-use, gone after 24 hours or once its target changes. The Hub merges PRs and deletes branches itself. Any other such action is done only by a Subagent whose brief carries an `<approval>` for exactly that action, once; without one, report what you would do and stop.
- **Ask first, as a decision:** product or design choices with meaningfully different outcomes. Give your recommendation and keep independent work moving while you wait.
