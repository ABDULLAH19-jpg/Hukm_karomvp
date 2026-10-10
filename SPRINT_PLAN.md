# Sprint plan

Work in order. A sprint is done only when its exit criteria pass. “Today” is not a technical requirement; an end-to-end voice MVP is not credible until credentials, the verified address pin, actual restaurant hours, and the menu have been checked.

## Sprint 0 — Inputs and repository baseline

**Deliver:** Confirmed menu JSON/schema and option-price rules, fixed restaurant config, actual hours and last-order cutoff, delivery fee, pickup policy, verified map pin, Vapi account/phone setup, Geoapify key, human handoff plan.

**Tasks**

- Inspect the supplied menu and document required fields and invalid records. The reviewed export has 103 items; explicitly decide how empty categories, required option groups with zero min/max values, and full-price versus surcharge choices are handled.
- Geocode the restaurant address once and manually verify the pin against a trusted map/restaurant confirmation.
- Record secrets only in local environment/deployment secret settings; add a safe example env file.
- Confirm provider concurrency and call/voice provider selection.
- Decide what call data is retained and ensure appropriate notice/consent for any recording.

**Exit:** No fabricated business values remain in the call path; option-price behavior is verified with the menu owner/POS reference; app configuration validates on startup.

## Sprint 1 — Foundation and menu domain

**Deliver:** FastAPI health endpoint, settings, validated menu loader, PostgreSQL connection and migrations, calls/carts/orders schema skeleton.

**Tasks**

- Establish one runnable app and local setup instructions.
- Validate duplicate IDs, missing prices, malformed options, and invalid availability in the menu. Normalize the nested export and enforce the verified option selection/pricing rules.
- Create database schema and startup health checks.
- Define domain models separate from HTTP and provider payloads.

**Exit:** Application starts with valid config/menu; malformed data fails loudly; database connectivity and migrations are verified.

## Sprint 2 — Deterministic order engine

**Deliver:** Menu lookup, cart mutation, option/availability validation, price calculations, hours/last-order checks, transactional/idempotent create-order service.

**Tasks**

- Implement menu search and item lookup.
- Implement per-call cart operations and correction paths.
- Calculate totals from server data only, including explicitly configured delivery fee.
- Implement order snapshots and one-order-per-confirmed-call protection.
- Add API/service tests for happy path, invalid quantity/options, out-of-stock item, mutation replay, and transaction rollback.

**Exit:** All acceptance cases for data/order correctness pass without an LLM.

## Sprint 3 — Vapi assistant and function tools

**Deliver:** A Vapi-configured assistant with the approved tool schemas and authenticated FastAPI tool endpoint.

**Tasks**

- Configure Vapi model/STT/TTS; test English and Roman Urdu with representative menu terms.
- Connect tools one at a time, beginning with search/get item and cart operations.
- Bind requests to the provider call ID; validate webhook/tool authentication.
- Implement concise success/error result messages and caller interruption tests.
- Do not add LangGraph or Jev in this sprint.

**Exit:** A scripted call can ask menu questions and add/change/remove items; every mutation is visible in the server cart and isolated by call.

## Sprint 4 — Address, delivery, and restaurant availability

**Deliver:** Address resolution/confirmation, route check, 3 km enforcement, configured schedule validation.

**Tasks**

- Integrate Geoapify forward geocoding with timeout and error mapping.
- Ask the caller to confirm address candidate before routing.
- Route using drive mode and metric units from the verified fixed origin.
- Test inside, outside, ambiguous, unrouteable, and provider-timeout cases.
- Enforce service hours and last-order cutoff in server code.

**Exit:** The model cannot override zone or open-state results; failures do not approve an order.

## Sprint 5 — End-to-end voice order

**Deliver:** Phone call from greeting through final confirmation and persisted order number.

**Tasks**

- Add complete conversational prompt and dynamic order summary.
- Ensure corrections invalidate stale totals and force fresh cart/total reads.
- Create an order only after explicit affirmative confirmation.
- Test caller barge-in, ambiguous confirmation, caller asks for human, and call disconnect.
- Configure fallback/handoff destination if available; otherwise callback capture.

**Exit:** The full acceptance script passes on a real test call and produces exactly one persisted order.

## Sprint 6 — Hardening and prototype release

**Deliver:** Reliable two-call behavior, operational runbook, useful logs, and a controlled demo.

**Tasks**

- Test two or more simultaneous independent calls; check Vapi account concurrency separately.
- Add rate limits, request authentication, timeouts, one safe retry, and idempotency checks.
- Test database, model/voice, and Geoapify failure paths.
- Review logs for secrets and unnecessary personal data.
- Measure call latency, tool latency, tool errors, and completion rate on a small scripted set.

**Exit:** Acceptance suite passes, unresolved production constraints are documented, and no real restaurant launch is represented as safe without staff verification.

## Later, only with evidence

Evaluate LangGraph if we need resumable graph workflows, explicit human approvals, or multi-step recovery beyond the current tool-call pattern. Evaluate Jev using a repeatable offline benchmark for Roman Urdu understanding and tool-call accuracy. Migrate menu JSON to PostgreSQL only when operators need editing, availability changes, or multiple menus. Each addition needs a measured requirement.
