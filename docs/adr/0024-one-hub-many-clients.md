# One Hub serves several Clients, and one of them has Focus

The User wants to reach Jarvis from an iPhone as well as from the terminal, on the same conversation and the same Tasks. We keep one Hub and let it serve several Clients: the terminal Client and the iPhone Client. There is no second Jarvis and no split into services. The Hub's input and its output, speech and floor are already injected, so a Client attaches at those seams and the Talker, Orchestrator and Approval logic do not change.

Requests from every Client join the one Talker inbox in arrival order. Input from any Client stops speech on every Client. A reply is spoken only on the Client that made the request; text goes to all of them. A typed line gets a text-only reply on the phone as it does on the Mac in voice mode.

An Announcement is spoken only on the Client with Focus. Focus moves on input alone: a Talk, a typed line, a spoken request on the Mac, or the app's "Send updates here" control. Opening the app does not move it.

When the Focus Client is disconnected or suspended, the Announcement is spoken nowhere else. The Hub holds it the way it holds Quiet-mode news (ADR 0012). If the phone has Focus and the app opens with Silence off, the held news goes out as one batched Announcement once the phone is connected and idle. With Silence on, news shown on the open screen counts as told, as text mode's screen does in ADR 0010.

Quiet mode stays Hub-wide and ends when the User next speaks to Jarvis, by Wake word or by Talk. Stand by on the phone only stops the reply. An Approval is never answered with a button: the app shows the question and the User answers by Talk or typed line, so ADR 0022's path through the Talker and the Hub check is the only one.

The Hub now saves the conversation. ADR 0011 saved none of it. An event log keeps 30 days of what was heard, replied and shown, as text, so a reconnecting Client catches up and both Clients show the same history. Uploaded audio is deleted once transcribed, and synthesized speech is never stored. The log does not change what the Talker remembers: a restart still starts a fresh Talker.

The User decided this on 2026-11-18.

## Considered Options

- A second Jarvis on the phone, or a Hub per Client. Rejected: Tasks, Approvals and the conversation would have two owners, and the User asked for one.
- Focus follows the app coming to the foreground, or goes to the phone whenever it is connected. Rejected: a glance at the phone while sitting at the Mac would move the voice by accident.
- Speak an Announcement on another Client when the Focus Client cannot speak. Rejected: the User may not be near that Client, and the Hub would count the news as heard.

## Consequences

- If the User talks to the Mac and then leaves, Focus stays on the Mac and the next Announcement is spoken to an empty room. The same happens today with one Client.
- In the first version the iPhone Client speaks an Announcement only while the app is open, or while a reply is still playing after the screen locks. iOS suspends the app otherwise, and there are no notifications yet, so news waits until the app opens.
- The Hub still stops with its terminal in the first version, so the phone reaches Jarvis only while `./jarvis` runs on the Mac. Making the Hub outlive the terminal is later work.
- A session-scoped Authorization (ADR 0022) belongs to the Hub's session, so one given on the Mac also covers requests made from the phone.
