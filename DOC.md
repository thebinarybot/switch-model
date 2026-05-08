# switch-model — High-Level Overview

A Claude Code plugin that automatically picks the cheapest Claude model capable of handling each prompt, then routes to it. Goal: cut Claude API spend without giving up quality on hard tasks.

## The problem

Claude has three model tiers with very different prices (per 1M tokens, in/out USD):

| Tier   | Input | Output | Use case             |
|--------|-------|--------|----------------------|
| Haiku  | $1    | $5     | Trivial tasks        |
| Sonnet | $3    | $15    | Standard code work   |
| Opus   | $15   | $75    | Deep reasoning       |

Most developers leave Claude Code on Sonnet or Opus all day. Result:
- Trivial prompts ("rename this file", "list 3 colors") run on the same expensive model as architectural design questions.
- A single Opus session can spend $5–$15/hour even when 80% of prompts are mechanical.
- Manually switching models with `/model` per prompt is friction nobody actually does.

## What this plugin does

For every user prompt:

1. **Classifies** the prompt's complexity (haiku / sonnet / opus tier) using a hybrid of regex heuristics and a tiny LLM tie-breaker.
2. **Compares** the suggested tier against the model the session is currently running.
3. If they match, the prompt passes through normally — zero overhead.
4. If they differ, the plugin **silently runs the prompt on the suggested (usually cheaper) model** in a background process and returns the answer to the user, while the main session model stays unchanged.
5. Shows the user the answer along with how much was saved.

The user types one prompt and gets one answer. The savings happen behind the scenes.

## Example

User in an Opus session types:

> list 3 prime numbers

Plugin's response:

```
[switch-model: routed to haiku · saves ~93% vs opus]

2, 3, 5.

(answered by subprocess. main session still on opus. prefix +force to bypass router.)
```

The Opus model never saw the prompt. The Haiku subprocess answered for ~$0.0001 instead of ~$0.005. Same answer, 50× cheaper.

## Features (current)

| Feature                     | What it does                                                       |
|-----------------------------|--------------------------------------------------------------------|
| Heuristic classifier        | Regex rules map prompt keywords to tiers                           |
| Hybrid LLM fallback         | Ambiguous prompts escalated to a Haiku tier-picker                 |
| Context awareness           | Reads recent transcript activity (errors, edits, tool use, model streak) and adjusts tier accordingly |
| Effort matching             | Picks `--effort` level (low/medium/high/xhigh/max) per prompt complexity, capped per tier |
| Subprocess routing          | Routed prompts run via `claude -p --model X`, answer returned inline |
| Savings estimate            | Block-reason header shows estimated % saved vs current model       |
| `+force` / `+keep` bypass   | Prefix prompt with `+force` to skip the router for this turn       |
| Recursion guard             | Subprocess sets env var so router doesn't fire on itself           |
| Statusline badge (optional) | Shows current model + last routing decision                        |
| Heuristics-only mode        | `SWITCH_MODEL_NO_LLM=1` skips the LLM tie-breaker                   |
| No-context mode             | `SWITCH_MODEL_NO_CONTEXT=1` skips transcript signal analysis        |
| No-effort mode              | `SWITCH_MODEL_NO_EFFORT=1` skips effort selection                   |
| Debug trace                 | `SWITCH_MODEL_DEBUG=1` logs decisions to hook stderr                |

## How effort matching helps

A trivial question on Opus at `max` effort burns ~10× more reasoning tokens than at `low` effort. Without effort matching, Opus spends max-effort tokens on every prompt — even ones that don't need it.

The plugin pairs every routed call with an effort level:
- `rename foo` on Haiku → `low` (no benefit from more reasoning on a small model)
- `implement auth` on Sonnet → `medium` (default for code work)
- `audit auth code for security` on Opus → `high` (deserves thorough review)
- `deep dive into the consensus algorithm tradeoffs` on Opus → `xhigh` (research-grade)

Per-tier effort ceilings prevent waste: Haiku capped at `low`, Sonnet at `high`, Opus at `max`.

## How context awareness helps

Without context, the classifier sees only the current prompt. A session deep into debugging an auth bug might submit "fix this" — three trivial words that look like a haiku-tier prompt. With context, the plugin notices the last 10 turns had 4 tool errors and 3 file edits on auth code, and routes to opus instead.

Specific adjustments:
- **Active debugging** (recent errors) → bump tier up
- **Active code editing** (many edits/bash calls) → never haiku
- **Long focused chain** (5+ turns same tier) → lock to that tier, avoid flapping
- **Routine tool work** (many tool calls, no errors) → don't waste opus on grunt work

## Expected impact

A typical mixed-task session (60% trivial, 30% standard, 10% deep) on a default Opus configuration:

- **Without router:** 100% of prompts run on Opus. Estimated $/session × 1.0
- **With router:** 60% routed to Haiku (~93% cheaper), 30% to Sonnet (~80% cheaper), 10% stay on Opus. Estimated $/session × **~0.18**

Savings depend on prompt mix and session length. Estimated 70–85% cost reduction on mixed workloads.

## What it does NOT do

Honest limitations:

- **No multi-turn memory in routed answers.** When a prompt is routed to a different tier via subprocess, the answer is rendered to the user but does NOT enter the main session's in-memory history. Follow-up questions don't see the routed answer's content.
- **5–15s latency on routed prompts.** Subprocess spawn + classifier LLM call adds wall-clock delay. Trivial prompts feel slower than they would on the main session.
- **No streaming.** Routed answers appear all at once when subprocess finishes. No live token streaming.
- **Cost split.** Subprocess token usage isn't shown in the main session's `/cost` output. Total spend is correct on the Anthropic console but split across two CLI invocations.
- **Heuristics are crude.** Edge cases route wrong. The `+force` prefix is the manual override.
- **English-only keyword rules.** Non-English prompts fall through to the LLM tie-breaker more often.

## Who this is for

- Developers using Claude Code on Sonnet or Opus all day
- Teams paying significant Claude API bills
- Anyone who notices they're using Opus to run `ls` or rename a file

## Roadmap

Items being considered (no commitment yet):

- Decision cache to avoid re-classifying repeat prompts
- Local embedding classifier to remove the 6s LLM tie-breaker latency
- Telemetry slash command (`/switch-model stats`) showing accumulated savings
- Feedback loop that learns from `+force` usage
- Per-project rules via `.switch-model.json` in repo root
- Public GitHub release + marketplace listing

Recently shipped:
- Hybrid heuristic + LLM tie-breaker
- Transcript context awareness
- Effort-level matching

## Security

The plugin runs entirely locally. It does not phone home, send prompts to third parties, or modify project files. The only network calls are to the Anthropic API (the same destination Claude Code itself uses).

State files:
- `~/.cache/switch-model/state.json` — last classification result (no prompt content)
- `~/.cache/switch-model/router.log` — subprocess errors

## Status

**Alpha.** Working and used daily on the developer's own machine. Not yet hardened for public release. Speed and accuracy improvements pending before broader publish.
