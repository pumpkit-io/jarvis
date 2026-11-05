# Jarvis

Jarvis is a voice-first orchestrator agent that talks with one person about the repos of one GitHub organization and gets work done on them through subagents.

## Language

### People and agents

**Jarvis**:
The whole system as the User experiences it: one voice and one persona that talks with the User and gets work done in the Org. The Hub, the Talker and the Orchestrator are its parts.
_Avoid_: assistant, bot, main agent

**User**:
The one person Jarvis works for, named by `JARVIS_USER_NAME`. Jarvis addresses them as "sir".
_Avoid_: master, owner, operator

**Hub**:
The always-running part of Jarvis that owns the microphone and speaker, decides when Jarvis listens and speaks, and runs the Talker, the Orchestrator and every Subagent.
_Avoid_: server, daemon, runtime

**Talker**:
The agent that holds the conversation with the User and is the only part that speaks as Jarvis. It answers directly or hands work to the Orchestrator as a Task.
_Avoid_: voice agent, front agent, frontend

**Orchestrator**:
The agent behind the Talker that plans each Task, delegates it to Subagents, judges their results and reports back. It never changes a repo itself.
_Avoid_: Jarvis, manager, planner

**Subagent**:
An agent the Orchestrator starts to do practical work, such as changing code, investigating or reviewing. The Orchestrator picks its model by how hard the task is.
_Avoid_: worker, helper, child agent

### Scope

**Org**:
The GitHub organization whose repos Jarvis covers, named by `JARVIS_GITHUB_ORG`.
_Avoid_: company, workspace

**Org repo**:
A repository in the Org. Its local clone sits in the folder above this repo.
_Avoid_: project, codebase

**Repo catalog**:
The short list, kept in the Orchestrator's instructions, giving the purpose of each Org repo. The live repo list the Orchestrator fetches when asked is not the Repo catalog.
_Avoid_: repo index, project list

### Work

**Task**:
One piece of work the User asked for, from the moment the Talker hands it to the Orchestrator until it is reported done, failed or cancelled. A Task may run several Subagents, Watchers and Checks.
_Avoid_: job, request, ticket

**Approval**:
The User's explicit yes to one named action the Boundary reserves for them, such as merging one PR, recorded with the User's own words. It covers that action once, on the state the User was told about, and nothing else.
_Avoid_: confirmation, consent, sign-off

**Watcher**:
A wait on something outside Jarvis that announces its own end, such as a CI run, and wakes the Orchestrator when it ends without any model call in between.
_Avoid_: poller, monitor

**Check**:
A timed reminder the Orchestrator sets for itself when nothing outside Jarvis will announce the change it waits for, such as a human review.
_Avoid_: poll, cron, heartbeat

**Working preferences**:
The User's preferences for how agents behave while doing work. The Orchestrator follows them and hands them to every Subagent.
_Avoid_: orchestrator preferences, rules

**Usage limit**:
The Max subscription's cap on model use, which stops a session until its window resets. Jarvis says so with the reset time, holds the Orchestrator's Task and any new Subagent until then (at least a minute, doubling each time a reset turns out early), and makes one Announcement that it reset, only once a turn of a held session completes after its hold cleared, so the User is never told a reset that was not real. A retry that is limited again stays silent unless the reset the User heard moves later or becomes unknown. Until a limited session has a successful turn again it is the same limit, told of once.
_Avoid_: rate limit, quota, billing limit

### Conversation

**Wake word**:
A phrase such as "Hey Jarvis", "Hi Jarvis", "Ehi Jarvis" or "Jarvis" that makes an idle Jarvis start listening. During playback it is evidence that speech addresses Jarvis, interpreted in context rather than treated as an unconditional Interruption.
_Avoid_: hotword, trigger phrase

**Follow-up window**:
The few seconds after Jarvis finishes speaking when the User can go on without the Wake word. When it passes in silence, Jarvis goes idle without a word.
_Avoid_: grace period, listening timeout

**Announcement**:
Jarvis speaking up unprompted, in one sentence, because a Task has news.
_Avoid_: notification, alert, ping

**Quiet mode**:
A state the User sets in which Announcements wait until the next Wake word, except those that need the User.
_Avoid_: do not disturb, mute

**Barge-in**:
Speech heard while Jarvis is speaking. It may address Jarvis, acknowledge what it says, or belong to another conversation; hearing it alone does not make Jarvis pause or stop.
_Avoid_: overlap, talk-over

**Backchannel**:
A short sound of attention during a Barge-in, such as "okay", "mhm" or "sì", that does not ask Jarvis to stop.
_Avoid_: filler, acknowledgment

**Interruption**:
A Barge-in that clearly addresses Jarvis and asks to take the floor. Jarvis stops speaking and listens; speech length or particular words alone do not establish an Interruption.
_Avoid_: cancel, abort
