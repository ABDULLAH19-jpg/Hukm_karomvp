# ADR-001: single-location voice-ordering architecture

- Status: Accepted for MVP
- Date: 2026-10-04

## Context

The product needs a phone conversation, a menu lookup, transactional cart/order behavior, a single delivery zone, and persistence. The proposed initial stack included Vapi, ElevenLabs, LangChain, LangGraph, Jev, MongoDB, and Geoapify. Current product documentation shows that Vapi already supports a configured assistant model, server-side function tools, call events, and ElevenLabs TTS. Stacking a second model-driven agent behind those tools would duplicate orchestration.

## Decision

Use a single FastAPI application as the business backend. Vapi owns the voice session and its assistant model for the first MVP. Vapi function tools call server-side endpoints. Python domain services own menu validation, cart mutation, pricing, hours, delivery eligibility, and order creation. Use PostgreSQL for transactional records. Load the supplied menu JSON as read-only menu data initially. Use Geoapify for address geocoding and driving-route distance.

Do not add LangChain, LangGraph, Jev, MongoDB, or a separate ElevenLabs pipeline to the first critical path. Use ElevenLabs through Vapi's supported voice configuration if call tests confirm the required voice quality. Keep provider interfaces small enough to swap later.

## Why

- Each critical transaction has one authoritative backend implementation.
- Vapi's documented function-tool flow already maps model intent to server-owned actions.
- The order domain has relational constraints and a transactional commit point; PostgreSQL fits it.
- Menu JSON is enough for one branch and one fixed catalog; no search database is warranted.
- A synchronous server-side tool call is simpler to debug than coordinating Vapi tools, a LangGraph model loop, and a Jev fallback before the first order works.

## Consequences

- Natural-language quality depends on the configured Vapi assistant model and voice pipeline; benchmark English and Roman Urdu on actual calls.
- Conversation state must be attached to provider call ID and persisted as needed by the backend; process globals are forbidden.
- LangGraph may be adopted later if real requirements emerge for resumable multi-step workflows or graph-native human approval. If adopted, it becomes the sole application-level orchestrator behind a custom-model route; do not leave a second independent Vapi model making tool decisions.
- Provider concurrency and pricing limits remain external constraints and must be verified in the account.
- The fixed address must be geocoded once and manually verified before it is used as the routing origin.

## Alternatives considered

### Vapi + FastAPI function tools (chosen)

Fastest route to a tested phone order. Vapi's model decides when to call a tool; backend code validates and performs the action.

### Vapi voice transport + application-owned LangGraph/custom model

Potentially better control of workflow state and model routing, but adds a custom LLM endpoint, streaming/latency concerns, and more integration tests. Revisit after the simpler flow demonstrates a need.

### MongoDB for all state

Possible, but unnecessary for the order/customer/cart relationships and transaction requirements. JSON menu compatibility alone is not enough reason to choose a document database.

### Jev as primary model

Deferred. The first requirement is reliable function calling in real calls, especially for quantities, options, corrections, Roman Urdu, and tool errors. Jev can be evaluated offline later against a fixed test set.

## Non-negotiable invariants

1. The model never supplies authoritative price, delivery distance, open status, or order ID.
2. Order creation requires explicit caller confirmation and is idempotent.
3. No cart or conversation state is stored in a global variable.
4. The delivery origin is a verified fixed coordinate, not a fresh model interpretation.
5. Provider/tool payloads are untrusted input and are validated server-side.
