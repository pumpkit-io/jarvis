# Subagents read the web through Claude in Chrome

A Subagent could not use the browser. On 2026-11-16 Subagent S2 found why: Claude Code 2.1.325 connects the Claude in Chrome extension to a headless session only when it starts with `--chrome`, and the Hub never passed it. Even with the flag, the connector check of ADR 0008 refused every `mcp__claude-in-chrome__*` tool. S2 got there through a separate `claude -p --chrome --tools "" --permission-mode default` session limited to navigation and page reads, which opened one tab, read the Stripe dashboard and closed the tab. The User decided Subagents should have the same reach.

So a Subagent session starts with `--chrome`, passed as `extra_args={"chrome": None}` in `_skipping_permissions` in `src/jarvis_hub/agents/sdk.py`, and only when the session has an MCP guard, so the browser never joins a session the guard does not watch. `CHROME_READS` in `src/jarvis_hub/subagent_rules.py` adds the tools S2 used to `READ_CONNECTOR_TOOLS`: `list_connected_browsers`, `tabs_context_mcp`, `tabs_create_mcp`, `tabs_close_mcp`, `navigate`, `get_page_text`, `read_page` and `find`. Every other browser tool stays refused by ADR 0008's check, because each one acts in the User's signed-in Chrome or reads more than the page: `computer` (clicks, typing, keys, scrolling, screenshots), `form_input`, `javascript_tool`, `file_upload`, `upload_image`, `browser_batch`, `gif_creator`, `read_network_requests`, `read_console_messages`, `resize_window`, `select_browser`, `switch_browser`, and any tool the extension adds later. `prompts/subagent.md` tells a Subagent to read in a tab it opens and closes, since the check sees only the tool's name and cannot stop `navigate` or `tabs_close_mcp` on one of the User's own tabs.

No Approval grant opens an interactive browser tool. ADR 0013's grants are a closed list (`x_post`, `posthog_config`) and each Approval opens its tools for one call, while acting on a page takes many clicks. A browser grant would change the Orchestrator's `report` tool schema and needs its own design, so until then a Subagent that needs to click or type reports what it would do, and the User does it.

## Considered Options

- Leave every browser tool behind an Approval, navigation included. Rejected: no grant could open them, so the browser would stay unusable, and the risk it would guard against is mostly already open (below).
- Allow every browser tool. Rejected: `computer`, `form_input` and `javascript_tool` act in the User's signed-in accounts under `bypassPermissions`, the case ADR 0008 exists for.
- Start the browser with `--chrome` only in Subagents whose brief asks for it. Rejected for now: the Orchestrator would need a new field for it, and the check already limits what the flag adds.

## Consequences

- Navigation is a GET request with the User's cookies. Most pages only show something, but a link can act on a GET (an unsubscribe or confirmation link), and a page can carry instructions aimed at the model. A Subagent that read a signed-in page could send what it read elsewhere through `navigate`; Bash with `curl` already allows the same, and only the Boundary stops either (ADR 0001).
- The browser works only on a host where Chrome is running with the Claude in Chrome extension installed and enabled and its native messaging host registered, as S2 found on the User's Mac. How a session behaves without them was not tried. A session started without `--chrome`, such as the Talker or the Orchestrator, never connects the extension.
- A new read tool from the extension is unavailable until someone adds it to `CHROME_READS` with a table case in `tests/test_subagent_rules.py`.
