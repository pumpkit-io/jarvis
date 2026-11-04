# Quiet mode's news opens the User's next turn

Spec story 56 asks Quiet mode to hold Announcements until the next Wake word, which then opens with "N updates while you were away" and the details on screen. Story 57 lets news that needs the User through Quiet mode, once per change. Both build on ADR 0010's queue.

The Talker's `quiet_mode(on)` tool turns it on, and the next Wake word ends it, as the story says ("until my next Wake word"). `quiet_mode(on=false)` also ends it, for a typed line or a change of mind in the same exchange. News already waiting for the reply to end when Quiet mode starts waits for the Wake word too.

When the Wake word ends Quiet mode, the Hub puts what it held first in the news as one item: a `<event source="hub" away="N">` line with the exact count, then each held event. If the User speaks after that Wake word, the item goes to the Talker in the same message as their turn, and the Talker opens its reply with the count, then answers. If they say nothing, the item is an ordinary Announcement once Jarvis is idle. Either way the Hub shows the list of held news and their details itself, so the screen never depends on the model.

News with `needs_user="true"` gets through Quiet mode when it differs from the last `needs_user` news on its Task: a different spoken sentence (ignoring case and spacing), or the first since the Task reported news that didn't need the User. The same news again waits with the rest and counts among the updates. Outside Quiet mode every report is announced as before; the Hub still records it, so a repeat in Quiet mode later stays held.

News that queued while the User had the floor goes out as one Announcement: one chime, one Talker turn with every event in arrival order, one Follow-up window. Each Announcement from idle starts with the chime, and the Wake word is ignored during it, as during the Wake word's own chime.

## Considered Options

- Say the count as a Hub line right after the Wake word's chime. Rejected: the User has just woken Jarvis to speak, and the line would talk over their request, the problem ADR 0010 removed. A count spoken by the Hub would also need a line synthesised at that moment rather than a cached clip.
- Announce each queued item on its own. Rejected: three items meant three chimes and three Follow-up windows, 12 s of Jarvis taking the floor for what one sentence can say.
- Let the Talker count the held news. Rejected: the count must be exact, so the Hub writes it and the Talker repeats it.

## Consequences

- The count reaches the User in the Talker's voice, so it depends on the Talker saying the number given; the prompt asks for it exactly.
- A spoken turn that is a stop phrase sends nothing to the Talker, so after "that's all" the held news is announced once Jarvis is idle.
- Text mode has no Announcements (ADR 0010), so `quiet_mode` there changes nothing and says so.
- Quiet mode, the news it held and the per-Task `needs_user` record are saved in the state file (ADR 0011), so a restart neither drops held news nor announces a repeat it would have held. Held news the User has not heard yet, including news in a Talker turn the restart cut off, comes back held until the next Wake word, or waiting to be announced if the Wake word had already ended Quiet mode; news cut off mid-turn may be heard twice.
- The Announcement that the Usage limit has reset is ordinary news: batched with the rest and held in Quiet mode. The one saying the limit was reached gets through Quiet mode like news that needs the User, since until the reset nothing the User asks can be done.
- News a recovery turn reports follows the same rules as any other: held in Quiet mode, otherwise an Announcement once Jarvis is idle.
