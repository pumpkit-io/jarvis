# Talker Memory: first-reply time and Memory quality

**Status: Not yet run: needs the User's go-ahead (spends Max usage).** The harness is committed (#201, spec #193) and skipped by default. Every table below is empty until that run.

ADR 0028 fixed three Memory sizes before any real-model evidence: a Fold starts at `JARVIS_MEMORY_FOLD_AT` (200,000 tokens), keeps a Tail of `JARVIS_MEMORY_TAIL` (40,000), and the ceiling restart fires at `JARVIS_MEMORY_CEILING` (300,000). Measured Talker latency so far stops at about 30,000 tokens of context; the largest Talker context in `.jarvis/metrics.jsonl` when the spec was written was 39,626 tokens. This run is meant to say whether the three numbers hold. If a result argues for other numbers, it is reported here with a recommendation, and the defaults stay until the User decides.

## What the run measures

The harness is `tests/test_real_memory_measurement.py`; its pure helpers (made-up Conversations, planted facts, tables, a copy of the `recall` handler) are in `tests/memory_measurement.py` and tested without a model in `tests/test_memory_measurement.py`. Every session uses the real Talker prompt, persona and tool schemas, or the real Summariser prompt, with the models in `.env` (the defaults are Sonnet at low effort for both).

1. **Fixed prefix.** One Talker turn on a seed with no exchanges gives the context the CLI reports for the system prompt, the tools and the seed's own text. The table gives its share of 40,000, 100,000 and 200,000 tokens. The Fold trigger counts that prefix today (ADR 0028), so this number says whether the trigger should subtract it.
2. **Chars-to-tokens estimate.** Two Talker seeds, one holding about 15,000 tokens of English exchanges and one of Italian, each against the empty seed. The difference is the tokens those exchanges cost, set against their characters and the Hub's starting estimate of 3.5 characters per token.
3. **First-reply time** at about 40,000, 100,000 and 200,000 tokens of Talker context, mixed English and Italian. For each size a new session receives a seed holding a made-up Conversation no earlier run sent. That seed turn is the **cold** row: the whole Conversation is read without the prompt cache, as when the cache has expired. Two typed lines follow at once, one English and one Italian; they are the **warm** rows, reading the prefix the seed cached. Each row gives time to the first text and to the end of the reply, from sending the message, and the input, cache-read, cache-write and output tokens.
4. **Summariser** on a made-up Conversation of about 100,000 tokens holding the four planted facts: duration, output tokens counted and estimated against the 5,000-token cap, whether the Hub's checks (`memory.problem`) pass, and which planted facts the summary kept. The summary itself is written out for judging by hand.
5. **Memory quality.** A made-up Conversation of four blocks, about 40,000 tokens each, four days apart, with one planted fact per block, is folded four times: each Fold gives the Summariser the previous summary and the next block, and a refused summary leaves its block for the next Fold, as in the Hub. A Talker is then seeded with the last summary and a 10,000-token Tail, and asked about the fact from the first block, three Folds back. `recall` answers from an event log holding the whole made-up Conversation at its own times. The result says whether the answer was right, and whether it came through the summary or `recall`.
6. **Usage-limit readings.** Any usage-limit window the CLI reports during a turn (`RateLimitEvent`: window, status, utilization) is recorded per turn. Comparing the readings around warm turns, which are mostly cache reads, with those around cold turns is the only cheap view of how Max counts cache reads. The CLI reports a window only when it changes, so this may come back empty.

## How to run it

Only with the User's go-ahead. From the checkout, with the Max login and `ANTHROPIC_API_KEY` unset:

```sh
JARVIS_REAL_MODEL_TESTS=1 uv run pytest tests/test_real_memory_measurement.py -s -v
```

Without `JARVIS_REAL_MODEL_TESTS=1` every test is skipped (`tests/conftest.py`), and the module refuses to run if that skip is ever bypassed. One part runs alone with `-k`: `-k fixed_prefix`, `-k first_reply`, `-k summariser_on`, `-k three_folds`. Results go to `.jarvis/measurements/talker-memory-<date-time>.md` (gitignored), one section per part, with the same tables as below; the path is printed at the end. Copy the tables here, with the date, the CLI version and the models.

The sizes above put the run at roughly 700,000 tokens written to or read past the cache, plus about as many read from it by the warm turns, and five summaries of up to about 6,000 tokens each. That is an estimate from the sizes, not a measurement.

## Results

Not yet run.

### Fixed prefix

| Model | Window | Context of an empty seed | Estimated from chars | Share of 40,000 | Share of 100,000 | Share of 200,000 |
| --- | --- | --- | --- | --- | --- | --- |

### Chars-to-tokens estimate

| Language | Chars | Tokens counted | Chars per token | Error of 3.5 chars per token |
| --- | --- | --- | --- | --- |

### First reply

| Size | Context reported | Cache | First text ms | Reply ms | Input | Cache read | Cache write | Output | Outcome |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

### Summariser

| Input (context reported) | Duration ms | Output tokens | Estimated tokens | Cap | Checks | Planted facts kept | Outcome |
| --- | --- | --- | --- | --- | --- | --- | --- |

### Memory quality

| Fold | Exchanges folded | Duration ms | Output tokens | Checks | Planted facts in the summary |
| --- | --- | --- | --- | --- | --- |

| Fact | Folds since | In the final summary | recall calls | recall found it | Answer right | Survived through |
| --- | --- | --- | --- | --- | --- | --- |

### Usage-limit readings

| Turn | Window | Status | Utilization |
| --- | --- | --- | --- |

## What stays unmeasured

- **Cache hit rate over a week of real use.** It comes from the existing metrics, not from this run: `./jarvis stats --since 7d` gives a `cache hit` column per role: cache reads over input, cache reads and cache writes together.
- **First audio.** Times stop at the first text from the CLI; the speech pipeline adds its own time after it, measured separately in `controlled-voice-latency.md`.
- **A cache that expired while Jarvis sat idle.** The cold rows stand in for it; no run waits for the cache to expire.
- **Spread.** Each size has one cold sample and two warm ones, so a single slow request moves a row.
- **Real Fold sizes in the quality check.** Each Fold there folds about 40,000 tokens, not the 160,000 a default Fold folds (200,000 less the 40,000 Tail), to spend less. The Summariser timing at 100,000 tokens is the nearer guide to a real Fold.
- **The User's own talk.** The Conversations are made-up English and Italian exchanges about repos and tasks; one fact is asked about, and right means the answer holds the planted word.
- **How Max counts cache reads**, if the CLI reports no usage-limit window during the run.
