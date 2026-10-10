# Operations and security

## Secrets and trust boundaries

- Keep Vapi, Geoapify, database, and any model provider credentials in environment/deployment secret storage.
- Never commit real credentials or paste them into a prompt, menu, logs, or test fixture.
- Authenticate Vapi server/function requests using the current supported mechanism and reject invalid requests before processing.
- Treat transcript text, tool arguments, provider events, and geocoder results as untrusted input.
- The model is not an authorization system. Server code performs validation and all writes.
- Do not expose raw stack traces, SQL exceptions, provider payloads, or secrets in tool results.

## Reliability

- Apply bounded timeouts to database and external provider calls.
- Retry only idempotent reads or writes protected by idempotency; one retry is enough for the prototype.
- On uncertainty, fail closed for delivery eligibility and order creation.
- Preserve incomplete call disposition, but do not turn an abandoned draft into a confirmed order.
- Give the caller short progress messages for slower address checks and recovery messages on failure.

## Concurrency

- Use async I/O where it materially helps provider/API waits; do not use global mutable cart state.
- Scope every call/cart/order operation to a trusted provider call ID.
- Use database transactions and unique constraints to guard simultaneous/replayed writes.
- Confirm Vapi account call concurrency separately from backend concurrency. A call-provider cap can reject inbound calls even when FastAPI has capacity.

## Logging and privacy

Log a request/call correlation ID, operation name, outcome, duration, and safe error code. Do not log API keys, full payment details, or complete transcripts by default. Restrict access to phone/address/order data. Define retention and deletion before moving from synthetic test calls to real customers.

If call recording or transcript retention is enabled, disclose it appropriately and configure a retention period. The prototype does not require storing audio recordings.

## Monitoring for a demo

Track call started/ended, completed order count, abandonment, tool failures, geocoding failures, out-of-zone attempts, median and p95 tool latency, and provider concurrency errors. Alert or show a visible operator status when the database or voice provider is unavailable.

## Local development

Use synthetic customer details and test addresses. Expose the local API to Vapi only through a temporary authenticated tunnel. Remove or rotate temporary credentials after testing. Keep development and any live database separate.

## Launch boundary

Passing the prototype acceptance suite does not establish restaurant readiness. Before real ordering: restaurant staff must verify the menu, prices, hours, fees, address pin, pickup/delivery rules, handoff coverage, and order-monitoring process. Confirm local telecom, privacy, and customer-notice requirements for the actual deployment.
