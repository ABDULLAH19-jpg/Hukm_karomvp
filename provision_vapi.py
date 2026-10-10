"""Provision the Vapi assistant + phone number from .env.hukm (one-off admin task).

Run once after the server and tunnel are up. It pushes our system prompt, tool definitions,
and tool-server URL to Vapi so a phone call reaches app/vapi.py.

Usage:
    python provision_vapi.py              # apply the changes
    python provision_vapi.py --dry-run    # show what would change, write nothing
"""
import argparse
import re
import sys

import httpx

from app.config import get_settings
from app.conversation import SYSTEM_PROMPT
from app.vapi import vapi_tool_definitions

VOICE_PREFIX = (
    "You are the voice ordering assistant for Hukm Karo, a restaurant in Lahore. "
    "You talk to callers, never text. Greet the caller briefly, then take their order. "
    "Keep every spoken reply to one or two short sentences.\n\n"
)

FIRST_MESSAGE = "Assalam-o-alaikum, welcome to Hukm Karo. What would you like to order?"

BASE = "https://api.vapi.ai"


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def build_tools(settings) -> list[dict]:
    """Our tool definitions as Vapi function tools (each relies on the assistant's server for routing)."""
    tools = []
    for tool in vapi_tool_definitions():
        tools.append({"type": "function", "function": tool["function"]})
    return tools


def build_assistant(settings) -> dict:
    return {
        "name": "Hukm Karo",
        "firstMessage": FIRST_MESSAGE,
        "model": {
            "provider": "openai",
            "model": "gpt-4.1",
            "temperature": 0.7,
            "messages": [{"role": "system", "content": VOICE_PREFIX + SYSTEM_PROMPT}],
            "tools": build_tools(settings),
        },
        "server": {
            "url": settings.vapi_server_url,
            "headers": {"Authorization": f"Bearer {settings.vapi_webhook_secret.get_secret_value()}"},
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    args = parser.parse_args()

    settings = get_settings()
    missing = [name for name, value in (
        ("HUKM_VAPI_API_KEY", settings.vapi_api_key),
        ("HUKM_VAPI_ASSISTANT_ID", settings.vapi_assistant_id),
        ("HUKM_VAPI_SERVER_URL", settings.vapi_server_url),
        ("HUKM_VAPI_WEBHOOK_SECRET", settings.vapi_webhook_secret),
    ) if not value]
    if missing:
        sys.exit("Missing: " + ", ".join(missing))

    headers = {"Authorization": f"Bearer {settings.vapi_api_key.get_secret_value()}"}
    assistant = build_assistant(settings)

    print(f"assistant:   {settings.vapi_assistant_id}")
    print(f"server url:  {settings.vapi_server_url}")
    print(f"tools:       {len(assistant['model']['tools'])}")

    if args.dry_run:
        print("dry run — nothing was changed")
        return

    # 1. Update the assistant.
    r = httpx.patch(f"{BASE}/assistant/{settings.vapi_assistant_id}", headers=headers,
                    json=assistant, timeout=30)
    if r.status_code >= 300:
        sys.exit(f"assistant update failed ({r.status_code}): {r.text[:600]}")
    print(f"assistant updated ({r.status_code})")

    # 2. Attach the phone number, matched by the dialed number (E.164 digits).
    if not settings.vapi_phone_number:
        print("HUKM_VAPI_PHONE_NUMBER not set — attach a number in the Vapi dashboard")
        return
    rn = httpx.get(f"{BASE}/phone-number", headers=headers, timeout=30)
    if rn.status_code >= 300:
        sys.exit(f"phone list failed ({rn.status_code}): {rn.text[:600]}")
    target = _digits(settings.vapi_phone_number)
    phone = next((p for p in rn.json() if _digits(p.get("number", "")) == target), None)
    if phone is None:
        print(f"WARNING: no phone number matching {settings.vapi_phone_number} was found; "
              "attach it in the Vapi dashboard")
        return
    rp = httpx.patch(f"{BASE}/phone-number/{phone['id']}", headers=headers,
                     json={"assistantId": settings.vapi_assistant_id}, timeout=30)
    if rp.status_code >= 300:
        sys.exit(f"phone attach failed ({rp.status_code}): {rp.text[:600]}")
    print(f"phone {phone['number']} attached ({rp.status_code})")


if __name__ == "__main__":
    main()
