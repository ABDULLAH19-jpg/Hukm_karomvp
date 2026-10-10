# Hukm Karo — Runbook

## Quick start

```bash
# 1. Create the env file (copy the template and fill in the values)
cp .env.hukm.example .env.hukm

# 2. Run the server
uvicorn app.api:get_app --factory --reload --port 8000

# 3. Verify
curl http://localhost:8000/health
# → {"status":"ok","menu_items":...}
```

## Environment variables (.env.hukm)

| Variable | Required | Default | Notes |
|---|---|---|---|
| `HUKM_DATABASE_URL` | no | `sqlite:///./hukm_karo.db` | SQLite, Postgres, etc. |
| `HUKM_MENU_PATH` | no | `data/menu.json` | Menu definition |
| `HUKM_DELIVERY_AREAS_PATH` | no | `data/delivery_areas.json` | Area rules for quick check |
| `HUKM_BRANCHES_PATH` | no | `data/branches.json` | Restaurant branch list |
| `HUKM_DELIVERY_FEE` | no | `0` | Delivery charge (PKR) |
| `HUKM_RESTAURANT_LAT` | **yes** | — | Restaurant latitude |
| `HUKM_RESTAURANT_LON` | **yes** | — | Restaurant longitude |
| `HUKM_RESTAURANT_PIN_VERIFIED` | no | `false` | Set `true` after verifying on a map |
| `HUKM_DELIVERY_RADIUS_M` | no | `3000` | Max road distance (metres) |
| `HUKM_GEOAPIFY_API_KEY` | **yes** | — | Geocoding + route distance |
| `HUKM_OPENING_TIME` | no | — | `HH:MM`, e.g. `11:00` |
| `HUKM_LAST_ORDER_TIME` | no | — | `HH:MM`, e.g. `21:30` |
| `HUKM_CLOSING_TIME` | no | — | `HH:MM`, e.g. `23:00` |
| `HUKM_GROQ_API_KEY` | no | — | Primary LLM (text channel) |
| `HUKM_GROQ_MODEL` | no | `openai/gpt-oss-20b` | Production model |
| `HUKM_GROQ_FALLBACK_MODEL` | no | `llama-3.3-70b-versatile` | Fallback |
| `HUKM_GEMINI_API_KEY` | no | — | Fallback LLM |
| `HUKM_GEMINI_MODEL` | no | `gemini-3.8-flash` | Fallback model |
| `HUKM_API_SECRET` | no | — | Bearer token for the order API |
| `HUKM_RATE_LIMIT_PER_MINUTE` | no | `120` | Per-IP POST/PATCH/DELETE limit |
| `HUKM_VAPI_WEBHOOK_SECRET` | no | — | Shared secret for Vapi webhook auth |
| `HUKM_VAPI_API_KEY` | no | — | Vapi REST key (assistant/phone provisioning) |
| `HUKM_VAPI_ASSISTANT_ID` | no | — | Vapi assistant to attach tools to |
| `HUKM_VAPI_PHONE_NUMBER` | no | — | The number customers call |
| `HUKM_VAPI_SERVER_URL` | no | — | Public URL Vapi POSTs tool-calls to |

## Vapi voice channel setup

### 1. Expose the server

Vapi POSTs tool-call requests to your server. During development, expose `localhost:8000`:

```bash
# Option A: Cloudflare Tunnel
cloudflared tunnel --url http://localhost:8000

# Option B: ngrok
ngrok http 8000
```

Copy the public URL (e.g. `https://hukm-karo.ngrok-free.app`).

### 2. Configure the Vapi assistant

In the [Vapi dashboard](https://dashboard.vapi.ai), create an assistant:

- **Server URL**: `https://your-tunnel.example.com/vapi/tool-calls`
- **Server URL Secret**: same value as `HUKM_VAPI_WEBHOOK_SECRET`
- **System Prompt**: start with Vapi-specific prefix, then paste the `SYSTEM_PROMPT` from `app/conversation.py`:
  ```
  You are a voice assistant for Hukm Karo, a restaurant in Lahore. Speak naturally and keep every response to one or two short sentences.
  ```
- **Voice**: e.g. Azure `en-US-JennyNeural` or ElevenLabs
- **Transcriber**: Deepgram with `hi` and `ur` models (for Roman Urdu)

### 3. Export and import tool definitions

```bash
python -c "from app.vapi import vapi_tool_definitions; import json; print(json.dumps(vapi_tool_definitions(), indent=2))"
```

Paste the JSON output into Vapi's tool configuration for the assistant.

### 4. Buy or assign a phone number

In Vapi, assign a phone number to your assistant. Vapi handles the SIP trunk.

### 5. Test the call

Call the number. Verify:
- The call is routed to your server (check `/vapi/tool-calls` requests in logs).
- An order is created (check `GET /orders/{id}`).
- The evidence guards prevent the assistant from confirming without a customer "yes".

## Failure recovery

### Geolocation outage

If Geoapify returns errors:
- `resolve_address` returns `{"ok": false, "error": "geo_unavailable"}`.
- The model is instructed to retry once, then hand off to staff.
- Check `HUKM_GEOAPIFY_API_KEY` is still valid.

### LLM provider outage

The text channel falls back from Groq to Gemini automatically. If both fail:
- The chat endpoint returns a generic error message.
- The Vapi assistant is independent; its LLM runs on Vapi's infrastructure.

### Database locks

SQLite can lock under concurrent writes. Symptoms:
- `concurrent_update` errors in the log.
- For production, switch to Postgres.

### Address validation failures

- If `check_delivery_zone` fails twice: model hands off to staff.
- If a place is out of zone: model offers pickup, new address, staff, or cancel.
- If address resolution returns `need_detail`: model asks for more detail once, then hands off.

### Order recovery

- Every order has a public number (`HK-00001`). The customer can quote it to staff.
- `GET /orders/{id}` returns the full order state.
- Idempotency keys prevent duplicate orders.

## Running tests

```bash
python -m pytest -q
```

Expected: all tests pass, no failures. Currently 179 tests covering core ordering, delivery, schedule enforcement (including overnight hours), Vapi webhooks, and hardening.

## Monitoring

- All logs are JSON on stdout. Pipe to your log aggregator.
- Key events: `tool_call`, `order_transition`, `vapi_tool_call`.
- Warnings: `delivery_not_configured`, `schedule_not_configured`, `restaurant_pin_unverified`, `confirm_blocked`.
