# Interruption context and physical hearing

The Hub tags an interrupted reply prefix with `basis="queue_estimate"`. The Talker receives that prefix as context and must not treat it as proof that the User heard those exact words. This metadata grants no Task or Approval authority.

SoundPlayer tracks output samples submitted through its callback. Within a partly played clip, the text prefix is proportional to its sample progress; it has no word-level alignment. Device buffering, fading and the actual room/device path further separate callback progress from physical hearing. Completed fake-player clips can establish deterministic scheduling behavior, but cannot validate real word boundaries or DAC output.

A delayed-correction Hub regression plays one fake clip while another remains queued, then checks that the interruption context contains only the played prefix, explicitly labeled as an estimate. The source policy no longer tells the Talker that this is all the User heard. Physical timing and word attribution remain unmeasured and require the targeted human session. This change does not claim that #41 or Spec #26 has passed those gates.

A keyword whose source timestamp predates a newer typed request cannot reopen listening. The Hub regression delivers that late detection while the typed reply is pending, then verifies that a genuinely fresh wake still works. Natural listening expiry remains distinct: a delayed wake already captured before that expiry can retain the existing recovery behavior.

## Integrated race checks (#41)

The #40 capture/assessor path is now integrated with the playback-estimate metadata and typed-wake freshness guard. Hub fakes cover an Announcement source whose endpoint arrives after natural completion and Follow-up expiry, duplicate endpoint delivery, an ambiguous opening retained with a later directed segment, and an ignored Backchannel excluded from that request. They also cover queued second news after interruption, Quiet-mode news-count delivery without replay after voice-only stop, stale assessments/legacy partials after typed replacement or shutdown, and a duration-limited Announcement candidate that leaves playback running and cannot admit its tail without assessment. Existing Capture tests retain a four-second raw candidate beyond pre-roll with playback-reference submission active; this is adapter scheduling evidence rather than speech-recognition quality.

The source onset fixes eligibility; delivery time and later background speech do not extend the original deadline. Request-owned corrections preserve Task scope holds, while an Announcement carries an empty admitted request and creates a fresh request only after a current floor decision. A source/capture loss or documented duration/retention limit must produce the existing notice rather than silently admit a shortened request. Generation interruption drains obsolete work through the Hub before a replacement line is sent. Fake players verify queue ordering and exactly-once delivery, not acoustic audibility.

## Targeted session script

Use the existing authorized human session and its final configuration record. Nothing here authorizes opening audio devices or making model calls. Keep raw audio/transcripts local; record reviewed counts, conditions and aggregate errors. The following checks remain unmeasured:

1. During a long EN/IT reply, give a correction longer than the configured pre-roll while the reply continues. Repeat with a Backchannel followed by a correction and with an ambiguous opening that becomes directed. Check the entire selected raw request once, including its opening; rejected background must stay out.
2. Repeat during an Announcement. Let one candidate span Announcement completion and its Follow-up deadline. Queue a second news item, interrupt the first, and verify each delivered item once, with no phantom resume or replay. Repeat after Quiet mode releases held news.
3. Replace an in-flight correction with typed input, and repeat with a newer spoken owner and shutdown. Deliver old recognition/generation/audio completion late; verify that the current owner keeps the floor. Exercise the configured candidate limit and source-loss notice; recover with a fresh idle wake.
4. Compare the interruption context with the actual speaker/headphone recording. Report callback queue estimates separately from physical word boundaries, echo leakage and onset-to-silence. Count false stops and missed directed requests together with delay; no numeric directed-stop limit has been agreed.

The fake suite does not establish real-local recognition accuracy, final provider quality, physical hearing/stop timing or the integrated device configuration. Those gates remain open under Spec #26.
