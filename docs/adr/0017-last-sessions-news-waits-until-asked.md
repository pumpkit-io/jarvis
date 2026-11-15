# The last session's news waits until the User asks

A launch used to tell the User everything the last session left: held news came back as a "while you were away" opener, news the limited Talker could not tell went out after the reset, and the recovery turn's reports were announced like any other news. The User wants each conversation to start fresh, so a new session opened with work from the old one, and the User could not just talk about something else.

So the Hub keeps that news as the Backlog. At launch, the held and parked news the state file saved goes to the Backlog, and so does every report and failure from the recovery turn (ADR 0011). None of it is announced or shown. The User's next turn, typed or spoken, opens with a `<event source="hub" backlog="N">` line and each event after it, as context for the Talker. The Talker prompt says to tell none of it unless the User asks what is pending or about that work, or what they ask depends on it. Once attached, the Backlog is gone from the Hub; `status` still lists every open Task with its last report and pending Approval, so a later question can be answered from there. A Backlog not yet attached is saved with the held news, and the next launch restores it as Backlog again.

News that arrives after the recovery turn is announced as usual, even on a Task the last session started: a Watcher restored at launch that ends mid-conversation is told when it lands. Spoken-scope holds are not Backlog, since they belong to a revision in progress.

The User decided this on 2026-11-15, including that news which needs them, such as an Approval question, waits in the Backlog too.

## Considered Options

- Hold the restored news in Quiet mode, so the next Wake word opens with "N updates while you were away". Rejected: the User would still hear about old work before their own request.
- Leave the news out and rely on `status` alone. Rejected: the Talker would not know there was anything to ask about, and the reports' wording would be lost.
- Let news that needs the User through. Rejected by the User: they ask when they want it.

## Consequences

- A blocked Task can wait silently until the User asks. Its Approval stays pending and expires after 24 hours as usual (ADR 0004).
- The Backlog rides on the User's next turn even when the Talker does not mention it, which costs its tokens once.
- A User turn cut off by a limit keeps the Backlog for the next one, but a turn that fails after it was attached drops it; `status` still has each Task's last report.
