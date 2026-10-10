# Latency analysis: is the architecture slow, and what to do about it

Scope: the ordering voice path end to end. The question is not "is FastAPI fast" (it is) but "how long does each spoken turn take from the customer's mouth to their ear", because that is the number that decides whether the call feels human.

## 1. The two pipelines in play

**Production today (Vapi):**

```
customer speaks
  -> Vapi STT (cloud, US regions)
  -> LLM on Vapi's side sees the transcript, must decide which tool to call
  -> POST /vapi/tool-calls to our FastAPI server (one HTTP round trip over the public internet)
  -> we run the deterministic tool, return JSON
  -> Vapi's LLM continues, produces the reply text
  -> Vapi TTS synthesizes audio, streams it back to the phone
  -> customer hears
```

Every tool decision repeats this cycle. A complete order ("two biryani and a cold drink, home address", then confirm) goes through roughly 10 to 14 tool turns: search menu, read option groups, add to cart, quote, resolve address, confirm address, check zone, confirm order. Each one is a fresh LLM call plus the webhook round trip. That multiplication, not any single component, is the real latency creator.

**Demo (what we build now):**

```
customer speaks
  -> browser SpeechRecognition (on the customer's machine, no cloud hop)
  -> POST /orders/{id}/chat/stream (SSE)
  -> FastAPI runs the full LangGraph turn, then streams the reply over SSE word by word
  -> browser speechSynthesis speaks each word as it arrives (local, starts instantly)
```

This removes the Vapi platform leg of the round trip. The remaining structural cost is the LangGraph turn itself, which we did not make faster, only overlapped with TTS.

## 2. What the recorded Vapi call actually shows

The reference call in `_vapi_call_dump.json` (a short demo order, 12 messages, 5.5 bot/user alternations) cost:

| piece | cost (USD) | note |
|---|---|---|
| STT | 0.0128 | per spoken utterance |
| LLM | 0.0413 | 12,305 prompt tokens, 164 completions, 4,531 prompt tokens served from cache |
| TTS | 0.0162 | 324 characters of speech |
| Vapi platform | 0.0634 | transport, orchestration, analysis |
| total | 0.1337 | |

Two readings from this, not cost but latency:

1. **12,305 prompt tokens for a 5.5-turn call.** The conversation window, system prompt and full tool-call history are re-sent to the model repeatedly. Prompting a big context costs time at every call, even with caching. The cache hit (4,531 tokens) confirms the server is already paying the fixed part once; the growth is the tool transcripts piling up each turn.

2. **TTS is 324 characters total but 0.0162 of the bill.** Small speech amount. The impression of slowness is not the length of what is said; it is the silence before the assistant starts, which is the accumulated LLM + webhook time of that particular turn.

## 3. Where time goes in a "slow" turn

| step | typical cost | fixed or growing |
|---|---|---|
| Speech recognition | ~150-400 ms per utterance | fixed |
| LLM first token (Vapi orchestrator) | several hundred ms | fixed-ish |
| Tool-round-trip to our server | 50-150 ms + location of the webhook | fixed |
| Each extra LLM tool call in the loop | 300-800 ms each | **times the number of tool calls** |
| TTS first audio (Vapi) | ~200-500 ms | fixed |
| Your server engine (LangGraph invoke) | one graph run per turn | growth as the conversation lengthens |

The dominant term is the tool-loop multiplier. A turn that needs no tools (a greeting) is fast. A turn that resolves an address while quoting the cart runs the LLM three or four times back to back before it can speak. The customer hears nothing during all of it because speech does not start until the reply text is finished.

## 4. What the demo already does

- **SSE word streaming (`app/api.py`, `chat_stream`).** The browser starts TTS on the first word event and keeps speaking as later words arrive over the network. TTS no longer waits for the full HTTP body. For a 15-20 word turn this hides most of the transfer time.
- **Menu and geo client warmed at startup** (`app/api.py` lifespan). No cold file loads or first geocode inside a customer turn.
- **1-2 sentence reply rule in the system prompt.** Shorter replies mean fewer TTS characters and a quicker finish.
- **No Vapi round trip.** The demo replaces the platform leg with one local POST.

## 5. What still makes it feel slow, and the fixes with the most leverage

Ranked by impact per unit of effort. Each is independent; do not need to wait for the next.

**A. Stream the LLM tokens out of the LangGraph run. (Biggest single win)**

Today `engine.reply()` runs the entire graph turn (`graph.invoke`) and returns one finished string; we then chunk that string. The customer still waits for tool calls, cart quotes and the final "yes I can say this" before the first sound.

LangGraph supports streaming (`graph.astream` / `stream_mode="messages"`) that yields tokens the moment the final LLM call produces them. If the SSE endpoint consumes the token stream and emits sentence-length chunks, time-to-first-sound drops from "whole turn" to "first sentence of the reply", which is the difference between a 3 second gap and a 0.7 second gap. This is the single change that makes the call feel human.

**B. Reduce tool-call churn (the multiplier).**

The LLM must settle the turn before speaking, so fewer required round trips means a shorter quiet period. Cheap and effective:

- Combine menu search + item detail into fewer calls where safe (one tool that returns options for the matched items used today).
- Do not re-send the whole cart transcript when the model only needs the latest quote; the `get_cart` / `calculate_total` pairing inside the loop re-broadcasts state each time.
- Keep conversation history trimming in the checkpointer so the prompt does not grow unboundedly across a long call. The dump already shows the transcript dominating the token count.

**C. Trim the prompt and lean on caching.**

4,531 of 12,305 prompt tokens were cache hits; the rest was re-sent history. Structure the system prompt + tool definitions as a cache-stable prefix (put talky instructions before any per-turn transcript, keep per-turn text last). On Groq and Gemini this cuts both cost and the pre-computation latency on every call after the first. A "warm upstream call" at startup that primes the cache is worth doing.

**D. First-token speed of the chosen model.**

`openai/gpt-oss-20b` on Groq has excellent first-token latency for this job and the tool surface is modest enough that a 20B-class model is the right size. Do not move to a frontier model for this product; the increase in perceived latency outweighs anything the extra reasoning buys in a scripted ordering flow.

**E. For the Twilio phase, keep the same rule.**

When this moves to real phone lines, the lesson transfers: stream the LLM output to a TTS that starts on the first sentence (Deepgram or AssemblyAI for STT with barge-in, a streaming TTS), keep tool calls few, and keep the same SSE-style streaming contract on our side. The webhook-per-tool model is the thing to leave behind, because it hard-codes the round trip.

## 6. What is NOT the problem

- FastAPI startup and middleware add single-digit milliseconds. Not relevant.
- SQLite reads and writes are microseconds for this order volume. Not relevant.
- The browser demo's Recognition/TTS are local and effectively free. Not relevant.

The bottleneck is the serial LLM loop. One sentence to mark good: the architecture is not slow in its components, it is slow in the number of sequential LLM waits per turn. Streaming (A) and fewer tool calls (B) are worth more together than any infrastructure change.

## 7. Before/after walkthrough of one turn

A turn that says "and my address is 24 G block, phase 4".

**Today:** LLM #1 reads the transcript and calls `resolve_address`; webhook round trip; LLM #2 decides it needs confirmation and answers; TTS starts. Quiet period: three serial model waits.

**With A + B:** the engine resolves, and the moment LLM #2 emits its first words the SSE stream carries them; the browser speaks while the tail of the turn is still landing. Quiet period: roughly one model wait.

## 8. Suggested next move

Implement A (token streaming out of `reply()`) as the next backend change, keep the SSE contract identical so the frontend does not change, then measure time-to-first-sound on a 5-turn scripted order before and after. Target: first audio for a tool-requiring turn under 1.0 second except when the LLM is genuinely slow.