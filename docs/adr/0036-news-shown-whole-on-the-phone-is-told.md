# News shown whole on the phone is told, even when its speech is cut

The Hub used to count news as told only once its speech had played to the end. If anything cut that speech, the Hub held the news and told it again later as a "while you were away" opener. A typed line, the phone reconnecting or a fresh player after an app status change could all cut it. The Talker had already told that news, and the reply text was already on the phone's screen and in the event log every Client replays. So the second turn only repeated it: "That's the same update again, sir." The User wants Jarvis to speak up only when asked or when there is new news.

News is now told once the Talker's reply that tells it completes on the phone with Focus while its screen is open, without the Hub interrupting that turn. This holds even if the reply's speech is then cut. News held while that reply was still being written is dropped again once the reply completes on that open screen. ADR 0024 already counted news shown on the open screen as told under Silence; this extends the rule to speech that was started.

News is still told again when the User could have missed it:

- the reply did not complete, or the Hub interrupted its turn;
- the phone's screen was closed or the phone was disconnected when the reply completed;
- Focus moved to another Client during the Announcement, so the news follows the User;
- the reply went to the Mac in voice mode, where the terminal is not where the User looks.

The User asked for this on 2026-11-27, after duplicate updates on Tasks T6 and T8.

## Considered Options

- Dropping repeated events by report id. Rejected: no event was posted twice. The same news was held again and told again.
- Counting every completed reply as told. Rejected: news the User neither heard nor saw, on a closed screen, the Mac in voice mode or after a Focus move, would be lost.
