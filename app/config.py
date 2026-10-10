from decimal import Decimal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Own file on purpose: the project .env holds unrelated provider keys this app must not read.
    model_config = SettingsConfigDict(env_prefix="HUKM_", env_file=".env.hukm", extra="ignore")

    database_url: str = "sqlite:///./hukm_karo.db"
    menu_path: str = "data/menu.json"
    delivery_areas_path: str = "data/delivery_areas.json"
    branches_path: str = "data/branches.json"
    delivery_fee: Decimal = Decimal("0")

    # One restaurant, one delivery zone. Missing coordinates make the delivery check refuse, never guess.
    restaurant_lat: float | None = None
    restaurant_lon: float | None = None
    restaurant_pin_verified: bool = False
    delivery_radius_m: int = 3000
    geoapify_api_key: SecretStr | None = None

    # Service hours in Asia/Karachi ("HH:MM"). Unset means no hours enforcement (always open).
    opening_time: str | None = None
    last_order_time: str | None = None
    closing_time: str | None = None

    groq_api_key: SecretStr | None = None
    # Smaller production model is less likely to hit the free-tier capacity ceiling during demos.
    groq_model: str = "openai/gpt-oss-20b"
    groq_fallback_model: str = "llama-3.3-70b-versatile"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.8-flash"

    # Vapi voice-channel webhook auth: the secret shared between Vapi's dashboard and this server.
    vapi_webhook_secret: SecretStr | None = None

    # Shared secret for the internal order API. When set, every mutation endpoint requires
    # Authorization: Bearer <this>. Leave unset to skip (local dev).
    api_secret: SecretStr | None = None

    # Rate limit: max requests per minute per IP on mutation endpoints.
    rate_limit_per_minute: int = 120

    # Vapi voice-channel provisioning (the REST key Vapi issues for assistant/phone management).
    vapi_api_key: SecretStr | None = None
    vapi_assistant_id: str | None = None
    vapi_phone_number: str | None = None
    vapi_server_url: str | None = None  # the public URL Vapi sends tool-calls to


def get_settings() -> Settings:
    return Settings()
