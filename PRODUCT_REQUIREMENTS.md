# Product requirements: single-location phone-ordering MVP

## Goal

Let a customer call one restaurant and place a valid pickup or delivery order through a conversational agent in English or Roman Urdu. The backend validates every item and price, checks delivery eligibility, reads back a final order summary, and saves the order only after explicit caller confirmation.

## Fixed scope

| Area | MVP decision |
|---|---|
| Location | Plot No 68, Commercial Area, Cavalry Ground, Lahore; one fixed origin |
| Menu | Supplied menu JSON, loaded and validated at application startup; reviewed export has 1 menu, 22 categories, 103 items, 165 option groups, and 702 option choices |
| Voice | Vapi; ElevenLabs TTS configured through Vapi if voice tests pass |
| Backend | FastAPI application with ordinary domain/service code |
| Agent orchestration | Vapi assistant model and server-side function tools |
| Database | PostgreSQL for calls, carts/order snapshot, and orders |
| Geocoding/routing | Geoapify; customer address candidate + car route distance |
| Languages | English and Roman Urdu; no promise of perfect Urdu speech recognition |
| Delivery boundary | 3,000 metres maximum driving route from verified restaurant pin |
| Parallelism | Per-call data isolation; provider concurrency limit is checked separately |

## User journey

1. Greet the caller and establish whether they want pickup or delivery when not already clear.
2. Understand menu questions and multi-item order requests; ask clarification when an item, size, or option is ambiguous.
3. Add, change, or remove items. Repeat corrections accurately.
4. For delivery, collect enough address detail, geocode it, read back the candidate, and get caller confirmation.
5. Route from the stored restaurant coordinates in drive mode. At 3,000 metres or less, continue; otherwise offer pickup.
6. Recheck store availability and last-order cutoff.
7. Read back quantities, options, subtotal, delivery fee, and final total.
8. Require a clear affirmative confirmation before creating the order.
9. Persist the order and provide its order number.

## Functional requirements

- Search menu by name, category, and supported aliases.
- Return menu data and prices from the menu source, never from model memory.
- Enforce item availability, valid option groups, quantities, and price calculations server-side.
- Keep cart data isolated by provider call ID.
- Support cart correction until final confirmation.
- Use configured hours in Asia/Karachi, with distinct opening, closing, and last-order times.
- Validate a delivery address and confirm the geocoded result with the caller.
- Reject out-of-zone delivery deterministically and offer pickup.
- Make order creation idempotent so duplicate tool/webhook deliveries cannot create two orders.
- Record call lifecycle and final disposition without treating raw transcript as the source of truth.
- Provide a human handoff when requested or after repeated system failure, if a real destination is configured.

## Non-functional requirements

- Do not leak API keys, phone numbers, address data, or database errors into model-visible tool results.
- Use timeouts for external APIs and database work; return concise recoverable errors.
- Handle concurrent requests without shared mutable process state.
- Log correlation IDs, tool names, outcomes, latency, and call ID; minimize sensitive content.
- Keep the prototype small enough to run locally and deploy as one API service.

## Explicitly excluded

Multi-branch routing, marketplace, rider dispatch, Bykea/Yango integration, online payments, customer accounts, analytics dashboards, recommendation systems, vector search/RAG, custom model hosting, Kubernetes, and restaurant onboarding.

## Configuration and unresolved facts

These are blockers for a credible real call. Do not fabricate them:

- Restaurant name and exact verified map pin for Cavalry Ground.
- Actual weekly opening hours, closing times, and last-order cutoff.
- Confirm that runtime data/menu.json is the reviewed export. It has a root restaurantId and menus array; each menu contains categories → subcategories → items. Items have numeric IDs/prices, availability, and optional option groups/choices.
- Confirm option choice price semantics with the menu owner/POS source. Examples show both apparent full variant prices (size choices at 790/1590/2450 for a 790 base item) and apparent surcharges (a 220 fries-and-drink add-on for a 650 burger), plus zero-price modifiers. The JSON does not declare a price mode. Do not calculate selected-item prices by blindly adding every choice price to item.price.
- Confirm whether all items marked available=true are actually orderable. In the reviewed export all 103 items are true, all deal flags are false, and two categories are empty. The menu timing says 12:00 AM–11:59 PM every day; treat that as menu metadata, not verified restaurant operating hours.
- Pickup availability and delivery fee policy.
- Call number/telephony setup and provider concurrency allowance.
- Human transfer destination and staff availability.
- Whether orders are accepted with cash on delivery only or another payment method.
- Geoapify and Vapi credentials and expected usage budget.

The export has stable numeric IDs and no duplicate IDs among the 103 items, 165 option groups, or 702 choices. Forty items have no options; 63 have one or more. Fourteen item descriptions are blank. These are loader/reporting facts, not proof that menu prices or availability are current.

## MVP completion statement

An end-to-end test call can ask about the menu, add multiple items, change/remove an item, choose delivery, confirm a geocoded address inside the 3 km driving limit, receive the correct recalculated total, explicitly confirm, and find exactly one persisted order in PostgreSQL. An out-of-zone address, uncertain address, closed store, tool timeout, caller interruption, and two simultaneous calls have defined test outcomes.
