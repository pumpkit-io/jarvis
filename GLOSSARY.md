# Jarvis

Jarvis is a voice-first personal assistant for one person. It talks with them about whatever they bring up, and gets work done on the repos of their GitHub organization through subagents.

## Language

### People and agents

**Jarvis**:
The whole system as the User experiences it: one voice and one persona that talks with the User about anything and gets work done in the Org. The Hub, the Talker and the Orchestrator are its parts.
_Avoid_: assistant, bot, main agent

**User**:
The one person Jarvis works for, named by `JARVIS_USER_NAME`. Jarvis addresses them as "sir".
_Avoid_: master, owner, operator

**Hub**:
The always-running part of Jarvis that holds the one conversation, turns speech into text and text into speech, decides when Jarvis listens and speaks, and runs the Talker, the Orchestrator and every Subagent.
_Avoid_: server, daemon, runtime

**Client**:
One way the User reaches the Hub, owning one device's microphone, speaker and screen. There are two kinds: the terminal Client on the Mac and the iPhone Client.
_Avoid_: app, device, frontend, session

**Talker**:
The agent that holds the conversation with the User and is the only part that speaks as Jarvis. It answers directly or hands work to the Orchestrator as a Task.
_Avoid_: voice agent, front agent, frontend

**Orchestrator**:
The agent behind the Talker that plans each Task, delegates it to Subagents, judges their results and reports back. It never changes a repo itself.
_Avoid_: Jarvis, manager, planner

**Subagent**:
An agent the Orchestrator starts to do practical work, such as changing code, investigating or reviewing. The Orchestrator picks its model by how hard the task is.
_Avoid_: worker, helper, child agent

**Summariser**:
The agent that writes each new Memory during a Fold, from the previous Memory and the exchanges being folded. It has no tools and never speaks to the User.
_Avoid_: compactor, memory agent

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

**Report**:
What the Orchestrator tells the User about a Task: one sentence the Talker relays, and optional Details. A Report with Details has its own id, such as R12, unique across launches, unlike the id of its Task.
_Avoid_: result, update, summary

**Details**:
The markdown of a Report, shown on screen at once and kept in the 30-day log. They reach the Talker only when the User wants to hear them, one part of about two minutes of speech at a time, so a long plan never fills the Conversation unasked. They can be the whole text of a file the Orchestrator attaches.
_Avoid_: body, attachment, full report

**Authorization**:
The User's permission for one action, one Task, or explicitly the current session, including any conditions and exclusions. It may be given before the exact action is ready. Task permission survives a restart and ends with the Task or revocation; session permission ends at restart.
_Avoid_: blanket approval, permission mode

**Approval**:
The single-use record permitting one concrete action reserved by the Boundary, such as merging one PR. It records the User's words and either their agreement to the presented action or the Authorization that covers it. It applies only to its recorded target state.
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
The Max subscription's cap on model use, which stops a session until its window resets. When it stops the Talker, the Orchestrator or a Subagent, Jarvis says so with the reset time, holds the Orchestrator's Task and any new Subagent until then (at least a minute, doubling each time a reset turns out early), and makes one Announcement that it reset, only once a turn of a held session completes after its hold cleared, so the User is never told a reset that was not real. A retry that is limited again stays silent unless the reset the User heard moves later or becomes unknown. Until a limited session has a successful turn again it is the same limit, told of once. A limit on the Summariser is never told: it only holds the next Fold until the reset.
_Avoid_: rate limit, quota, billing limit

### Conversation

**Conversation**:
Everything the User and Jarvis have said to each other, as one ongoing sequence of exchanges shared by the Mac and the iPhone and continued across Hub restarts until the User resets it. Each exchange keeps the device it came from.
_Avoid_: chat, session, thread, history

**Memory**:
The summary of the older part of the Conversation that the Talker reads before the Tail: standing facts and preferences, open threads and promises, the User's decisions and recent topics. It is what Jarvis remembers, never a source of permission, and it holds no Approvals, Task state or secrets.
_Avoid_: context, history, notes, profile

**Fold**:
The background step that folds the exchanges before a cut into the Memory, which the Summariser writes anew from the previous one, then gives the Talker a fresh start on that Memory and the Tail while Jarvis keeps talking.
_Avoid_: compaction, compact, summarisation, restart

**Tail**:
The newest part of the Conversation, kept word for word after a Fold, beginning at a pause between two exchanges.
_Avoid_: recent messages, window, buffer

**Start afresh**:
The User asking, in one sentence and with no confirmation, for a Fold of everything said before that sentence, so a new subject begins with only the Memory behind it. The sentence and everything after it stay word for word, Jarvis keeps talking while the Memory is written, and Tasks, Authorizations and Approvals are untouched.
_Avoid_: reset, clear, new chat, forget

**Reset context**:
The User deleting the Conversation and its Memory on purpose, from the iPhone's Settings after a confirmation, for the Mac and the iPhone at once, carried out by the Hub with no model. The 30-day log, the transcript on both screens, Tasks, Authorizations and Approvals stay, and Recall reaches nothing from before it.
_Avoid_: clear history, new chat, wipe, start afresh

**Recall**:
Jarvis looking up the exact words of the last 30 days when the User asks what was said: the User's lines, Jarvis's replies, details shown on screen and Task reports, found by word or phrase, with the date of each. It reaches nothing older than 30 days or from before the last Reset context, and it is a record of what was said, never of what a Task or a permission is now.
_Avoid_: search, history lookup

**Wake word**:
A phrase such as "Hey Jarvis", "Hi Jarvis", "Ehi Jarvis" or "Jarvis" that makes an idle Jarvis start listening. During playback it is evidence that speech addresses Jarvis, interpreted in context rather than treated as an unconditional Interruption.
_Avoid_: hotword, trigger phrase

**Spoken request**:
An admitted User request with an identity that survives resumed speech or correction. Each accepted change creates a revision, preserving the opening and added words in order. The current revision owns its Talker generation and reply; replacing or retiring it makes old results stale.
_Avoid_: treating every transcript fragment as a fresh request

**Spoken scope**:
The Hub's pending decision about which existing Tasks an already locally admitted spoken revision changes. The Talker selects the complete affected Task set, a separate request or no change. Unresolved scope holds old Task and Approval authority without rewriting their targets.
_Avoid_: treating a local floor decision as a Task selection

**Talk**:
One push-to-talk capture on the iPhone Client, from press until the User sends or discards it. The User holds to speak and may lock the Talk to speak hands-free. Each sent Talk is a new Spoken request. Outside a Talk, the iPhone Client listens only in Live mode.
_Avoid_: recording, voice message, push

**Live mode**:
A setting of one iPhone Client, off at every launch, under which the iPhone listens for the Wake word and holds a spoken conversation the way the Mac does, Follow-up window included. The Mac does the listening on audio the phone sends it.
_Avoid_: always-on, hands-free mode, wake mode

**Outbox**:
The iPhone Client's saved queue of Talks and typed lines the Hub has not yet accepted. Every one goes through it, sent oldest first whenever the Mac is reachable, so a Talk never waits on the connection.
_Avoid_: offline mode, pending messages

**Follow-up window**:
The ten seconds after Jarvis finishes speaking when the User can go on without the Wake word, on the Client the exchange came from. When it passes in silence, Jarvis plays a short falling closing sound and goes idle.
_Avoid_: grace period, listening timeout

**Stand by**:
The User telling Jarvis to be quiet, in any words. The Talker answers with the `stand_by` tool and no speech; Jarvis stops any reply, plays the closing sound and waits for the Wake word. On an iPhone Client without Live mode it only stops the reply, since that Client listens only during a Talk.
_Avoid_: mute, dismiss

**Announcement**:
Jarvis speaking up unprompted, in one sentence, because a Task has news.
_Avoid_: notification, alert, ping

**Quiet mode**:
A state the User sets in which Announcements wait, on every Client, until the User next speaks to Jarvis by Wake word or Talk, except those that need the User.
_Avoid_: do not disturb, mute

**Focus**:
The one Client that speaks Announcements: the Client the User last gave input to or chose for updates. A reply is spoken on the Client that made the request, whichever has Focus.
_Avoid_: active device, primary client

**Silence**:
A setting of one iPhone Client under which Jarvis speaks nothing on it, replies included, while text and work carry on. News shown on its open screen counts as told. In Live mode the chime and the closing sound still play on it, since they are cues rather than speech. It is neither Quiet mode, which holds Announcements on every Client, nor Stand by.
_Avoid_: mute, do not disturb

**Backlog**:
News left from the last session: what the User had not heard when Jarvis stopped, and what the recovery turn reports at launch. Jarvis never announces it; it opens the User's next turn as context for the Talker, which tells it only when the User asks.
_Avoid_: catch-up, recap, briefing

**Barge-in**:
Speech heard while Jarvis is speaking. It may address Jarvis, acknowledge what it says, or belong to another conversation; hearing it alone does not make Jarvis pause or stop.
_Avoid_: overlap, talk-over

**Backchannel**:
A short sound of attention during a Barge-in, such as "okay", "mhm" or "sì", that does not ask Jarvis to stop.
_Avoid_: filler, acknowledgment

**Interruption**:
A Barge-in that clearly addresses Jarvis and asks to take the floor. Jarvis stops speaking and listens; speech length or particular words alone do not establish an Interruption.
_Avoid_: cancel, abort
