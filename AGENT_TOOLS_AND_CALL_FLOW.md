# Agent, tools, and call flow

## Runtime split

Vapi handles the phone interaction and configured model. The model may request tools, but all model-provided arguments are untrusted. FastAPI validates inputs and deterministic domain services perform all state changes. Tool results should be concise and explicit so the assistant can speak them accurately.

Do not put menu prices, delivery rules, store hours, or authoritative cart totals in the prompt as facts that the backend does not also enforce.

## MVP tool surface

| Tool | Inputs | Server responsibility |
|---|---|---|
| search_menu | query | Search validated menu names, categories, and aliases |
| get_menu_item | item_id | Return authoritative item details and price |
| check_item_availability | item_id | Return current availability |
| get_cart | call context | Return current server cart |
| add_to_cart | item_id, quantity, options | Validate item, quantity, options, availability; mutate cart |
| update_cart_item | cart_item_id, quantity/options | Validate and update existing line |
| remove_from_cart | cart_item_id | Remove exact line |
| calculate_order_total | call context | Recalculate subtotal, fees, and total server-side |
| validate_address | address_text | Geocode, return best candidate(s), ask caller to confirm |
| check_delivery_zone | confirmed_address_id or coordinates | Route in drive mode from verified fixed origin; compare distance ≤ 3,000 m |
| check_store_open | current time from server | Apply Asia/Karachi schedule and last-order cutoff |
| create_order | confirmed cart + confirmation token | Revalidate and atomically create one order |
| handoff_to_human | reason | Transfer only if configured; otherwise explain and capture callback request |

The provider's call ID should come from verified request context, not a free model argument. If a tool schema must include it for provider compatibility, ignore or compare it to trusted context.

## Conversation rules

- Match the caller's language and use short, natural English or Roman Urdu.
- Ask one clarification at a time when item, size, quantity, address, or order type is ambiguous.
- Handle a sentence containing multiple items by adding only unambiguous lines; clarify ambiguous modifiers.
- Read back meaningful cart changes and support corrections at any time before confirmation.
- Do not claim an item was added until the tool succeeds.
- The supplied menu is nested by menu/category/subcategory. Search results must retain category context and source IDs. Do not finalize an optioned item's price until its option group has a verified rule for whether choice price replaces the base price or is added as a surcharge.
- Do not invent menu items, prices, option availability, delivery fees, hours, or order numbers.
- Read the final order and total. Create the order only after a clear affirmative answer. “Maybe,” silence, or an ambiguous response is not confirmation.
- If the caller interrupts, stop the current utterance and process the new request. Then fetch the cart and recalculate as needed; do not rely on a stale spoken summary.
- If a caller requests a human, hand off immediately when possible.

## Order state

Call lifecycle: started → taking_order → choosing_order_type → collecting_address (delivery only) → confirming_address → reviewing_order → awaiting_confirmation → confirmed → completed.

Recovery paths: clarification_needed returns to the relevant step; invalid/out-of-zone address offers correction or pickup; caller cancellation ends without order; transient tool failure permits one safe retry; repeated failure offers handoff or callback.

Do not make this a rigid phone tree. The stage is a guide, while each new user utterance can correct the cart or change intent.

## Address flow

1. Collect street/house and useful landmark/sector/city detail.
2. Call Geoapify forward geocoding with Lahore/Pakistan context/bias where supported.
3. If candidates are ambiguous, ask a targeted follow-up. Do not choose an arbitrary result.
4. Read back the selected address/landmark and get caller confirmation.
5. Route from the stored verified restaurant coordinates to confirmed customer coordinates with mode=drive and metric distance.
6. Accept only if returned route distance is at most 3,000 m. On no route/API failure, do not silently approve delivery.

The location API does not establish that the customer will be reachable by a specific restaurant rider. The 3 km rule is only the prototype's configured eligibility test.

## Tool error contract

Return stable machine-readable outcomes internally, such as success, not_found, unavailable, needs_clarification, out_of_zone, closed, retryable_error, and permanent_error. Convert those to short tool results for the voice model. Do not return stack traces, raw provider responses, credentials, or SQL error text.
