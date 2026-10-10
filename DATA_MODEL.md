# Data model

This is the minimum operational schema for one location. It deliberately avoids restaurant and branch tables until the product has a second location.

## Core records

| Table | Purpose | Key fields |
|---|---|---|
| calls | One provider call and its lifecycle | id, provider_call_id unique, status, started_at, ended_at, disposition, created_at |
| carts | Current mutable order draft for one call | id, call_id unique, order_type, customer_name, phone, address_text, normalized_address, address_lat/lon, route_distance_m, delivery_allowed, version, updated_at |
| cart_items | Current cart lines | id, cart_id, menu_item_id, item_name_snapshot, unit_price_snapshot, quantity, selected_options_json, line_total |
| orders | Immutable confirmed order header | id, public_order_number unique, call_id unique, status, order_type, customer/contact/address snapshots, subtotal, delivery_fee, total, confirmed_at, created_at |
| order_items | Immutable confirmed order lines | id, order_id, menu_item_id, name_snapshot, unit_price_snapshot, quantity, options_snapshot_json, line_total |
| idempotency_keys | Prevent duplicate write operations | key unique, operation, response_json, created_at |

Use PostgreSQL numeric/decimal types for PKR monetary amounts; never binary floating point. Use integer metres for distance. Store timestamps as timezone-aware values and interpret configured restaurant hours in Asia/Karachi.

## Menu source

The MVP menu remains a versioned JSON file at data/menu.json, validated at startup. The reviewed export is nested as root restaurantId → menus → categories → subcategories → items. Items contain numeric id, name, description, price, deal, available, and optional options; each option group contains id, name, required, multiple, minSelections, maxSelections, and choices; each choice contains id, name, and price. The reviewed copy contains 1 menu, 22 categories/subcategories, 103 items, 165 option groups, and 702 choices.

Build an adapter that flattens category/subcategory context for search while retaining all source IDs and parent names. Do not rewrite source IDs into generated IDs. Preserve the supplied file and version the runtime copy.

Critical price caveat: the source has no explicit choice-price mode. Observed data includes full-looking variant prices, surcharge-looking add-ons, and zero-price modifiers. Before Sprint 2 total calculation, confirm which option groups replace the base item price and which choices add to it. Store an explicit normalized price_behavior in the adapter/config only after that mapping is verified. Until then, any item whose selected option pricing is unresolved must not be allowed into a confirmed order.

The raw data frequently has required=true and multiple=false while minSelections and maxSelections are both 0. Do not interpret those zeros as “selection optional.” Normalize effective selection rules from verified business semantics and test every option pattern. Several categories are empty; exclude empty categories from item-result claims. All exported availability flags are true, which still needs restaurant confirmation.

When an order is confirmed, copy item name, resolved base/variant price, quantity, chosen options, and line total into order_items. This snapshot preserves what the caller confirmed even if the menu later changes.

## Identifiers and concurrency

- Persist the Vapi call ID as a unique provider_call_id and use it to locate the call/cart.
- Create call and cart on call start, idempotently.
- A tool call must include/resolve the authenticated provider call context; do not trust a model-supplied call ID as authorization.
- Use a transaction for cart mutation + version increment and order creation.
- Add a uniqueness constraint for one order per confirmed call for the MVP, plus an idempotency key based on the provider call and confirmation operation.
- Use row locking or optimistic version checks for concurrent tool requests against the same cart.
- Different calls must never share a cart. Validate with two concurrent test sessions.

## Order creation transaction

Within one database transaction: lock the cart; verify it is non-empty and the latest total is server-calculated; verify availability and order type/address eligibility; verify confirmation is still pending; create the order and item snapshots; mark the call/cart completed; save the idempotent response; commit. If any step fails, roll back the whole order write.

## Data retention

Keep only information needed to fulfill and audit prototype orders. Avoid saving complete transcripts by default. If transcripts or call recordings are enabled, define consent, access controls, retention, and deletion behavior before real customers use the service.
