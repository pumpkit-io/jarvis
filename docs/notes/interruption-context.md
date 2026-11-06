# Interruption context and physical hearing

The Hub tags an interrupted reply prefix with `basis="queue_estimate"`. The Talker receives that prefix as context and must not treat it as proof that the User heard those exact words. This metadata grants no Task or Approval authority.

SoundPlayer tracks output samples submitted through its callback. Within a partly played clip, the text prefix is proportional to its sample progress; it has no word-level alignment. Device buffering, fading and the actual room/device path further separate callback progress from physical hearing. Completed fake-player clips can establish deterministic scheduling behavior, but cannot validate real word boundaries or DAC output.

A delayed-correction Hub regression plays one fake clip while another remains queued, then checks that the interruption context contains only the played prefix, explicitly labeled as an estimate. The source policy no longer tells the Talker that this is all the User heard. Physical timing and word attribution remain unmeasured and require the targeted human session. This change does not claim that #41 or Spec #26 has passed those gates.

A keyword whose source timestamp predates a newer typed request cannot reopen listening. The Hub regression delivers that late detection while the typed reply is pending, then verifies that a genuinely fresh wake still works. Natural listening expiry remains distinct: a delayed wake already captured before that expiry can retain the existing recovery behavior.
