from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
import httpx

from app.config import Settings
from app.logging_setup import logger


class LocalDemoModel(BaseChatModel):
    """Tiny offline fallback so the terminal prototype remains usable without network access."""

    @property
    def _llm_type(self) -> str:
        return "local_demo"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        text = str(messages[-1].content).strip().lower() if messages else ""
        if any(word in text for word in ("hello", "hi", "salam", "assalam")):
            reply = "Salam! I can help you place a food order. What would you like?"
        elif "menu" in text:
            reply = "I am in offline demo mode. Ask me for an item and then connect a provider key for full menu tools."
        else:
            reply = "I can chat in this offline demo. Tell me what you would like to order."
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=reply))])


def build_models(settings: Settings) -> list[BaseChatModel]:
    """Groq first, Gemini as the fallback. Returns only the providers that have a key."""
    models: list[BaseChatModel] = []
    if settings.groq_api_key:
        try:
            from langchain_groq import ChatGroq
        except ImportError:
            logger.warning("groq_provider_unavailable")
        else:
            # ponytail: bind the provider client to IPv4; this WSL host resolves Groq to IPv6
            # but has no usable IPv6 route.
            def groq(model: str):
                return ChatGroq(model=model, temperature=0, api_key=settings.groq_api_key,
                                max_retries=1, timeout=20, reasoning_effort="low",
                                http_client=httpx.Client(
                                    transport=httpx.HTTPTransport(local_address="0.0.0.0"),
                                    timeout=httpx.Timeout(20.0, connect=5.0)))
            models.extend([groq(settings.groq_model), groq(settings.groq_fallback_model)])
    if settings.gemini_api_key:
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError:
            logger.warning("gemini_provider_unavailable")
        else:
            models.append(ChatGoogleGenerativeAI(model=settings.gemini_model, temperature=0,
                                                 google_api_key=settings.gemini_api_key.get_secret_value(),
                                                 max_retries=3, timeout=25))  # Gemini returns brief 503 spikes
    return models
