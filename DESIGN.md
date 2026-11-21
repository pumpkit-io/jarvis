# Design direction

## Direction

Jarvis on iPhone is a dark, orb-led companion to one conversation. The main screen stacks a top bar, the orb, a preview of the conversation and a messaging-style input bar. The orb is the one bold element: a sphere of points that moves with whoever is speaking. Everything around it stays flat and quiet, with no gradients, card shadows or glass effects. The one exception is a soft blur behind the top bar when the expanded conversation scrolls under it.

The top bar is the same in both modes. Settings sits on the left as an icon that opens a full page. The connection dot sits in the centre, with a short label beside it whenever it is not green; tapping it shows the full status. Tasks sits on the right as an icon carrying the count of open Tasks.

The conversation expands by dragging the preview up or tapping the control at its top edge. Expanded, the orb goes away and the conversation fills the space between the top bar and the input bar; dragging down or tapping the control collapses it. The app always opens in orb mode. Jarvis's replies run full width with no bubble. The User's lines sit in rounded bubbles at the trailing edge. Details, Play, the spoken-sentence underline and the line recovery actions keep their behaviour.

The input bar works like a messaging app. The field reads "Message Jarvis". With text in the field, the round button beside it sends; empty, it is the mic. Holding the mic records a Talk, and releasing sends it. Sliding up locks the Talk, after which the bar shows a discard button, the timer and a live waveform, and a send button. Sliding left discards it. A press shorter than 0.3 seconds records nothing and shows "Hold to talk." A Talk waiting to be heard shows as the User's bubble with a waveform and its length; once heard it shows the User's words.

The sound button is a round 44-point button at the lower right of the orb, shown only in orb mode. It switches Silence for this iPhone: a speaker icon when Jarvis speaks aloud, a crossed-out speaker when Jarvis only writes. Voice is on by default and the choice survives relaunch.

Pairing, Settings and Tasks use the same palette and type with their existing layouts. There is no light theme.

## Palette

| Name | Hex | Use |
|---|---|---|
| Void | `#05070C` | Background on every screen, behind the orb |
| Deep | `#0E1420` | Input bar, the User's bubbles, sheets |
| Frost | `#E6EEF5` | Primary text |
| Haze | `#7F8EA3` | Secondary text, icons, outlines, placeholders |
| Cyan | `#33E6FF` | Jarvis's orb, the mic and send button, the held mic |
| Ice | `#D9EDFF` | The orb while the User speaks |
| Idle blue | `#5AB4FF` | The orb when nobody speaks |
| Signal | `#D6402E` | Recording dot and discard, the red connection state |

The connection dot is green `#3DDC84` when Jarvis is ready, amber `#F5B544` while connecting or warming up, and Signal when the Mac is unreachable or the Usage limit is reached.

## Type

Jarvis's replies use the system serif at 18 points, scaled relative to body, with 5-point line spacing. The User's lines, controls, status and labels use the system sans at 15 to 16 points, scaled relative to subheadline. Timers use monospaced digits and semibold weight. Labels wrap rather than imposing a fixed text height.

## Spacing

On a 393 × 852-point iPhone the top bar is 44 points, the orb area takes about 40 percent of the height with the orb near 250 points, and the input bar is about 56 points above the bottom safe area. The conversation preview fills the rest, its top edge fading into the orb. When the keyboard opens in orb mode, the orb shrinks to fit, down to about 120 points, keeping at least three lines of conversation visible; when even that does not fit, the screen switches to expanded until the keyboard closes.

Screens use 20-point outer padding. Conversation text has a 640-point maximum width; Settings has a 520-point maximum width. Small actions have a 44-point minimum touch target. The input field and the User's bubbles use fully rounded or large-radius shapes; the mic and send button is a circle.

## Motion

The orb follows the reference in `PointOrbView.swift` and the point-orb preview: 2,500 points on a Fibonacci sphere, dot size 0.011 of the radius, slow rotation, and its level smoothed as `level = level × 0.8 + target × 0.2`. It has three states:

- Idle, in Idle blue, whenever nobody speaks, including while a sent message waits for a reply.
- The User, in Ice, driven by the microphone level during a Talk.
- Jarvis, in Cyan, driven by the level of the audio playing. With Silence on, a reply moves the orb with the preview's simulated syllable rhythm from its first streamed text to its end.

The held mic grows to about 80 points in Cyan with a light haptic, and a lock capsule rises above it. Locking (about 60 points up) and discarding (about 120 points left) each give a haptic; discarding animates the trash. These thresholds are starting values to tune on the device. Expanding and collapsing the conversation animate the orb away and back. Conversation follows new text while the reader is near the bottom and preserves older reading position.

## Voice

UI copy is direct and specific: "Message Jarvis", "Hold to talk.", "‹ Slide to cancel", "Connecting", "Mac unreachable". An empty conversation shows "Ask Jarvis anything." in Haze under the idle orb. The Usage limit shows its reset time beside the red dot ("Limit · resets 14:30"). Offline, typing and Talk still work: queued messages show a small clock until heard. Failure and uncertainty copy explains the state and recovery without claiming success: "Couldn't reach the Mac. Not changed." Destructive unpairing asks for confirmation and explains that the saved pairing and conversation will be removed from this iPhone.
