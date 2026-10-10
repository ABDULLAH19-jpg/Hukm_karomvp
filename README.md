# Hukm Karo MVP documents

This folder defines the first working phone-ordering prototype for one Lahore restaurant location. Read the research and architecture decision before implementing; the technology list from the original idea is not treated as a requirement when it adds unnecessary moving parts.

## Start here

1. [Research and architecture](RESEARCH_AND_ARCHITECTURE.md) — what current voice-agent products do and the resulting design.
2. [Product requirements](PRODUCT_REQUIREMENTS.md) — frozen MVP scope and unresolved restaurant facts.
3. [Architecture decision](ADR-001-ARCHITECTURE.md) — chosen components and rejected duplication.
4. [Data model](DATA_MODEL.md) — the minimum persistent schema and call isolation.
5. [Agent, tools, and call flow](AGENT_TOOLS_AND_CALL_FLOW.md) — tool contracts, conversation behavior, and safety boundaries.
6. [Sprint plan](SPRINT_PLAN.md) — ordered work, exit criteria, and dependencies.
7. [Acceptance tests](ACCEPTANCE_TESTS.md) — observable definition of done.
8. [Operations and security](OPERATIONS_SECURITY.md) — secrets, retries, logs, outages, and live-call limits.

## Product boundary

One restaurant location: Plot No 68, Commercial Area, Cavalry Ground, Lahore. The reviewed menu export is the supplied menu JSON (the attached export is named menu-clean(1).json; use the project's data/menu.json path for the runtime copy). English and Roman Urdu. Phone ordering for pickup or delivery. Delivery is eligible only when a verified customer address can be routed by car at 3,000 metres or less from the stored restaurant coordinates.

This is a prototype for one location. It is not a marketplace, multi-tenant platform, delivery dispatch product, payment system, or production call center.

## Before a real customer call

Resolve the menu option-price interpretation before enabling cart totals. Also replace unresolved values in the requirements document: real opening and last-order hours, pickup/delivery policy, delivery fee, exact branch pin, phone number/provider configuration, and human handoff destination. The stated street address alone is not proof of the correct map pin.

## How to use these docs with coding agents

Give agents this folder and ask them to work only on the current sprint. They must read the architecture decision, keep server-side business rules deterministic, preserve the single-location limit, and report tests and unresolved blockers. Do not ask an agent to implement later sprints while an earlier sprint's exit criteria are failing.
