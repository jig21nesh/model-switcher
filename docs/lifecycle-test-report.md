# Session-lifecycle test report

Date: 2026-07-10. Method: four simulated user sessions, each driving the **real** `hooks/complexity_router.py` and `statusline/cost_statusline.py` binaries exactly as Claude Code invokes them (JSON on stdin), in isolated sandboxes via `MODEL_SWITCHER_HOME`. No live `~/.claude` configuration was read or written; the repo was not modified during the runs. Every invocation across all scenarios exited 0, and the statusline printed exactly one line every time.

Findings from this campaign led to one code fix (invalid project-override values now fall open to the *global* config per ADR-0003, not to hardcoded defaults) and the README notes on override value types, exact-directory override lookup, and the 10 KB scoring cutoff.

## 1. Session start

| # | Scenario | Expected | Observed | Result |
|---|---|---|---|---|
| 1 | Fresh install, `config.json` missing, first prompt | Models nag + pricing pointer, once | Both nags in one `additionalContext`; state file written | PASS |
| 2 | Second prompt, same session | No nag repeat | Silent | PASS |
| 3 | Models set, pricing all `null` | Pricing nag only, once | Pricing nag turn 1; silent turn 2 | PASS |
| 4 | Fully configured: simple prompt / complex prompt | Silence / directive naming `heavy-task-fable` | Silence / score 9/10 directive | PASS |
| 5 | `/help` first, then real prompt | Nag preserved for the real prompt | Slash turn writes no state; nag fires on the real prompt | PASS |
| 6 | Whitespace/empty/garbage/non-object stdin; hostile prompt + path-traversal session ID | Exit 0, no crash, no traversal | All variants exit 0; invalid session ID → no state file anywhere; no injected commands executed | PASS |
| 7 | Statusline: no/nonexistent/empty transcript; no pricing | Always one line | Pricing warning, `cost n/a: no usage data`, or `(builtin est.)` fallback as appropriate | PASS |
| 8 | Statusline first-turn cost math (fable rates) | Matches hand calculation | `$0.0352` — exact match (incl. IEEE-754 rounding at the half-cent boundary) | PASS |
| 9 | Project override disables routing while models unconfigured | Total silence, no state write | Silent; confirms the routing gate runs before nag/state logic | PASS |
| 10 | Corrupted (truncated) `config.json` | Treated as missing config | Full nags fire, exit 0 | PASS |

## 2. During a session

One fully-configured sandbox, a realistic 12-turn conversation under one session ID, plus statusline evolution across five turns.

| # | Scenario | Expected | Observed | Result |
|---|---|---|---|---|
| 1–2 | Simple lookups and follow-ups | Silence | Score 0/10, silent | PASS |
| 3 | Multi-step refactor ask | Directive | Score 9/10, names `heavy-task-fable` | PASS |
| 4–5 | "yes go ahead" / short steering reply | Silence | Score 0/10 (affirmation cap / no signal) | PASS |
| 6 | Pasted traceback + "fix this" | Directive | Score 5/10 — exactly at threshold, fired (confirms `>=` comparison) | PASS |
| 7 | >10 KB paste | Fast, capped, exit 0 | ~0.06 s, same as short prompts | PASS |
| 8 | Hostile prompt (shell metachars, fake JSON) scoring complex | Valid JSON directive, prompt inert | Score 10/10, valid JSON; no exec paths exist in the router (grep-verified) | PASS |
| 9 | Complex prompt with `agent_id` set (subagent context) | Silence | Silent; control without `agent_id` fires | PASS |
| 10 | Command-tag-wrapped prompt | Silence | Silent | PASS |
| 11 | "don't refactor anything, just tell me…" | Silence | Negation window suppresses the strong keyword | PASS |
| 12 | Project override (`enabled:false`) on a score-9 prompt | Silence | Silent; identical prompt fires without the override | PASS |
| 13–14 | Unconfigured install: nags once, then silence | Once per session | Both nags turn 1, silent turn 2 | PASS |
| 15 | Malformed stdin (hook + statusline) | Exit 0; statusline still one line | All 6 variants clean | PASS |
| 16–20 | Statusline growth: sidechain usage, streamed-duplicate message ID, unpriced model, turn vs session | Sidechains counted; dupes deduped; `no rate:` flag; turn cost never cumulative | All verified; turn-2 math hand-checked to the cent ($0.0620 turn / $0.0680 session) | PASS |

## 3. Resume / restart / new session

| # | Scenario | Expected | Observed | Result |
|---|---|---|---|---|
| 1 | Resume same session ID after days | No nag re-fire; routing still works | State persisted; directive only | PASS |
| 2 | Genuinely new session ID | Nags fire once again | Independent state per session | PASS |
| 3 | 8-day-old state files | Session-named cleaned; foreign-named preserved | Exactly that | PASS |
| 4 | State file hand-corrupted | No crash; nag may re-fire; routing works | Nag re-fired, directive fired, state self-healed on next write | PASS |
| 5 | Statusline across restart with re-streamed duplicate | No double-count; total = old + genuinely new | $0.0630 → $0.0630 (dupe) → $0.1050 (two new turns) | PASS |
| 6 | `routing.enabled` flipped false→true mid-session | Immediate effect, nag flags survive | Next prompt reflects each flip; no re-nag | PASS |
| 7 | Two interleaved sessions | No cross-contamination | Each nags once; separate state files | PASS |
| 8 | Weird session IDs (64-char boundary, 65-char, traversal, dots, empty, missing) | Valid persists; invalid never writes files, never crashes | Confirmed; no stray files anywhere | PASS |
| 9 | `state/` directory deleted mid-session | No crash; nags re-fire; directory rebuilt | Exactly that | PASS |

## 4. Routing switch and per-project override (v0.2.0)

| # | Scenario | Expected | Observed | Result |
|---|---|---|---|---|
| 1 | Global disabled: complex prompt; also with models unconfigured | Total silence, no nags | Silent both ways | PASS |
| 2 | Global disabled + project A `enabled:true`; project B no override | A fires, B silent | Exactly that | PASS |
| 3 | Global enabled + project `enabled:false` | Silent in that project only | Exactly that | PASS |
| 4 | Project thresholds 9 and 1 vs global 5 | Score-6 prompt: global yes / project-9 no; mild prompt delegates only under project-1 | Exactly that | PASS |
| 5 | Override file edited mid-session | Next prompt reflects it; nag state unharmed | Exactly that | PASS |
| 6 | Hostile overrides: malformed JSON, model-injection attempt, >64 KB, directory-as-file, unicode/space cwd, cwd `/`/empty/missing | All fall open to global, exit 0, no injected text, no content logged | All confirmed; oversized warning logs path only | PASS |
| 7 | Statusline with routing disabled | Cost unaffected | Normal cost line | PASS |
| 8 | User-expectation probes: `models` in override; string `"true"`/`"false"` booleans; parent-directory override | Documented behavior | See findings below | PASS (as coded) |

## Findings and resolutions

| Finding | Severity | Resolution |
|---|---|---|
| Invalid-typed override `threshold` fell back to the hardcoded default (5) instead of the global value — contradicting ADR-0003's fall-open-to-global guarantee | Bug (ADR mismatch) | **Fixed**: `merge_project_config` now type-checks override values and drops invalid ones, so the global value governs; regression tests added |
| String `"enabled": "false"` in an override forced routing **on** (fail-open-to-enabled), the opposite of the typo'd intent | UX gap | **Fixed** by the same change (invalid values keep the global setting) + README note on JSON value types |
| No parent-directory search for the override file — a repo-root override is ignored for sessions started in a subdirectory | Doc gap | README note added ("read from the session's working directory exactly") |
| Only the first 10 KB of a prompt is scored — a complex ask pasted after a big log dump can route as simple | Doc gap | README note added ("put your request before a large paste") |
| Corrupted global `config.json` fails silently (no stderr), unlike the project-override path | Minor | Open — candidate for a one-line warning for parity |
| A well-formed `models` key in a project override is ignored with no feedback (by design, for security) | Minor | Open — per-repo heavy models are unsupported; consider a warning or a documented "not supported" line |
| Session IDs outside `[A-Za-z0-9-]{1,64}` never persist nag state, so such a session would nag on every prompt | Assumption | Open — verify real Claude Code session IDs always fit this shape |
