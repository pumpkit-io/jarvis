# Design direction

## Direction

Jarvis is a quiet, script-led iPhone companion to one conversation. Pairing leads into Conversation; Tasks and Settings sit behind text navigation. Replies use a serif script, while the User's lines, controls and status use smaller system type. Details expand in place. The Talk key is the main coloured control. The bottom Talk and Voice controls share their owners across Conversation and Tasks; typing hides the keys while preserving the composer.

## Palette

Dark mode uses Baize `#16302B` as the background and Linen `#E9EEEA` for primary text. Secondary text and outlines use Slate `#8FA39C`; the Talk key uses Brass `#C39A4A`, with Baize text.

Light mode reverses Baize and Linen. Secondary text and outlines darken to `#536D64`; the Talk key darkens to `#806020`, with Linen text. Active recording uses Signal `#D6402E` with white text. Disabled Talk reduces opacity to 0.55. The active spoken sentence uses bold serif type and a single underline.

## Type

The script uses the system serif at a base size of 19 points, scaled relative to body. Controls, status and User lines use system type at 15 points, scaled relative to subheadline. Script line spacing is 5 points; User lines use 2 points. The recording timer uses monospaced digits and semibold weight. Labels wrap rather than imposing a fixed text height.

## Spacing

Screens use 20-point outer padding. Conversation rows and major Pairing/Settings groups use 32-point spacing. Smaller groups use 8, 12, 16 or 24 points according to their content. Conversation text has a 640-point maximum width; Settings has a 520-point maximum width.

Small actions have a 44-point minimum touch height. The Talk key has an 88-point minimum content height, 16-point horizontal and 12-point vertical padding, and a 10-point corner radius. Voice uses a 10-point outlined shape. The composer and Pairing fields use 8-point corners. Controls sit above the bottom safe area. When the scaled control size exceeds 20 points, Talk and Voice stack vertically.

## Motion

There are no custom animation durations or easing tokens. Conversation follows new text while the reader is near the bottom, preserves older reading position and uses native scrolling, disclosure and navigation. Recording changes the key colour, label and elapsed timer. Playback highlights the current sentence. Connection, pending and failure states use visible text rather than decorative motion.

## Voice

UI copy is direct and specific: “Type instead”, “Send updates here”, “Voice off” and “No open Tasks. Ask Jarvis for something.” Empty screens invite one useful action. Failure and uncertainty copy explains the state and recovery without claiming success: “Couldn't reach the Mac. Not changed.” Destructive unpairing asks for confirmation and explains that the saved pairing and conversation will be removed from this iPhone.
