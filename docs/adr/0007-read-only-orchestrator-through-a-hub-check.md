# The Orchestrator's shell runs only through a Hub check

ADR 0002 made the Orchestrator read-only, but it needs `gh` and `git` to look things up, and a Claude Code allow rule such as `Bash(gh *)` would also allow `gh pr merge`. Prefix rules cannot tell `gh api repos/x` from `gh api -X DELETE repos/x` either. So the Orchestrator runs under `dontAsk` with Read, Grep and Glob on the allowlist and Bash left off it, and a `PreToolUse` hook on Bash decides every call through the Hub's `check` in `src/jarvis_hub/read_only.py`.

`check` parses the command into words, validates the words, and the hook runs those words and nothing else:

1. **Parse.** The command is split the way bash splits plain words, single quotes and double quotes. Anything else bash would read as syntax is refused, not interpreted: unquoted operators, substitution, globs, comments and escapes (`UNQUOTED_SYNTAX`), `$`, backtick and backslash inside double quotes, an unclosed quote, and any line break, control or invisible character.
2. **Validate.** The first word is `gh` or `git` and the rest is a read on the lists in `read_only.py` (`GH_READ`, `GIT_READ` and the git listings beside them). Those lists are the only statement of what runs: the Orchestrator's prompt is generated from them by `describe_reads`, and a new look-up means adding to them with a table test in `tests/test_read_only.py`.
3. **Run the words.** On an allow, the hook returns `updatedInput` with the command rebuilt by `shlex.join`, every word quoted. Bash then reads exactly the validated words, so a disagreement between the Hub's parser and bash cannot add a command. An earlier version validated with `shlex` and let bash run the original string; `shlex` treats `#` as a comment anywhere and bash only at a word start, so `git log a#;touch /tmp/pwn` passed and bash ran `touch`.

Rules on the words, each with table cases:

- `gh api` and `gh auth` are refused: `gh api` flags turn a read into a write in too many spellings, and `gh auth status --show-token` and `gh auth token` print the GitHub token into the model's context.
- `gh` short flags go one per word. pflag reads `-cw` as `-c -w` and `-Rx` as `-R x`, so a bundle could hide `--web`, which opens the User's browser. `--watch` is refused too: waiting is the Hub's job (ADR 0003).
- `git` takes no global option but `-C <repo>`, which rules out `-c`, `--exec-path`, `--git-dir` and `-p`. `--output`, `--ext-diff` and `--textconv` are refused with every abbreviation git accepts for them. The Hub also adds `--no-optional-locks` and `-c core.fsmonitor=false` to every git call, and `--no-ext-diff --no-textconv` to `diff`, `log`, `show` and `stash list` (`--no-textconv` to `blame`), so a diff or textconv program in a repo's config never runs.

## Considered Options

- Allow rules per read-only subcommand (`Bash(gh pr list:*)` and so on). Rejected: a prefix cannot see a flag that turns a read into a write, and `git -C <repo> log` matches no fixed prefix.
- `can_use_tool`. Rejected: under `dontAsk` it is never called, and the other modes would prompt.
- Validate the string and let bash run it as written. Rejected after the `#` bypass: safety then depends on two parsers agreeing on every input.

## Consequences

- The check fails closed. Without the hook, or if it fails (`on_failure="block"`), every Bash call is denied.
- The model's command can differ from what runs (extra spaces gone, quoting changed, git switches added). The opt-in real-model test shows the CLI runs the rewritten command: in a repo whose config sets `diff.external`, the Orchestrator's `git diff` printed the plain diff and the program never ran.
- git still reads each clone's own config, so a program configured there that git offers no switch for (a clean filter on `git status`, say) could run. Those configs are local to the User's clones, written only by the User or a Subagent.
- Quoting is narrower than bash: a `--jq` filter or a search with `|`, `$` or `#` goes in single quotes, and the refusal says so.
