# PLAN.md: build and fix plan

Scope: Sprints 1 to 9, the MVP cut line in the 10-sprint build plan. That is the plan file in the repo root whose name begins with `HUKM KARO`. The other file, `SPRINT_PLAN.md`, covers sprints 0 to 6 and is not the one this plan follows.
Written 2026-10-08 from the code on disk, the sprint plan, and `ACCEPTANCE_TESTS.md`.
Every finding below cites a file and line. Nothing here is a guess about intent.

## 0. How to use this

- A **wave** is a chunk you can finish and verify before starting the next one.
- Every task has a **Done when** you can actually run or see.
- **Needs you** marks work only a person can do. That is the real critical path, not the code.
- Fix order is by blast radius, then by what unblocks a real phone order.

## 1. Verified state

| Area | State | Evidence |
|---|---|---|
| Order engine, cart, pricing, state machine | Working, tested | `app/states.py`, `app/orders.py`, `app/cart.py`, `app/pricing.py` |
| Persistence, idempotency, optimistic locking | Working | `app/orders.py:53-73`, `app/db.py` |
| Opening hours | Working | `app/schedule.py`, `tests/test_schedule.py` (14 tests) |
| Address resolution and 3 km zone | Working logic, wrong origin | `app/delivery.py`, `app/geo.py`; origin problem in F1 |
| LangGraph conversation and guards | Working, tested | `app/conversation.py`, `app/evidence.py` |
| HTTP surface, auth, rate limit, logging | Working, tested | `app/api.py`, `tests/test_hardening.py` |
| Kitchen display | Built, three defects | `frontend/app/kds/page.tsx`, findings in F2 |
| Voice webhook | Built, never verified end to end | `app/vapi.py`, F4 |
| Telephony (Sprint 8) | Does not exist | F5 |

Test suite: 179 passing, roughly 27 seconds. The app boots against local Postgres, serves 103 menu items, and loads three chat models (Groq, Groq, Gemini).

## 2. Findings

These four change what you should do first.

### F1: blocker. The delivery origin comes from `branches.json`, and that point is not your restaurant

`app/api.py:103-112` loads `data/branches.json`, takes the first branch, and calls `delivery.set_branch(branch)`. `DeliveryService.set_branch` (`app/delivery.py:75-80`) overwrites both `self.origin` and `self.radius_m` with that branch's values.

`data/branches.json` carries explicit coordinates (`31.5020685, 74.3644417`) and labels them **Cavalry Ground**. The `HUKM_RESTAURANT_LAT` / `HUKM_RESTAURANT_LON` values in `.env.hukm` are therefore dead config: the app never reads them once a branch has coordinates.

Two consequences:

1. Editing `.env.hukm` changes nothing. This will waste an afternoon if you do not know it.
2. The zone is measured from Cavalry Ground, not from the stated restaurant at 223-FF DHA Phase 4, Sector FF. The two candidate points are **5045 m apart**, which is larger than the whole 3000 m radius. The system currently approves and refuses a different set of addresses than you intend.

`HUKM_RESTAURANT_PIN_VERIFIED=false` also makes `app/api.py:119-120` log `restaurant_pin_unverified` at every startup, and `app/delivery.py:260` appends "restaurant pin unverified" to every near-edge handoff reason.

### F2: blocker. The kitchen display shows unconfirmed orders, and one of its buttons cannot ever work

Three separate defects that compound:

1. **`list_active` is too broad.** `app/orders.py:35-41` returns every order whose state is not terminal. That includes `DRAFT`, `COLLECTING_ITEMS`, `VALIDATING_ORDER`, `COLLECTING_ADDRESS`, `CHECKING_DELIVERY`, `READY_FOR_CONFIRMATION`, `OUT_OF_ZONE`, and `PICKUP_SELECTED`. Most of those are live customer conversations, not kitchen work.
2. **The KDS offers an illegal transition.** `frontend/app/kds/page.tsx:10` places `READY_FOR_CONFIRMATION` in the "New" column, and `frontend/app/kds/page.tsx:17` gives it an "Accept order" button targeting `SUBMITTED_TO_RESTAURANT`. `app/states.py:40-41` does **not** allow `READY_FOR_CONFIRMATION → SUBMITTED_TO_RESTAURANT`. Pressing that button always returns `invalid_transition` and paints the error banner.
3. **Unknown states silently fall into "New".** `frontend/app/kds/page.tsx:31` falls back to the `new` column for any state not listed. So `VALIDATING_ORDER`, `COLLECTING_ADDRESS`, `CHECKING_DELIVERY`, `OUT_OF_ZONE`, and `PICKUP_SELECTED` orders (which have line snapshots from checkout) appear on the kitchen board as new work, with a total and no way to action them.

Net effect: the kitchen can see, and believe it should cook, an order the customer has not confirmed. Meanwhile the single button offered for that state is guaranteed to fail.

Related: `frontend/app/kds/page.tsx:13` defines a "Completed" column for state `COMPLETED`, but `COMPLETED` is terminal (`app/states.py:48`, `app/states.py:56`), so `list_active` filters it out. **That column can never be populated.** The footer at `frontend/app/kds/page.tsx:225-226` claims completed orders stay visible for shift handover. They do not.

### F3: high. There is no version control

`git log` in this directory reports "not a git repository". Nothing in this plan is revertible and there is no record of what changed. Fix this before touching F1.

### F4: high. The voice channel has never completed an order

`app/vapi.py` implements the webhook and `tests/test_vapi.py` has 9 passing tests, all against fakes. The one recorded real call (`_vapi_call_dump.json`, 2026-10-06, 76 seconds) produced no order.

Reading that transcript, the cause is not the prompt. The caller's speech was Roman Urdu, but the transcriber returned Devanagari (`मैं ज़िंदा पैदल करना चाहता हूं`), and the model then mirrored the script of its input. The "always write in the Latin alphabet" rule at `app/conversation.py:44` is present and is pushed to Vapi (`provision_vapi.py:17,51`), but a prompt rule is a weak counter to a transcript that is entirely in another script. The fix is the transcriber configuration, not the wording.

Also note: voice does not use the same model as text. `provision_vapi.py:48-49` sets `gpt-4.1` on Vapi's side, while text uses Groq then Gemini. Only the tool layer is genuinely shared, so "voice uses the exact same backend logic" is true of the tools and false of the model.

Two durability problems in the same file:

- `app/vapi.py:46-47` kept `TRANSCRIPTS` and `CALL_ORDER` in module-level dicts. Two separate
  problems, and the first was smaller than this document originally claimed:
  - The call-to-order map is not needed at all. `OrderService.create` is already idempotent on the
    key string, so replaying `f"vapi:{call_id}"` returns the same order and **no second order is
    created** after a restart, or after a retry, or after a duplicated webhook. The map was a stale
    second copy of something the database already knows.
  - The transcript store is the real loss. The evidence guards in `app/evidence.py` require the
    customer's own words, so a restart mid-call erases the evidence and the guards then refuse
    legitimate `add_to_cart` and `confirm_order` calls. They fail closed, so nothing wrong is ordered,
    but the caller has to repeat themselves and the order can stall into `HANDOFF_REQUIRED`.

### F5: high. Sprint 8 does not exist

No SIP trunk, no Asterisk, no FreePBX, no transfer to a human, no business-hours routing at the carrier. Nothing in the repository references any of it. This is infrastructure, not code, and it needs a provider decision from you.

### F6: medium. `data/delivery_areas.json` is empty

`match_area` (`app/areas.py:26-30`) always returns `None`, so `check_area` (`app/delivery.py:82-105`) always falls through to a live geocode. The feature works, but every "do you deliver to X" question costs a Geoapify call and depends on Geoapify knowing the area.

### F7: medium. The kitchen cannot see modifiers

`frontend/app/kds/page.tsx:255-261` renders each line as `quantity x name`. The order payload carries `options` (`frontend/lib/api.ts:20`, `app/orders.py:256`), and the KDS never draws them. Sprint 9 requires the KDS to show modifiers. A kitchen that cannot see "Large, Deep Pan, extra cheese" cannot make the order.

### F8: low. Dead parameters

`load_branches` (`app/branches.py:16`) accepts `fallback_lat` and `fallback_lon` and never uses them. Either implement the fallback or remove the parameters.

## 3. Wave 0: decisions only you can make

No code depends on these, but Waves 1, 3, and 4 all stall without them. Wave 1 can start in parallel.

### 0.1 Confirm the restaurant's real coordinates
**Needs you.** Open 223-FF DHA Phase 4, Sector FF and the Cavalry Ground plot on a map, and pick one. For 223-FF, right-click the exact doorstep and copy the latitude and longitude. Do not let this be approximated: it is the reference point for every delivery decision in the system.
**Done when:** you have written down one pair of coordinates and the address they belong to.

### 0.2 Decide the delivery radius
3000 m is set and tested. Confirm it stays 3000 m for this location, or state the new number.
**Done when:** one number written down.

### 0.3 Decide the phone number and SIP provider
**Needs you.** Sprint 8 needs a real number and a carrier or SIP trunk. Options range from using Vapi's own number (fastest, no Asterisk) to a full Asterisk plus FreePBX deployment. This is the single largest open decision in the project.
**Done when:** the provider and the number are chosen, or you have explicitly deferred Sprint 8.

### 0.4 Initialise version control
**Needs you** only for the choice of where the remote lives.
**Done when:** `git log` works in this directory.

## 4. Wave 1: correctness fixes, no decisions required

Start here. Everything in this wave is code you can write and test today.

### 1.1 Initialise the repository and commit the current state
**Where:** repo root.
**Do:** `git init`, add a `.gitignore` covering `venv/`, `__pycache__/`, `node_modules/`, `*.db`, `.env.hukm`, `_vapi_call_dump.json`, then commit the current tree as one baseline commit.
**Why:** everything after this should be revertible.
**Done when:** `git log` shows one commit and `git status` is clean, and `.env.hukm` is not tracked.

### 1.2 Single-source the delivery origin and make the mismatch loud
**Where:** `app/api.py:96-112`, `app/branches.py:16-21`, `app/delivery.py:75-80`.
**Do:** pick one authority and enforce it. Recommend: `branches.json` stays authoritative for a branch's own coordinates, and `app/api.py` compares them against `settings.restaurant_lat/lon` at startup and logs a `delivery_origin_mismatch` warning naming both points and the distance when they disagree by more than 100 m. Remove the unused `fallback_lat` / `fallback_lon` parameters (F8) or use them when a branch has no coordinates.
**Why:** F1. Right now a config edit silently does nothing.
**Done when:** startup logs name the origin actually in use, which file supplied it, and, if the two disagree, the distance between them. A unit test covers the disagreement path.

### 1.3 Set the confirmed origin and the verified flag
**Where:** `data/branches.json`, `.env.hukm`.
**Depends on:** 0.1.
**Do:** write the confirmed coordinates into both files, set `HUKM_RESTAURANT_PIN_VERIFIED=true`.
**Done when:** the startup log no longer prints `restaurant_pin_unverified`, and a real address inside the zone checks out, and one outside it does not, using the real Geoapify client.

### 1.4 Give the kitchen board its own state set, server side
**Where:** `app/states.py`, `app/orders.py:35-41`, `app/api.py:246-252`, `frontend/app/kds/page.tsx:9-22`.
**Do:** define the board columns in `app/states.py` as the single source of truth, for example `KITCHEN_COLUMNS = {"new": [CONFIRMED, SUBMITTED_TO_RESTAURANT], "preparing": [PREPARING], "ready": [READY], "completed": [COMPLETED]}`, and an explicit set of states the board displays. Serve that mapping from a `/orders/board` endpoint (or extend `/orders/active`) along with the orders, so the frontend stops hard-coding a second copy. Keep orders that are still conversations off the board entirely.
**Why:** F2 items 1 and 3. One source of truth removes the class of bug rather than the instance.
**Done when:** an order in `VALIDATING_ORDER`, `COLLECTING_ADDRESS`, `CHECKING_DELIVERY`, `OUT_OF_ZONE`, `PICKUP_SELECTED`, or `READY_FOR_CONFIRMATION` does not appear on the kitchen board, and a test asserts it for each of those states.

### 1.5 Make the "Completed" column actually populate
**Where:** `app/orders.py:35-41`, `frontend/app/kds/page.tsx:13`.
**Depends on:** 1.4.
**Do:** include `COMPLETED` orders created within a window (for example the last 12 hours) in the board payload, so the column and the footer claim both become true.
**Done when:** completing an order in the KDS moves it to Completed and it stays there across a page refresh.

### 1.6 Prove no dead control can come back
**Where:** a new test, plus `app/states.py`.
**Depends on:** 1.4.
**Do:** for every state the board offers a next action for, assert that `TRANSITIONS` allows that exact target. This is the test that would have caught F2 item 2.
**Why:** `frontend/app/kds/page.tsx:17` shipped a button the state machine refuses.
**Done when:** the test fails if anyone adds a board action that `check_transition` would reject.

### 1.7 Make a call survive a restart
**Where:** `app/vapi.py:46-47`, `app/vapi.py:201-206`.
**Do:** drop `CALL_ORDER` and call `service.create(f"vapi:{call_id}")` on every tool-call batch; `OrderService.create` (`app/orders.py:53-73`) already returns the existing order for a repeated key. Move the transcript store to the database, keyed by call id, so the evidence guards still find the customer's words after a restart.
**Why:** F4. This was written as a duplicate-order fix, but there is no duplicate: the idempotency key already prevents it. The real bug is the erased transcript, which makes the guards refuse legitimate calls mid-conversation.
**Done when:** a test creates an order, restarts the service, replays the same call id, and asserts exactly one order exists with the same public number; and a second test shows the caller's words are still readable, and still accepted as evidence, after a restart.

### 1.8 Redact and relocate the call recording
**Where:** `_vapi_call_dump.json` in the repo root.
**Do:** move it out of the project or into a gitignored folder. It contains a call transcript and a recording URL, which is customer data.
**Done when:** the file is not tracked and not in the repo root.

## 5. Wave 2: make the kitchen usable

Depends on Wave 1.

### 2.1 Show modifiers on every line
**Where:** `frontend/app/kds/page.tsx:255-261`, `frontend/lib/api.ts:20`.
**Do:** render `line.options` under each line, as the choice names the customer picked. Type `options` properly in `OrderLine` instead of `unknown`.
**Why:** F7. Sprint 9 requires modifiers and a kitchen cannot work without them.
**Done when:** a pizza with crust, size, and topping shows all three on its card in the kitchen.

### 2.2 Give handoffs a place to land
**Where:** `app/states.py:47`, `frontend/app/kds/page.tsx`.
**Do:** `HANDOFF_REQUIRED` orders currently fall to the "New" column by accident. Give them their own visible treatment with the handoff reason (`app/orders.py:161-170` stores it) and the actions staff actually need: resolve to `COLLECTING_ITEMS`, `CONFIRMED`, `CANCELLED`, or `FAILED`, all of which `app/states.py:47` allows.
**Why:** Sprint 9 requires failed and handoff orders to be visible and understandable to staff.
**Done when:** an order sent to handoff by `handoff_to_staff` appears with its reason and can be resolved from the board.

### 2.3 Add the states Sprint 9 asks for and the code lacks
**Where:** `app/states.py`.
**Do:** Sprint 9 lists `rejected`, `cancelled`, `timeout`, and `handoff` as additional states. `CANCELLED` and `HANDOFF_REQUIRED` exist. Decide whether `FAILED` covers `timeout` (recommend: yes, with a reason string) and add `REJECTED` for a kitchen refusal, reachable from `SUBMITTED_TO_RESTAURANT` and `PREPARING`.
**Done when:** a kitchen can reject an order and the customer-facing state is distinguishable from a cancellation.

### 2.4 Screen-level check of the board on a phone
**Where:** `frontend/app/kds/page.tsx`.
**Do:** open the board at 375 px wide with an order in each column. Check for horizontal overflow, that every control is reachable by Tab with a visible focus ring, and that the columns reflow instead of scrolling sideways.
**Done when:** the board is usable at phone width with no sideways scroll and keyboard navigation works end to end.

## 6. Wave 3: make the voice channel true

Depends on Wave 0 decisions.

### 3.1 Fix the transcriber, not the prompt
**Where:** Vapi assistant config, `provision_vapi.py:43-58`.
**Do:** set the transcriber language explicitly so Roman Urdu and English come back in Latin script, and add the transcriber settings to `provision_vapi.py` so the configuration is reproducible rather than clicked in a dashboard. Keep `app/conversation.py:44` as a backstop.
**Why:** F4. The Devanagari output came from the input script, not from a missing rule.
**Done when:** a recorded call where every assistant turn is Latin script, including after the caller speaks Urdu.

### 3.2 Make the voice path reproducible
**Where:** `provision_vapi.py`, `.env.hukm`, `RUN.md`.
**Do:** confirm `provision_vapi.py --dry-run` then apply, so the assistant in Vapi is definitely the one this repository describes. Record the assistant id and the tool count.
**Done when:** the dashboard assistant matches `provision_vapi.py` output, and the tool count equals the count `vapi_tool_definitions()` returns.

### 3.3 Run the Sprint 7 call scripts and record the results
**Where:** `ACCEPTANCE_TESTS.md` section D, and the Sprint 7 test list in the sprint plan.
**Do:** place real calls for: two burgers; "nahi aik hi kar do"; "large wali"; "coke nahi sprite"; "repeat karna"; a silent turn; an interruption mid-reply; and a hang-up before confirmation. Record the actual result of each, as the acceptance doc demands. Confirm the disconnect case does not confirm an order.
**Done when:** every Sprint 7 case has a recorded pass or a documented blocker, and the database holds no confirmed order for the abandoned call.

### 3.4 Latency check on a real call
**Where:** `app/conversation.py:200-208` already logs `llm_call` with milliseconds.
**Do:** from a real call, collect the per-turn gaps a caller actually hears, and compare against the dead-air threshold in `GAPS_AND_SOLUTIONS.md` Gap 1.
**Done when:** a recorded call with per-turn timings attached, and a stated verdict on whether the pauses are acceptable.

## 7. Wave 4: telephony (Sprint 8)

Depends on 0.3. This wave is mostly not code.

### 4.1 Choose the topology and write it down
**Needs you.** Either keep Vapi's number (no Asterisk, fastest path to a pilot) or stand up SIP to Asterisk to FreePBX with Vapi behind it.
**Done when:** one diagram and one provider decision, recorded in the repo.

### 4.2 Incoming calls reach Hukm Karo
**Needs you:** a number and trunk credentials.
**Done when:** a call to the restaurant number starts an AI session and a tool call lands in the server log.

### 4.3 Human transfer and AI-failure routing
**Where:** Vapi transfer config, plus a fallback route.
**Do:** transfer to a staff extension, and route to staff when the AI fails rather than dropping the call.
**Done when:** a requested transfer reaches a person, and a forced AI failure also reaches a person.

### 4.4 Business-hours routing at the carrier
**Where:** `app/schedule.py` already answers open or closed; the carrier-side routing is separate.
**Done when:** a call outside 11:00 to 03:00 Asia/Karachi gets the closed message without reaching the model.

### 4.5 Prove telecom retries do not duplicate
**Where:** `app/vapi.py`, `app/orders.py:53-73`.
**Depends on:** 1.7.
**Done when:** replaying the same call's tool requests produces exactly one order, verified after a server restart.

## 8. Wave 5: record the acceptance run

### 5.1 Execute `ACCEPTANCE_TESTS.md` A through D
**Where:** `ACCEPTANCE_TESTS.md`.
**Do:** the deterministic service and API tests already pass (179 green). Run the remaining scenarios against the real stack and record each result with the model, voice, and revision used, as the document's preamble requires.
**Done when:** every scenario in sections A to D has a recorded pass or a documented blocker.

### 5.2 Run the text acceptance harness and keep the output
**Where:** `tests/chat_acceptance.py`.
**Do:** run it and store the output. It drives the real engine over 10 scenarios and asserts on server state rather than wording. It reports provider quota separately, so a rate limit is not scored as a behaviour failure.
**Done when:** a stored run with a pass or fail per scenario.

### 5.3 The definition of done, checked as a whole
**Do:** confirm all three of these together: a real phone call that completes an order; an in-zone order correction; an out-of-zone attempt. Then confirm the database holds exactly one correct confirmed order for the successful call and none for the abandoned ones.
**Done when:** all three are recorded, with the database state pasted next to each.

## 9. Critical path

The shortest true path to one real phone order, with the human steps named:

1. Decide the coordinates (0.1). **Human.**
2. Set the origin and the verified flag (1.3).
3. Fix the kitchen board states (1.4, 1.5), because someone has to see the order arrive.
4. Fix the transcriber (3.1). **Human, dashboard or config.**
5. Provision cleanly and place one real call (3.2, 3.3). **Human, on a phone.**
6. Watch it land on the board with its modifiers (2.1).

Steps 1 and 4 are the only ones that cannot be done in code, and both are small. Everything else in this plan can proceed while you do them.

Realistically, Waves 1 and 2 are a day or two of work. Wave 3 is an afternoon plus your time on the phone. Wave 4 is the only genuinely long item, and it is gated on 0.3.

## 10. Open questions

1. Which address is the restaurant: 223-FF DHA Phase 4, Sector FF, or the Cavalry Ground plot? F1 makes this the highest-impact unknown in the system.
2. Does Sprint 8 mean Asterisk and FreePBX specifically, or just "customers can dial a number"? The first is a project, the second is a Vapi setting.
3. Are the 3 active orders currently in the database real, or leftover test data? The kitchen board currently shows them.
4. Who operates the kitchen display, on what device, and does that device exist yet?
5. `data/delivery_areas.json` is empty. Do you want a curated list of served and refused areas (F6), or should every area question stay a live geocode?

## 11. Explicitly out of scope

Sprint 10 (marketing site, customer ordering UI polish, live transcript, operator dashboard, call history, analytics, branding, motion) is not in this plan. One item in that list is worth pulling forward if the pilot needs it: call history and handoff visibility, because Wave 3 produces recordings and transcripts nobody can currently read.
