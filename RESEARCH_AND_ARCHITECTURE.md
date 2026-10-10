# Research and architecture

Research checked on 2026-10-04 against current vendor documentation. The sources below are vendor primary sources. Product behavior and prices can change; recheck before connecting a live phone number.

## What current voice-agent products show

### Voice layer and application layer are separate

Vapi's platform receives call events and can send status updates, transcripts, function calls, and end-of-call reports to a server URL. Its function tool pattern lets the model propose a named action and arguments, then sends the call to application-owned code; the server validates and executes it and returns a result for the assistant to speak. That is a direct fit for ordering: Vapi handles the voice session while our API owns menu, pricing, and order truth.

Twilio ConversationRelay describes the same broad split with a different integration surface: Twilio handles speech recognition and speech synthesis while the application connects through a WebSocket and supplies the conversational logic. Twilio's guidance recommends streaming generated text to reduce perceived latency. This confirms that the general product pattern is a voice transport/provider connected to a separately owned application, not one giant model doing everything.

### One conversational orchestrator, not two

Vapi's assistant already has a model that decides whether to call its configured tools. If the MVP also puts an independent LangGraph LLM behind those same tools, there are two model-driven orchestration layers. They can disagree about state, call the same action twice, add latency, and make debugging harder.

There are two coherent choices:

| Pattern | What runs the conversation | Best use |
|---|---|---|
| Vapi assistant + backend function tools | Vapi's configured model | Recommended first phone MVP; shortest integration path. |
| Vapi voice transport + custom LLM endpoint backed by LangGraph | Our application / graph | Use when application-owned graph logic is a demonstrated requirement; more code and streaming work. |

Do not mix the two by having both models independently decide the next action. The MVP should start with the first pattern. Keep LangGraph as a later, evidence-based addition. A deterministic Python service layer is required either way.

### LangGraph persistence is scoped by thread

LangGraph checkpointers persist graph state by thread and require a stable thread identifier. If a later sprint adopts LangGraph, use the provider's call ID as the thread ID and use a durable Postgres-backed checkpointer for restart recovery. Do not use process-global variables for carts or call state. Do not persist secrets or unnecessary transcripts in graph state.

### Address eligibility needs routing, not straight-line distance

Geoapify's routing API accepts ordered coordinate waypoints and a travel mode such as drive; its route result includes distance in metres when metric units are selected. Its forward geocoder accepts free-form or structured addresses and returns candidate locations. Therefore address text must first be resolved to a candidate, read back for caller confirmation, and then routed from the fixed restaurant pin. The server compares route distance to 3,000 metres. The LLM never grants delivery eligibility.

### Voice provider configuration

Vapi currently documents ElevenLabs as a supported TTS provider. Configure it through Vapi's voice settings if selected; do not build a second independent TTS pipeline beside Vapi for the first prototype. Urdu/Roman Urdu quality, pronunciation, interruptions, and latency need a real call test; a provider listing does not guarantee the desired language quality.

### Parallel calls have two separate capacity limits

Application state must be isolated by call ID, but the voice provider also enforces account-level concurrent-call capacity. Vapi documents subscription concurrency limits for inbound as well as outbound calls. Thus “the backend is async” does not mean the product can accept unlimited parallel calls. Check the account limit and test two simultaneous calls during hardening.

## Chosen MVP architecture

Phone caller → Vapi call/voice pipeline (configured STT, assistant model, ElevenLabs TTS) → FastAPI function-tool endpoint → deterministic domain services → menu JSON, PostgreSQL, and Geoapify → structured tool result → Vapi speaks response.

FastAPI also receives relevant Vapi call lifecycle events. The application records call start/end and final order/call outcomes. One call ID scopes its cart and call record.

## Important correction to the original proposal

The original stack listed Vapi, LangChain, LangGraph, Jev, and another LLM in one critical path. That is not a validated architecture; it is several orchestration choices stacked together. For the first complete phone order, use Vapi's assistant and function tools with deterministic backend services. Do not put Jev on the transaction path. Evaluate LangGraph only after an observable failure that a graph/checkpointer solves. LangChain is not required just to expose ordinary FastAPI functions as Vapi tools.

The key reliability property is not a fashionable framework: a model may request an action, but only validated backend code can change a cart, compute the total, admit an address, or persist an order.

## Sources

- [Vapi server URLs and call events](https://docs.vapi.ai/server-url)
- [Vapi tool execution and server-side validation guidance](https://docs.vapi.ai/tools)
- [Vapi function tools](https://docs.vapi.ai/tools/custom-tools)
- [Twilio ConversationRelay overview](https://www.twilio.com/docs/voice/conversationrelay)
- [Twilio ConversationRelay best practices](https://static0.twilio.com/docs/voice/conversationrelay/best-practices)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Geoapify forward geocoding](https://apidocs.geoapify.com/docs/geocoding/forward-geocoding/)
- [Geoapify routing API](https://apidocs.geoapify.com/docs/routing/)
- [Vapi ElevenLabs TTS integration](https://docs.vapi.ai/providers/voice/elevenlabs)
- [Vapi call concurrency](https://docs.vapi.ai/calls/call-concurrency)
