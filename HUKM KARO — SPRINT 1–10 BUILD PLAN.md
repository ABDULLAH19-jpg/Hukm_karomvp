HUKM KARO — SPRINT 1–10 BUILD PLAN

SPRINT 1 — Order Engine + State Machine
Goal:
Build the core transaction system before AI, voice, or UI.

Build:
- Menu engine
- Cart engine
- Pricing engine
- Order engine
- Order state machine
- PostgreSQL persistence

Core states:
DRAFT
→ COLLECTING_ITEMS
→ VALIDATING_ORDER
→ COLLECTING_ADDRESS
→ CHECKING_DELIVERY
→ READY_FOR_CONFIRMATION
→ CONFIRMED
→ SUBMITTED_TO_RESTAURANT
→ PREPARING
→ READY
→ COMPLETED

Failure states:
CANCELLED
OUT_OF_ZONE
HANDOFF_REQUIRED
FAILED

Dependencies:
- menu-clean.json
- FastAPI
- PostgreSQL
- Pydantic
- ORM

Acceptance criteria:
- Invalid item/modifier combinations are rejected.
- Required modifiers are enforced.
- Prices come only from menu data.
- Order totals are deterministic.
- Duplicate requests do not create duplicate orders.
- State transitions are validated.
- Orders persist correctly after restart.


SPRINT 2 — Production Backend + Database
Goal:
Turn the order engine into a reliable backend system.

Build:
- Clean service layer
- MenuService
- CartService
- PricingService
- OrderService
- AddressService
- DeliveryService
- BranchService
- Transactions
- Idempotency
- Logging
- Error handling

Core rule:
LLM → tool → service → database

Acceptance criteria:
- No direct LLM database access.
- All business rules live in deterministic services.
- Failed requests can be retried safely.
- Core services can be tested without AI.


SPRINT 3 — LangChain Tool Layer
Goal:
Expose deterministic backend actions to the AI safely.

Tools:
- search_menu()
- get_menu_item()
- check_availability()
- add_to_cart()
- update_cart_item()
- remove_cart_item()
- get_cart()
- calculate_total()
- find_branch()
- resolve_address()
- check_delivery_zone()
- create_order()
- confirm_order()
- cancel_order()
- get_order_status()
- handoff_to_staff()

Dependencies:
- Sprint 1 and 2 complete

Acceptance criteria:
- Tools use backend services only.
- Tool schemas are structured and validated.
- AI cannot invent menu items or prices.
- All tool executions are logged.


SPRINT 4 — LangGraph Decision + Conversation Engine
Goal:
Manage conversation state, clarification, and order decisions.

Graph responsibilities:
- Intent detection
- Menu inquiry flow
- Cart building
- Missing modifier clarification
- Order correction
- Address collection
- Delivery eligibility check
- Confirmation loop
- Handoff logic
- Failure recovery

Example flow:
START
→ understand intent
→ find menu item
→ missing required option?
  → yes: ask user
  → no: add to cart
→ continue ordering
→ collect address
→ check delivery
→ confirm
→ create order
→ send to restaurant

Acceptance criteria:
- English + Roman Urdu work.
- Ambiguous requests trigger clarification.
- Order corrections work.
- AI never bypasses deterministic validation.
- Conversation state survives multi-turn ordering.


SPRINT 5 — Smart Address Resolution
Goal:
Resolve real Pakistani-style spoken addresses into confirmed coordinates.

Example:
“Phase 5 ki wo wali gali jo masjid ke samne hai”

Flow:
spoken address
→ LLM structured extraction
→ Google Places / Geocoding
→ candidate locations
→ confidence score
→ clarification if needed
→ customer confirmation
→ exact lat/lng

Structured address fields:
- raw
- city
- area
- street
- landmark
- latitude
- longitude
- confidence

Acceptance criteria:
- Low-confidence addresses trigger clarification.
- Coordinates are never silently guessed.
- Customer confirms uncertain locations.
- Final coordinates are saved with the order.


SPRINT 6 — Branch Routing + Delivery Zone Engine
Goal:
Choose the correct restaurant branch and verify delivery eligibility.

Branch data:
- branch_id
- branch_name
- address
- latitude
- longitude
- delivery_radius
- opening_hours
- active

Routing logic:
1. Haversine distance shortlist
2. Google Routes / road distance check
3. Remove closed/inactive branches
4. Apply configurable delivery radius
5. Select best eligible branch

Initial rule:
- Default delivery threshold: 5 km
- Keep configurable per branch

Acceptance criteria:
- Out-of-zone customers are handled correctly.
- Closed branches are excluded.
- Delivery radius is configurable.
- Selected branch is persisted with the order.
- Nearest straight-line branch is not automatically assumed best.


SPRINT 7 — Vapi Voice Channel
Goal:
Make the same ordering system work over real voice calls.

Architecture:
Caller
→ Vapi
→ STT
→ LangGraph
→ LangChain tools
→ Hukm Karo backend
→ TTS
→ Caller

Test cases:
- “2 burgers”
- “nahi aik hi kar do”
- “large wali”
- “coke nahi sprite”
- “repeat karna”
- silence
- interruptions
- disconnects

Acceptance criteria:
- Voice uses the exact same backend logic as text.
- User can modify earlier order items.
- Interruptions work.
- Silence is handled.
- Disconnect does not accidentally confirm an order.
- Latency feels conversational.


SPRINT 8 — Asterisk + FreePBX Telephony
Goal:
Integrate real restaurant phone infrastructure.

Architecture:
Restaurant number
→ SIP trunk
→ Asterisk
→ FreePBX routing
→ AI or human staff
→ Vapi

Asterisk responsibilities:
- SIP
- Call routing
- Queues
- Transfers

FreePBX responsibilities:
- Manage Asterisk configuration

Vapi responsibilities:
- AI voice session
- STT / TTS / model interaction

Acceptance criteria:
- Incoming calls reach Hukm Karo.
- Human transfer works.
- AI failure routes to staff.
- Business hours routing works.
- Call IDs map to voice sessions.
- Telecom retries do not create duplicate orders.


SPRINT 9 — Restaurant Execution + KDS
Goal:
Close the loop from customer call to kitchen execution.

Flow:
customer calls
→ AI takes order
→ address resolved
→ branch selected
→ order confirmed
→ order sent to restaurant
→ KDS receives order
→ restaurant accepts
→ preparing
→ ready
→ completed

KDS must show:
- Order number
- Items
- Modifiers
- Quantity
- Total
- Timestamp
- Status actions

Additional states:
- rejected
- cancelled
- timeout
- handoff

Acceptance criteria:
- Restaurant receives executable orders.
- Staff can accept and progress order.
- No developer needs to touch DB or terminal.
- Failed orders are visible and understandable.

MVP CUT LINE:
STOP HERE.

At this point, Hukm Karo can:
Phone call
→ Vapi
→ LangGraph
→ LangChain tools
→ Menu/Cart/Pricing
→ Address resolution
→ Branch selection
→ Delivery eligibility
→ Order confirmation
→ PostgreSQL
→ Restaurant/KDS

This is enough for a real single-branch pilot.


SPRINT 10 — UI + Commercial Polish
Goal:
Only now build the premium customer-facing and operator experience.

Build:
- Marketing website
- Customer ordering UI
- Live transcript
- Cart view
- Address confirmation
- Order status
- Operator dashboard
- Call history
- Handoff visibility
- Basic analytics
- Branch configuration
- KDS polish
- Hukm Karo avatar/branding
- Subtle motion

Design principle:
Premium, warm, operational.
No neon AI dashboard style.

Acceptance criteria:
- UI reflects the real working system.
- Customer state matches backend state.
- Operator can understand orders, calls, and failures.
- Visual polish never changes business logic.


PILOT PHASE AFTER MVP
Before adding more features, test with one restaurant branch.

Measure:
- order completion rate
- handoff rate
- wrong-item rate
- wrong-modifier rate
- address failure rate
- branch-routing errors
- average call duration
- AI cost per order
- latency
- restaurant acceptance rate

Founder rule:
Do not add advanced features until pilot data shows what actually matters.
