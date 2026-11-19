# Client protocol v1

This is the wire contract for Spec #91 and ticket #92. The terminal Client attaches in-process; only remote Clients use this protocol. The Hub owns the conversation, Tasks, Approvals and speech models. A Client never executes an Approval directly.

## Transport and version

The door is off unless `JARVIS_REMOTE=on`. It listens only on `127.0.0.1`, on `JARVIS_REMOTE_PORT`. A non-loopback bind is a startup error. Tailscale Serve exposes that listener inside the tailnet as HTTPS; Funnel is never used. The examples use `https://mac.example.ts.net` as a placeholder for the paired Mac's actual origin.

All endpoints start with `/v1`. The WebSocket URL is `/v1/events?after=0`; its subprotocol is `jarvis.v1`. HTTP responses and WebSocket control messages carry `version: 1`. An unsupported version fails before executing anything, with HTTP 426 and `unsupported_version`; the response lists `supported_versions: [1]`. An incompatible change gets a new path and WebSocket subprotocol. Additive optional fields do not change v1: receivers ignore unknown fields, and ignore unknown event types while advancing their cursor. Unknown commands fail with `unknown_command`.

JSON uses UTF-8. Times are UTC RFC 3339 strings. Sequence numbers are nonnegative signed 64-bit integers. Identifiers are opaque strings, case-sensitive; Client ids come from the Hub, command ids and Talk ids from the Client (UUIDs are recommended). An event's `client_id` identifies its source or destination as specified below; `null` means Hub-wide. Device names are display labels, never authentication identities.

## Authentication and limits

Every HTTP request except pairing carries `Authorization: Bearer <device-token>`. The WebSocket upgrade uses the same header; tokens never appear in URLs. Pairing authenticates with the one-time code instead. Health also requires a token. Invalid or revoked tokens receive HTTP 401 `unauthorized`; a revoked socket closes with code 1008. Clients erase a refused token and return to Pairing. Tokens are random secrets of at least 256 bits; the Hub stores only hashes and compares them in constant time. The iPhone keeps its token only in Keychain. Responses and logs must not disclose tokens or pairing codes.

The following are v1 defaults, chosen for this slice:

- A pairing code expires 5 minutes after creation and is consumed atomically once. Creating another code invalidates the previous one.
- Five failed pairing attempts within 5 minutes lock pairing for 5 minutes, across the listener rather than per claimed Client. During lockout, return HTTP 429 `pairing_locked` and `Retry-After` in seconds. An expired known code returns `code_expired`; an incorrect or already consumed code returns `invalid_code`.
- An authenticated Client may begin at most 10 new utterance uploads per rolling minute, with at most one upload being transcribed at a time per Client. Exact retries reuse the original operation and do not consume another allowance. Excess new uploads return HTTP 429 `upload_rate_limited` and `Retry-After`.
- Client-to-Hub JSON requests and WebSocket commands are capped at 64 KiB. This is an incoming-request limit, not a limit on Hub-to-Client events or history responses: retained reply text, shown details and aggregate snapshots may exceed it. V1 defines no Hub event size ceiling; Clients must preserve full valid events through caching and replay rather than truncating or skipping them. A typed line is nonempty and at most 16,384 UTF-8 bytes. Device names are 1 to 80 UTF-8 bytes after trimming outer whitespace, without control characters. Active names are unique and case-sensitive. A duplicate friendly name receives a suffix such as `My iPhone (2)`; the original pairing stays valid. A revoked name may be reused with a new Client identity.
- An upload contains at most 300 seconds of audio: 9,600,000 bytes. Reject a larger body while reading, including chunked bodies, with HTTP 413 `audio_too_large`.

Only conversation text, explicitly shown details, Task summaries, Approval questions, status and live speech cross this boundary. There is no file, log, environment, credential or metrics endpoint. The existing Hub tool guards and Authorization checks apply unchanged to remote input.

## Commands and results

A WebSocket command has exactly this envelope, with a command-specific payload:

```json
{"version":1,"id":"cmd-1","type":"say_text","payload":{"text":"How is the refactor going?"}}
```

The socket's token identifies its Client; a payload cannot impersonate another Client. Results return only to that Client, outside the sequenced event stream:

```json
{"version":1,"type":"command_result","id":"cmd-1","ok":true,"result":{"request_id":"request-1"}}
```

```json
{"version":1,"type":"command_result","id":"cmd-1","ok":false,"error":{"code":"invalid_payload","message":"text must not be empty","retry_after_seconds":null}}
```

HTTP errors use the same `error` object with top-level `version: 1`; they omit `id` when no command id was supplied. Error messages are safe to display and contain no internal traceback. Schema errors return HTTP 400 where HTTP is used. A command result acknowledges admission or a completed control action, not the end of a Talker reply or Task.

Command ids are unique per authenticated Client across WebSocket commands and utterance uploads. The Hub durably reserves `(client_id, id)` before any side effect. A completed repeat with the same type and payload receives its original result, including original errors and identifiers, without repeating the effect. A concurrent repeat waits for the reserved operation's result; it cannot run it again. Reusing an id with different content returns `id_conflict`. The command result record survives Hub restarts and is retained for the lifetime of the paired Client, independently of 30-day event pruning. If a restart leaves an admitted operation without a known result, return `outcome_unknown` for that id, never execute it again. The Client must show the uncertainty and reconcile with history before starting a new request.

An interrupted upload retries the same id and byte-identical body for at most about 30 seconds from the first attempt. The reservation must distinguish incomplete receipt (safe to receive the same body again) from admission to transcription/the Talker (must not repeat it). A Client never queues a fresh Talk or typed request while disconnected.

### Client command catalog

Every example below is the `type` and `payload` portion of the command envelope. All fields shown are required unless stated otherwise. Every command produces `command_result`.

| Type | Payload and example | Successful result |
|---|---|---|
| `say_text` | `{"text":"How is the refactor going?"}` | `{"request_id":"request-1"}` |
| `talk_begin` | `{"talk_id":"talk-1"}` | `{"talk_id":"talk-1"}` |
| `talk_cancel` | `{"talk_id":"talk-1"}` | `{"talk_id":"talk-1","cancelled":true}` |
| `interrupt` | `{"speech_id":"speech-1","last_clip_index":0}` | `{"speech_id":"speech-1","stopped":true}` |
| `playback_report` | `{"speech_id":"speech-1","clip_index":0,"state":"finished"}` | `{}` |
| `set_presence` | `{"can_show_text":true}` | `{"can_show_text":true}` |
| `set_silence` | `{"silence":true}` | `{"silence":true}` |
| `take_focus` | `{}` | `{"focus_client_id":"iphone-1"}` |
| `replay_speech` | `{"reply_id":"reply-1"}` | `{"speech_id":"speech-2","reply_id":"reply-1"}` |
| `acknowledge` | `{"seq":42}` | `{"seq":42}` |

`say_text` joins the one Talker inbox in arrival order, takes Focus and stops all speech. Its reply is text-only even if Silence is off. It uses the same Talker tools and Hub checks for Approval answers as terminal input.

`talk_begin` is sent when recording starts. It takes Focus, ends Quiet mode, stops all speech and suppresses the Mac microphone while the Talk is active. One Talk is active per Client. Beginning another while one is active returns `talk_in_progress`. If the Client was playing speech, it first sends `interrupt` with what it heard, then `talk_begin`. `talk_cancel` discards the capture without a User line or a Talker turn. A cancelled, uploaded or expired Talk cannot later be uploaded under a new command id. Disconnect releases the active recording floor; the Hub retains the Talk identity for the 30-second upload retry window. An abandoned Talk also releases its floor after 300 seconds plus a 30-second upload allowance.

`interrupt` refers only to speech sent to this Client. `last_clip_index` is the last fully heard clip, zero-based, or `-1` when none completed. It stops synthesis and playback, emits `speech_stop`, and carries the heard and unheard text into the next turn using the Hub's existing interruption path. A partially played clip is treated as unheard. Input from any Client also stops speech on every Client. `stand_by` remains a Talker tool; on a phone turn it stops the reply without changing other conversation state.

`playback_report.state` is `started`, `finished` or `stopped`. `clip_index` is zero-based; `stopped` may use `-1` before any clip starts. Only reports for clips sent in that Client's speech stream are accepted. Ordered reports update its played record; repeated or late reports cannot move it backwards. The remote floor is busy as soon as a clip is queued for transport, before a delayed `started` report can arrive, until all sent clips finish, a stop, or disconnect; speech end alone does not mean the last clip was heard. The Mac ignores microphone frames during remote recording or playback.

`set_presence` describes this socket's display availability. A new or replacement socket starts unavailable. After safely applying replay and current snapshots, the foreground Client sends `can_show_text: true` with a fresh command id for that connection, then becomes ready after the result. Backgrounding sends `false` and awaits the result before retaining a socket for active playback; failure closes the socket. Presence never moves Focus or changes Silence. It gates new Announcements and the start of new reply speech. If an opening is skipped while unavailable, that reply stays text-only; foregrounding never starts its later sentences. An already playing reply, including an Announcement, may drain while unavailable; it keeps its existing playback eligibility. Reconnecting resets presence rather than restoring the previous socket's value. No new requests are admitted by the app while its foreground handshake is incomplete.

`set_silence` persists for the Client across reconnects. Turning it on stops that Client's speech and synthesis immediately; text and Tasks continue. It does not affect the Mac's voice. `take_focus` moves Focus without a Talker turn. Connecting, foregrounding and disconnecting do not move Focus. Focus survives a Hub restart.

`replay_speech` synthesizes retained reply text again for this Client only. It does not run the Talker, create another reply or change Focus. Unknown/pruned reply ids return `reply_not_found`; Silence returns `silenced` without synthesizing. Only one speech stream plays at a time. A new replay stops the old stream before beginning another.

`acknowledge` records the greatest safely cached sequence for this Client. It cannot advance beyond the latest sequence the Hub has issued; lower acknowledgements do not move it backwards. The reconnect query uses the Client's durable local cursor, so a lost acknowledgement never loses history.

## Connection, replay and history

A Client opens one authenticated WebSocket with `after=<last safely cached seq>` and the `jarvis.v1` subprotocol. A new installation uses `after=0`. The Hub captures a high-water mark, sends retained replayable events after that cursor through the mark in increasing sequence order, then sends:

```json
{"version":1,"type":"replay_complete","through_seq":42,"oldest_seq":1}
```

It then streams live events strictly after the mark. Events committed during replay are buffered or read from the log so there is no gap or double delivery at the handover. Only one socket per Client is active; replacing it closes the old socket with 1000. The Hub sends current `status`, `task_snapshot` and `pending_approval` snapshots after replay, even when unchanged, so old replay cannot leave stale controls on screen.

Every event has a durably allocated, globally increasing sequence, including live-only events. Speech event payloads and audio bytes are never written to the log. Sequence gaps are therefore normal, both during replay and when speech was addressed to a different Client. Never infer missing history from a gap. Persist the allocation counter across restart and pruning; never reuse a sequence. Speech has no replay and is never played automatically after reconnect. A missed reply can be requested with `replay_speech`.

A Client stores replayable events keyed by `seq`, applies each once, and preserves increasing order. It advances its live cursor only after safely caching the event; live-only frames may advance that cursor once handled. Older history pagination does not rewind the live cursor. The Client may advance to `through_seq` after replay completes, since any omitted events through that mark are unavailable or live-only. A cursor beyond the Hub's high-water mark returns HTTP 409 `invalid_cursor` before upgrading. If the cursor predates retained history, replay what remains and include `history_pruned: true` in `replay_complete`; the Client preserves its offline cache and does not imply that all older history was recovered.

Conversation events expire after 30 days and are pruned at launch. The log never supplies the Talker's context on restart. Status and snapshot events describe their event time; only the latest is current. App backgrounding closes the socket unless speech is still playing under the audio background mode. Only a connected socket with foreground display presence can count a new text Announcement as delivered. A handshake or background socket cannot count unseen news as told. Existing audible replies may finish after display presence ends. WebSocket Ping/Pong detects a dead connection; the Hub closes a socket after 30 seconds without a Pong. Reconnect uses backoff (1, 2, 4, 8 seconds, capped at 15, with jitter), resetting when ready; a network change triggers a fresh attempt.

### Event envelope

```json
{"version":1,"seq":12,"time":"2026-11-18T22:00:00Z","type":"heard","client_id":"iphone-1","payload":{"request_id":"request-1","text":"How is the refactor going?","spoken":true}}
```

The tables give the payload of a complete event formed with this envelope. Reply and request ids remain stable through deltas, shown details, speech and reconnect. Replayable text events go to every connected Client that can show text. Speech goes only to its destination Client. Commands never allow choosing another destination.

### Replayable events

| Type | Fields and example payload |
|---|---|
| `heard` | `{"request_id":"request-1","text":"How is the refactor going?","spoken":true}`. `client_id` is the input source. One event per admitted line. |
| `reply_delta` | `{"reply_id":"reply-1","request_id":"request-1","text":"Rather well, sir."}`. Append text in sequence order; `client_id` is the input source. For an Announcement `request_id` is `null`. |
| `reply_end` | `{"reply_id":"reply-1","request_id":"request-1","text":"Rather well, sir.","reason":"complete"}`. Final text is authoritative; `reason` is `complete`, `interrupted`, `stand_by` or `error`. |
| `notice` | `{"text":"Two updates are waiting.","count":2,"task_ids":["task-1","task-2"]}`. `count` and `task_ids` may be omitted for ordinary notices; `client_id` is `null` for Hub news. |
| `show` | `{"reply_id":"reply-1","title":"Details","text":"The tests pass and the PR is open."}`. Explicitly shown text, collapsed under its reply. `reply_id` may be `null` for an unassociated display. |
| `task_snapshot` | `{"tasks":[{"id":"task-1","title":"Refactor login","state":"working","last_report":"Subagent is writing tests.","approval_question":null}]}`. Complete set of open Tasks, each with those fields; `state` is the existing Hub Task state: `queued`, `working` or `needs_user`. Terminal states (`done`, `failed`, `cancelled`) are absent; `last_report` may be `null` before any report. `client_id` is `null`. |
| `pending_approval` | `{"approvals":[{"id":"approval-1","task_id":"task-1","question":"Merge PR 91 into main?","expires_at":"2026-11-19T22:00:00Z"}]}`. Complete set of pending Approval questions; an empty array clears them. These are display records, never action handles. `client_id` is `null`. |
| `status` | `{"ready":true,"usage_limit":{"limited":false,"resets_at":null},"quiet":false,"focus_client_id":"iphone-1","mac_voice_on":true,"silence":false}`. Full current status; `resets_at` is a UTC timestamp or `null` when unknown. `silence` is the destination Client's setting; deliver the appropriate value separately to each Client, and replay only that Client's status. Other Clients must not receive it as their own setting. `client_id` is that destination. |

A reply interrupted before completion still ends with `reply_end`. `reply_delta` has no audio; its arrival does not mean the User heard anything. `task_snapshot` omits Subagent logs, filesystem paths to private content, credentials and full agent transcripts. Approval questions contain the full completed visible Talker reply associated with `present_approval`, rather than the private action record. An incomplete reply adds no question; expiry, withdrawal or approval clears it. Restoring a displayed question does not restore presentation authority for `approve`. There is no approve/refuse protocol command.

### Live-only speech events

| Type | Fields and example payload |
|---|---|
| `speech_begin` | `{"speech_id":"speech-1","reply_id":"reply-1","sample_rate":24000,"channels":1,"sample_width":2}` |
| `speech_clip` | `{"speech_id":"speech-1","reply_id":"reply-1","index":0,"text":"Rather well, sir.","byte_length":48000}` |
| `speech_end` | `{"speech_id":"speech-1","reply_id":"reply-1","last_clip_index":0}` |
| `speech_stop` | `{"speech_id":"speech-1","reply_id":"reply-1","reason":"interrupted"}` |

`client_id` is the destination for all four. `speech_clip` is a JSON header immediately followed by exactly one binary WebSocket frame containing that clip's raw PCM. No other frame is interleaved between the header and its binary frame. Indexes begin at zero and increase by one within a speech stream. `byte_length` is even and matches the binary payload. A malformed/mismatched clip closes the connection with 1002; discard incomplete clips on disconnect. Outgoing clips are signed 16-bit little-endian mono PCM at 24,000 Hz, without a WAV header or compression. The five-minute recording limit applies only to uploads; outgoing speech clips have no duration ceiling. Audio bytes are held only in memory until sent/played, never persisted by the Hub or replay stream.

`speech_end` means synthesis finished and no more clips follow. `last_clip_index` is `-1` when none were emitted. The Client drains already queued clips, reports completion, then releases its audio session. `speech_stop.reason` is `interrupted`, `silence`, `stand_by`, `disconnected` or `error`; stop immediately and discard queued clips. A local Talk or Silence also stops playback immediately without waiting for this event. If no clip arrives for about two seconds while awaiting the next clip, release the playback session; reactivate it only when another clip is ready. The session is inactive while Jarvis thinks.

Announcements use the same reply and speech events, with `request_id: null` on their text. They speak only on the Focus Client when its floor is quiet. If it is disconnected, news is held and never rerouted. Reconnecting with Focus and Silence off delivers one batched Announcement with the exact count when idle; with Silence on, showing news counts as telling it. Input arriving first folds held news into that next turn instead. Quiet mode and news needing the User retain the Hub's existing rules.

## HTTP calls

### Pair

`POST /v1/pair`, `Content-Type: application/json`. The one-time code is this request's credential; it cannot yet carry a device token.

```json
{"version":1,"code":"824917","name":"My iPhone","kind":"iphone"}
```

Successful HTTP 201:

```json
{"version":1,"client_id":"iphone-1","device_token":"<secret returned once>","mac_name":"My Mac","origin":"https://mac.example.ts.net"}
```

`kind` is `iphone` or `reference`. Failure is HTTP 400 `invalid_code` or `code_expired`, or HTTP 429 `pairing_locked`. The optional `client_name` response field gives the assigned unique label for listing/revocation. Suffixes stay within the 80-byte bound by shortening the label if needed. Invalid name/kind/JSON returns `invalid_payload` without consuming the code or counting a failed credential. Wrong, consumed and expired codes count toward lockout; a fifth failure returns `pairing_locked` immediately. Creating a code does not reset lockout. The printed QR encodes a JSON object `{"version":1,"origin":"https://mac.example.ts.net","code":"824917"}`. Manual entry supplies the same origin and code. The Hub must be given its Tailscale HTTPS origin for printing; never invent or infer an externally accessible address from an untrusted Host header. HTTPS origin and tailnet access still need the User's setup. A lost success response consumes the code; generate another rather than replaying a token.

### Health

`GET /v1/health` with the device token. HTTP 200:

```json
{"version":1,"ready":true,"mac_name":"My Mac","usage_limit":{"limited":false,"resets_at":null}}
```

A reachable listener with `ready: false` is starting or stopping; a Usage limit is separate from transport failure. Refused connections and network timeouts are Client-side reachability errors. The protocol cannot by itself distinguish a sleeping Mac from a stopped Hub; the app must not claim that distinction from an HTTP timeout alone.

### Upload one Talk

`POST /v1/utterances/{talk_id}` with the device token, `Idempotency-Key: cmd-upload-1` and `Content-Type: application/octet-stream` and `X-Jarvis-Audio-Format: pcm_s16le_16000_mono`. The body is signed 16-bit little-endian PCM, with no WAV header. It is mono, 16,000 Hz, at most 300 seconds and must have an even, nonzero byte length. A WAV input in the reference Client is decoded and converted before upload.

The Talk must belong to the authenticated Client and have an accepted `talk_begin`; otherwise return HTTP 409 `talk_not_active`. After complete receipt, the Hub transcribes on the one shared service, discards uploaded bytes, and admits exactly one spoken line. It responds HTTP 200 with the command result envelope:

```json
{"version":1,"type":"command_result","id":"cmd-upload-1","ok":true,"result":{"talk_id":"talk-1","request_id":"request-1"}}
```

The result retains only Talk and request identifiers. The transcript is delivered in the canonical `heard` event; it is never retained in lifetime command results. The resulting `heard`, reply and speech events arrive over the WebSocket. An empty transcript returns HTTP 422 `empty_transcript`, with no Talker turn. Unsupported format returns HTTP 415 `unsupported_audio`; transcription failure returns HTTP 503 `transcription_failed`. Rejected, failed, cancelled and disconnected uploads release their audio buffers too. The command idempotency rules apply even if the response is lost after admission. This upload does not run wake word, voice activity detection, Smart Turn or the barge-in judge. A Talk is one Spoken request with one revision.

### Older events

`GET /v1/history?before=42&limit=100` with the device token. `before` is exclusive; omit it for the newest page. `limit` defaults to 100 and is between 1 and 500. HTTP 200:

```json
{"version":1,"events":[{"version":1,"seq":12,"time":"2026-11-18T22:00:00Z","type":"heard","client_id":"iphone-1","payload":{"request_id":"request-1","text":"How is the refactor going?","spoken":true}}],"next_before":12,"has_more":true}
```

The page contains the newest matching retained replayable events strictly below `before`, returned in increasing sequence order. `next_before` is the oldest sequence in this page, or `null` when empty; stop when `has_more` is false. Exclude live-only events and status intended for other Clients. An empty final page has `events: []`, `next_before: null`, `has_more: false`. History does not run the Talker or synthesize speech.

## Shared exchange fixtures

The loopback protocol tests produce JSON event/command exchanges from this document for the Swift package to replay. Each fixture identifies `version: 1`, preserves exact envelopes and sequence numbers, and represents binary frames separately as bytes with matching headers. Tests cover reconnect while a reply streams, concurrent retries of one command/upload, Silence during a clip, interrupt before the first clip and after a completed clip, a revoked token, and held news with two Clients. A transcript fixture contains no real credentials, User recording or synthesized recording. Fixtures are generated from fake Hub sessions, transcriber and synthesis; they spend no model quota.

## Typed transport implementation

The typed listener uses the same Hub admission operation as the terminal. Accepted lines are logged before display delivery; replies store stable source, request and reply identities, final text and termination reason. Typed request ids do not claim the Mac's spoken Floor. Replay captures its subscription and high-water mark atomically; a durable pruning watermark distinguishes expired history from ordinary sequence gaps. `Door.on_replay` is the post-replay hook for the snapshot owner to append fresh canonical snapshots. Terminal EOF stops new admission, and remote sockets cannot extend the process lifetime.

Each socket has a bounded queue and one writer. A queue overflow or a write that takes five seconds detaches that transport; committed events remain available on reconnect. Registration ownership guards replacement cleanup. A Hub-lifetime watcher rechecks the pairing database every 250 milliseconds, including idle sockets; separate-process revocation closes them with 1008 within one polling interval plus scheduler/database time. Ping runs every 18 seconds and its Pong deadline is 9 seconds, leaving room for timer rounding within the 30-second idle unresponsive connection bound. Shutdown closes sockets and cancels pending command handlers, recording uncertain outcomes without repeating admission.

The single shared typed fixture is `ios/JarvisClient/Tests/JarvisClientTests/Fixtures/client-v1.json`. Regenerate it with `uv run --frozen --offline python -m tests.generate_protocol_fixtures`. `tests/test_protocol_stream.py` compares a fresh real loopback capture with that file. Only generated Client/request/reply/speech ids are normalized by a documented first-seen mapping; wire envelopes, sequences, per-socket event order and fixed fake timestamps are preserved. The Client identity mapping also applies to `focus_client_id`, so it matches the corresponding `client_id`. Swift replay tests must use this same file as a package resource when that integration is implemented. It currently covers typed partial replies, duplicate/conflicting commands, concurrent retry, reconnect during a reply, history/acknowledgement, socket replacement, idle revocation, restart uncertainty, complete Task and Approval snapshots, status changes, foreground presence and fresh snapshots after replay. The uploaded Talk exchange extends the same fixture with the canonical transcript, reply and identifier-only upload result. The speech exchange adds sentence headers followed by compact `binary_base64` descriptors for the actual fake PCM frames, playback reports and an audio-free reconnect replay. Swift feeds those frames through the same Client decoder and checks the PCM bytes.


## Uploaded Talk receipt lifetime

The launch owns one lazy transcription service for Mac capture and uploads. Remote `--text` uses that service without opening Mac audio devices or loading wake, VAD or turn-detection models. Shutdown closes the Door and its owned operations before closing the shared service. The transcription worker releases its completed job and recording references before waiting for another job.

A recording has a 330-second abandonment deadline. The first HTTP receipt narrows it to 30 seconds, including receipt and transcription; incomplete receipt retries and reconnects never extend that deadline. Disconnect immediately releases the recording floor and narrows any remaining deadline to 30 seconds. Explicit cancellation, expiry, revocation and shutdown prevent late transcription from admitting a turn. An admitted request keeps its inbox order even if another Client begins a Talk before it is dequeued. A retired Mac line is rejected before it can invalidate a current Talk.

An uploaded Talk retains its full transcript as one immutable segment at revision zero. The Mac's 6,000-character resumable-request limit does not truncate or reject a valid uploaded transcript. The scope policy survives Hub restart with its Task words and Approval bindings. Resolved scope history still keeps the latest 64 requests, and unresolved Mac scope holds remain retained.

Incomplete receipt stores no command reservation or audio. A complete validated body reserves its Talk, format and audio digest in the shared command namespace before transcription. One upload processes per Client, with at most ten new uploads per minute; HTTP 429 includes `Retry-After`. Exact retries wait for or return the original result without spending another allowance. A different body conflicts, and a different upload id cannot consume the same Talk again.

Durable Talk tombstones keep only identifiers and state. Lifetime command rows retain digests and safe result identifiers/status, never recording samples or transcripts. The canonical transcript belongs only to the conversation's 30-day event retention. A restart expires unfinished Talks and converts uncertain reserved commands to `outcome_unknown`; it never automatically executes them again. Completed exact-body retries remain available after restart. Clearing Python references releases recording ownership; it is not a guarantee of zeroed allocator or model memory.
