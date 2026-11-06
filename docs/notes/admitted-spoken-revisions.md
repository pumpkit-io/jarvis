# Preserve Task authority across admitted spoken revisions

#57 completes the mechanical contracts for revising speech that has already passed the local admission seam. #38 still requires a working production admission path and its original natural-continuation behavior. At the #57 checkpoint, no production recognizer was selected or wired; its evidence uses fake recognition decisions.

Before this change, the Hub had no durable spoken scope connecting a revised request to its existing Tasks and their producer authority. For example, after “Open an issue” started T1, a later admitted “about the scheduler” needed to preserve both the opening and T1's identity. The new contract keeps an ordered source history and increasing revision, then holds T1's authority while the Talker explicitly resolves what changes. A local relation label alone cannot rewrite T1, discard earlier words or grant an Approval.

## Source contracts

- [SpokenTurn](../../src/jarvis_hub/voice/ownership.py) retains request identity, revision, raw segments and the current semantic boundary. Capture epochs and candidate sequences in [the input pipeline](../../src/jarvis_hub/voice/input.py) distinguish ownership from speech-end timing and pre-roll. Logical onset survives delayed delivery and reply startup; obsolete results and reply callbacks cannot acquire the new revision's floor.
- [VoiceMode](../../src/jarvis_hub/voice/voice_mode.py) uses the injected contextual assessment seam before handing an accepted revision to the Hub. Unavailable or uncertain assessment preserves the existing generation. Confirmed unrelated fragments are discarded; uncertain fragments can be reassessed with later observed speech. The default with no recognizer is uncertain.
- [The Hub](../../src/jarvis_hub/hub.py) synchronously persists admitted raw history and a pending [Spoken scope](../../src/jarvis_hub/spoken.py) before awaiting obsolete-generation interruption. Owned Task requests, revisions and exact Approval targets stay unchanged until resolution.
- The Talker's `resolve_scope` names the exact scope, revision and attempt. It chooses the complete affected Task set, `preserve_tasks`, or a separate request at an observed raw segment boundary. Selected Task intent, producer revision, obsolete Approval withdrawal and cancellation commit in one synchronous save batch. Unselected Tasks retain their intent and Approval targets.
- Task tools, Subagent SDK hooks, waits, news and Approval dispatch recheck producer authority after asynchronous waits. Existing synchronous permission and Approval checks remain, including `strict_mcp_config` and blocking hook failures. A later revision cannot undo an irreversible action already dispatched.
- Restart restores pending scope, typed clarification and source revisions before held Task recovery. Invalid saved scope is rejected without committing a partially restored Hub. Missing, stale, invalid or timed-out resolution leaves authority held and gives a clarification notice.

The retained microphone context is bounded to four waiting, retained or in-flight fragments and a 6,000-character spoken request. Recent resolved scope history keeps 64 entries; unresolved holds remain durable. The scope-resolution deadline is 30 seconds. Duration or retention limits are explicit failures, and abandoned tails cannot become new requests. Typed clarification remains a distinct source and typed voice-mode replies stay silent. [ADR 0016](../adr/0016-revise-spoken-requests-before-replacing-their-generation.md) records the full ownership boundary.

## Verification

At source checkpoint `7d7a4f5d88c6da1b5c561a84e1fff9cf5dd6e7cc`, after merging `origin/main` at `0534961ee3fca8d0f1b3debc0cc3ab1ae9a3ec2c`, the full checks passed using initialized dependencies:

```sh
uv run --no-sync pytest -q
uv run --no-sync pyright
```

The suite reported **1,623 passed, 18 skipped**; Pyright reported **0 errors, 0 warnings**. This documentation-only scope clarification preserves that source tree. No physical device, conversational model or real GitHub test was invoked.

The existing injected seams establish these behaviors:

- [VoiceMode/Hub tests](../../tests/test_voice_mode.py) preserve EN/IT fragments and one Task identity, reject late owners across first-audio races, keep typed input silent, preserve independent work and retain quiet-time Announcement scheduling.
- [Spoken scope tests](../../tests/test_spoken_scope.py) exercise explicit Task selection, raw/typed context, unresolved holds, restart before old work, corrupt-state atomic recovery and Approval authority across target reads and expiry.
- [SDK-hook tests](../../tests/test_subagent_rules.py) show that source authority waits precede the existing permission guard and cannot bypass an Approval that expires during the wait.
- [Input and worker tests](../../tests/test_input_pipeline.py) retain capture identity and onset; [assessment tests](../../tests/test_assessment.py) exercise bounded readiness, timeouts, stale results and unavailable recognition without freezing the Hub.

These checks establish the ownership mechanism after scripted local admission. Semantic model selection, production continuation admission, actual worker/model coexistence, physical audio/STT behavior and the #39/#40 admission/interruption gates remain unverified. Completing #57 does not close #38 or #26, and authorizes no cloud processing of background speech.

Later #38 work adds a disabled-by-default [cloud admission seam](cloud-spoken-admission.md) under [ADR 0017](../adr/0017-opt-in-cloud-spoken-admission.md). The #57 checks here do not certify that adapter's semantic accuracy, availability or physical behavior.
