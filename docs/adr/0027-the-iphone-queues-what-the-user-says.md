# The iPhone queues what the User says until the Hub takes it

ADR 0026 refused a Talk while the Hub was unreachable, so a request spoken minutes ago could not run a merge when the signal returned. In use the phone lost the Hub for a second or two many times an hour, on 4G behind Tailscale. Each time the talk key went grey and a Talk in progress was cancelled. The User could not rely on being heard.

Every Talk and typed line now goes into the iPhone's Outbox, a queue saved on the phone, whatever the connection. The Outbox sends one at a time, oldest first, whenever the Hub is reachable, and keeps doing so in the background. The talk key and the typing field never wait on the connection. A Talk still sends `talk_begin` when the socket is up, so speaking stops Jarvis at once, but its upload no longer depends on that command.

An upload whose Talk identity was never begun is a queued Talk: it holds no floor and needs no open socket. Its command id makes retries exact, as before, and the phone retries for as long as it takes. A Talk the Hub began and then ended still cannot be uploaded; the phone sends it again under new ids. Each item carries when it was said, and a line that arrives two minutes or more late reaches the Talker as `delayed`. The Talker confirms a consequential action or an Approval answer from a delayed line before acting on it, which keeps the protection ADR 0026 wanted without dropping what the User said.

The User decided this on 2026-11-20.

## Considered Options

- Keep refusing Talks offline and only reconnect less often. Rejected: a dead spot on a train still silences the User, and the User asked to be heard whatever the signal.
- Hold queued lines until the User confirms each one on reconnect. Rejected: it turns every delayed question into a chore; the Talker can tell which lines need confirming.
