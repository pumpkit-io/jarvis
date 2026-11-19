# Whole-stack review fixes

The final review of the iPhone Client added source-aware terminal echo, Talk key press feedback and release of obsolete speech destinations. The regression tests use the Hub, fake agent/audio adapters and the loopback protocol described in Spec #91.

## Terminal and speech ownership

Canonical `heard` records keep typed provenance and the originating Client/request identity. Displays receive the source Client identity so an interactive terminal suppresses only its own typed echo. The production text-entrypoint regression uses a fake TTY and a real loopback `say_text` exchange; remote and local User lines each appear once.

Retirement removes a destination from Speech's active players and replay ownership. Speech retains played text as a value when its last destination retires. Queued synthesis and externally held completion checks keep their reply alive for their legitimate lifetime; the idle worker releases its last queue item. Completed replay records are pruned with completed replies. Local cues still use their generation guard, so retirement cannot revive queued stale cues when a fresh local chime starts.

`test_replaced_remote_players_release_transport_owners_after_live_and_replay_work_drains` uses public Hub attachment, admission and replay with fake transports. Held live and replay clips require actual playback reports before completing. Weak references prove that all replaced players and transport owners are collected after the final replacement. The diagnostic final-retirement probe also covers completion receipt truth, retained played text and a fresh local chime. No Player weak-reference or hashability requirement was added.

## Replaced test observations

The following map records the intent preserved when the review removed behavioral assertions on saved JSON and the direct `SpokenScopes` schema tests.

- Two Clients uploading the same local Talk counter still receive distinct request IDs. `test_two_clients_same_talk_counter_have_distinct_scopes_and_approval_targets` observes canonical Client/request identities, separate Task IDs/titles, the exact question for A1/T1 and refusal to resolve the completed uploaded request as a revision of T2. A third independent Task leaves A1 attached to T1 and its original question. This replaces saved scope-to-Task and Approval dictionaries.
- The five-minute upload test retains the actual five-minute PCM body, transcript longer than 6000 characters, exact canonical `heard` text and exact Talker input. Task/Approval snapshots and refusal to resolve the one-shot request replace `segments`, `revision`, `task_ids` and `pending` assertions. Restart checks replay completion and restored Task/Approval identity. An unpresented Approval refuses without an acting Subagent; a fresh question and subsequent User answer authorize that same Approval.
- `test_long_uploaded_user_words_authorize_max_effort_before_and_after_restart` replaces the saved Task `words` assertion with an authority oracle. Only the User transcript tail beyond character 6000 asks for max effort; both delegate summary and Subagent brief omit it. An actual fake `spawn_subagent` succeeds before and after restart. The matched case where only the delegate summary asks for max effort refuses both times. A scheduled check keeps the Task open through restart. Exact transcript reception and tail-dependent authority are separate claims; the test does not claim byte-for-byte inspection of persisted words.
- `test_pending_local_task_authority_survives_heavy_uploaded_history` replaces direct retention-count and held-scope assertions. A local spoken amendment leaves its Task unresolved while 70 independent long uploaded requests are admitted through the Hub. The old Task report still waits, and its stale local resolver refuses. Existing voice-input tests retain the local 6000-character admission and amendment bounds.
- Existing uploaded-Talk protocol tests retain exact retry receipts, one canonical heard event, conflicting-content/identity rejection, independent later Talks and stale SDK-tool refusal. These behaviors replace direct immutable `SpokenScopes.admit` assertions.
- The exact dump count, `one_shot` field and malformed-storage mutation matrix were internal policy cases rather than agreed observation seams. They were removed. Valid long-request restart, immutable protocol behavior and protected old Task authority cover their user-visible intent. Raw malformed-storage acceptance/rejection has no newly declared public contract.

At-rest SQL assertions remain in the lost-response upload test. They prove that command/Talk lifetime tables contain no transcript or PCM and that stored upload receipts expose only safe identity fields. Those confidentiality checks serve a different purpose from the removed storage-based behavioral assertions.

The reference Client and E5 runner share audio eligibility and outcome classification. Eligibility is captured when the audio wait starts: a later Silence status cannot relabel an already requested stream as `not_requested`. The executable regression holds synthesis, receives the completed canonical reply, then applies Silence and verifies the original unavailable/deadline outcome. Each caller still owns its deadline and error handling.

## Verification boundaries

Python and native tests use fake agents, clocks, recorders, playback and transport. Mac-hosted rendering and synthetic mouse gestures exercise actual SwiftUI sources; they do not establish iPhone touch behavior. Direct iOS SDK linking and unsigned packaging do not establish signed installation. Physical gestures, microphone indicators, audio routes, VoiceOver, Dynamic Type, device/network/account checks and real E5 measurements remain Waiting on User.
