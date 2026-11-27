# The Talker hears the local time and the plan's usage with each User line

The Talker knew only the date the CLI put in its context, in the Mac's zone. It could not say what time it was for the User abroad, and it could not answer how much of their Claude plan they had left.

Each User line now reaches the Talker opening with `<now>`: the local date, time, weekday and IANA zone with its UTC offset. The zone is the one the device the line came from last reported. The iPhone sends `X-Jarvis-Time-Zone` with every request, read when the request is sent, and the Door records any valid IANA name it carries. Lines from the Mac, and from a phone that sent no zone, use the Mac's system zone, read from `/etc/localtime` each time so a Mac that changes zone is followed while the Hub runs. Nothing in the system prompt changes, so its prompt cache stays as it was.

The plan's usage comes from the CLI's `get_usage` control request with `skip_behaviors` and `detached`: the same five-hour and weekly figures `/usage` shows, which the CLI reads from Anthropic with its own sign-in. Jarvis sends nothing anywhere itself, and no model call is made. The Hub asks the current Talker session every five minutes, from the moment it opens. The reading reaches the Talker as one `<claude_usage five_hour="…" weekly="…"/>` line on a User line, only when that Talker session has not seen it, or a window has moved by five points or reset since. The latest one in the session's history therefore still holds. A reading over thirty minutes old is not sent, and an account with no plan windows sends none.

Neither line is saved with the exchange: each saved exchange keeps its own time, so seeds and Memory hold no stale clock or usage. The system prompt has no room under its budget, so the tags are named to read on their own.

The User asked for both on 2026-11-27, with no settings to turn on.

## Considered Options

- The time in the system prompt. Rejected: it changes every minute and would break the prompt cache on every turn.
- The usage through a Talker tool. Rejected: the User's first "how much is left?" would wait for a tool round, and a tool's description costs prompt budget the Talker does not have.
- The usage on every User line. Rejected: a figure that barely moves, repeated in every message, fills the context for nothing.
- The statusline's `rate_limits` or the CLI's `rate_limit_event`. Rejected: Jarvis runs no statusline, and the event comes only when a limit's status changes, for one window at a time.
- Calling Anthropic's usage endpoint directly with the CLI's stored token. Rejected: Jarvis would read the User's credentials itself.

`get_usage` is marked experimental in the CLI and has no SDK method yet, so the adapter sends it through the SDK's own control sender; if either changes, the read fails and the Talker simply gets no usage line.
