# The Hub listens on loopback, and Tailscale and a device token guard the door

The Hub runs Subagents with permissions skipped (ADR 0001), so a network listener on it is a way into the Mac. The iPhone Client still has to reach it from cellular. The Hub binds its listener to `127.0.0.1` only and refuses any other address. `tailscale serve` publishes that port inside the User's tailnet over HTTPS, with a certificate Tailscale manages, so iOS connects without transport-security exceptions. `tailscale funnel` is never used, and there is no public endpoint and no VPS.

The tailnet is one lock and pairing is the second. `./jarvis pair` prints a short one-time code; the app trades it for a long-lived token for that device, kept in the iOS Keychain, and the Hub stores only its hash. `./jarvis revoke` removes a device. The door is off unless the User turns it on.

The phone receives conversation text, details the Hub shows, Task summaries and speech. No endpoint reads the filesystem, Subagent logs, the environment or credentials.

An unlocked phone can speak an Approval. We add no Face ID check to Approvals: the Mac has no stronger check, since anyone in the room can say yes, and revoking the device is the remedy for a lost phone.

A Talk is refused while the Hub is unreachable and is never queued for later. A request spoken minutes ago should not run a merge when the signal returns. An upload that loses its connection retries with the same id for about 30 seconds, then fails where the User can see it.

The User decided this on 2026-11-18.

## Considered Options

- Bind the Hub to the tailnet address. Rejected: plain HTTP would need an iOS transport-security exception, and a bind mistake could expose the port on another interface.
- Rely on the tailnet alone, without pairing. Rejected: any device that joined the tailnet could drive an agent host that skips permissions.
