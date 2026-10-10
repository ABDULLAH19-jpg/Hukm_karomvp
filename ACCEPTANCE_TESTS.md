# MVP acceptance tests

Run deterministic service/API tests first; then run the call scripts against Vapi. Record actual results, model/voice configuration, and build revision. Never mark a scenario passed from prompt inspection alone.

## A. Menu and cart

1. Ask “Zinger kitne ka hai?” The assistant returns the exact configured price and currency from the menu.
2. Ask for two zinger burgers and one large fries in one sentence. Cart has exactly those lines and quantities.
3. Add a drink with ambiguous size. The assistant asks regular or large; no guessed option is committed.
4. Change a quantity, remove an item, then recalculate. Spoken and persisted totals match backend output.
5. Ask for a nonexistent or unavailable item. No item is added and the explanation is clear.
6. Submit zero, negative, excessive, or malformed quantity/options directly to the tool endpoint. Server rejects them.

## B. Delivery and opening schedule

1. Give a resolvable Lahore address whose drive route is below 3,000 m. Assistant confirms candidate; backend route permits delivery.
2. Give a route above 3,000 m. Delivery is rejected and pickup is offered.
3. Give an ambiguous address. System asks a clarifying question and does not route an arbitrary candidate.
4. Simulate Geoapify timeout/no route. System does not approve delivery; it retries once or offers staff/callback.
5. Test just before opening, during service, after last-order cutoff, and after closing using configured Asia/Karachi time. Expected response follows configuration.

## C. Confirmation and persistence

1. Caller says yes to a fresh final summary. Exactly one order and its item snapshots are saved; caller receives the persisted public order number.
2. Caller says “actually remove one zinger” during confirmation. No order is created yet; cart and total are refreshed and the caller is asked again.
3. Caller says “maybe,” stays silent, interrupts, or disconnects before confirmation. No confirmed order is created.
4. Replay the same create-order tool request. Exactly one order exists and the same result is returned.
5. Force a database failure during order creation. Transaction rolls back; assistant does not claim success.

## D. Isolation and voice behavior

1. Start two simultaneous calls and add different items. Each call sees only its own cart.
2. Interrupt the assistant while it reads the order. It stops; the new instruction is applied; the next summary reflects current cart.
3. Request a human. Transfer occurs to the configured destination, or a callback request is captured if transfer is unavailable.
4. Cause a tool timeout and a model/voice response timeout. Caller receives a short recovery prompt; no silent failure or false success.

## Definition of done

Every test in sections A–D has a recorded pass or a documented blocker. The happy-path real phone call plus an in-zone order correction and an out-of-zone attempt all pass. The database contains exactly one correct confirmed order for the successful test call and none for abandoned/unconfirmed calls.
