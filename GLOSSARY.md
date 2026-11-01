# Jarvis

Jarvis is a voice-first orchestrator agent that talks with one person about the repos of one GitHub organization and gets work done on them through subagents.

## Language

### People and agents

**Jarvis**:
The orchestrator agent that runs in this repo. It talks with the User, makes recommendations, and delegates practical work to Subagents.
_Avoid_: assistant, bot, main agent

**User**:
The one person Jarvis works for, named by `JARVIS_USER_NAME`. Jarvis addresses them as "sir".
_Avoid_: master, owner, operator

**Subagent**:
An agent Jarvis spawns to do practical work, such as changing code, investigating or reviewing. Jarvis picks its model by how hard the task is.
_Avoid_: worker, helper, child agent

### Scope

**Org**:
The GitHub organization whose repos Jarvis covers, named by `JARVIS_GITHUB_ORG`.
_Avoid_: company, workspace

**Org repo**:
A repository in the Org. Its local clone sits in the folder above this repo.
_Avoid_: project, codebase

**Repo catalog**:
The short list in AGENTS.md giving the purpose of each Org repo. The live repo list Jarvis gets at session start is not the Repo catalog.
_Avoid_: repo index, project list

### Work

**Briefing**:
Jarvis's spoken summary of what needs the User across the Org repos: open PRs and their CI, issues assigned to the User, local unpushed work, the last production deploy and product analytics.
_Avoid_: status report, standup, digest

**Working preferences**:
The User's preferences for how agents behave while doing work. Jarvis follows them and hands them to every Subagent.
_Avoid_: orchestrator preferences, rules
