"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import Vapi from "@vapi-ai/web";
import {
  API,
  cancelSpeech,
  createOrder,
  rupees,
  streamChat,
  type Order,
  type OrderLine,
} from "../lib/api";

type Phase = "idle" | "listening" | "thinking" | "speaking" | "error";

interface Turn {
  role: "user" | "agent";
  text: string;
}

const PHASE_LABEL: Record<Phase, string> = {
  idle: "Ready",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
  error: "Needs attention",
};

const PHASE_HINT: Record<Phase, string> = {
  idle: "Tap the mic and order in your own words.",
  listening: "I am listening. You can speak now.",
  thinking: "Checking the menu and your order.",
  speaking: "Reading back. Tap the mic to interrupt.",
  error: "The line dropped. Tap the mic to reconnect.",
};

// Vapi mode is on only when the web demo has the client credentials in env.
const VAPI_PUBLIC_KEY = process.env.NEXT_PUBLIC_VAPI_PUBLIC_KEY?.trim() ?? "";
const VAPI_ASSISTANT = process.env.NEXT_PUBLIC_VAPI_ASSISTANT_ID?.trim() ?? "";
const USE_VAPI = Boolean(VAPI_PUBLIC_KEY && VAPI_ASSISTANT);
const EMPTY_ORDER: Order = {
  id: 0,
  number: "",
  state: "",
  created_at: null,
  updated_at: null,
  address: null,
  address_confirmed: false,
  branch: null,
  lines: [],
  subtotal: null,
  delivery_fee: null,
  total: null,
  issues: [],
};

export default function VoicePage() {
  const [phase, setPhase] = useState<Phase>("idle");
  const [order, setOrder] = useState<Order | null>(null);
  const [turnLog, setTurnLog] = useState<Turn[]>([]);
  const [interim, setInterim] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [supported, setSupported] = useState<boolean | null>(null);

  const orderRef = useRef<Order | null>(null);
  const vapiRef = useRef<Vapi | null>(null);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const stopListeningRef = useRef(false);
  const logRef = useRef<Turn[]>([]);
  const callSpanRef = useRef<[number, number] | null>(null);

  const setOrderAndRef = useCallback((o: Order) => {
    orderRef.current = o;
    setOrder(o);
  }, []);

  const appendTurn = useCallback((turn: Turn) => {
    logRef.current = [...logRef.current, turn];
    setTurnLog(logRef.current);
  }, []);

  // ---- direct engine helpers (fallback when Vapi is off) ----------------

  const getOrderKey = useCallback((): string => {
    try {
      let key = window.localStorage.getItem("hk.orderKey");
      if (!key) {
        key = `web-${crypto.randomUUID()}`;
        window.localStorage.setItem("hk.orderKey", key);
      }
      return key;
    } catch {
      return `web-${Date.now()}`;
    }
  }, []);

  const ensureOrder = useCallback(async (): Promise<Order> => {
    if (orderRef.current?.id) return orderRef.current;
    const created = await createOrder(getOrderKey());
    setOrderAndRef(created);
    return created;
  }, [getOrderKey, setOrderAndRef]);

  // ---- TTS (fallback path speaks in the browser) ------------------------

  const speak = useCallback((word: string): void => {
    if (!("speechSynthesis" in window)) return;
    const utter = new SpeechSynthesisUtterance(word);
    utter.lang = "en";
    utter.rate = 1;
    window.speechSynthesis.speak(utter);
  }, []);

  // ---- mic (fallback path uses the browser SpeechRecognition) -----------

  const ensureRecognition = useCallback((): SpeechRecognitionLike | null => {
    if (!supported) return null;
    if (recognitionRef.current) return recognitionRef.current;
    const SR = window.SpeechRecognition ?? window.webkitSpeechRecognition;
    if (!SR) {
      setSupported(false);
      return null;
    }
    const rec = new SR();
    rec.continuous = false;
    rec.interimResults = true;
    rec.lang = "en-PK";
    rec.onresult = (ev: SpeechRecognitionEventLike) => {
      let finalText = "";
      let interimText = "";
      for (let i = ev.resultIndex; i < ev.results.length; i += 1) {
        const res = ev.results[i];
        if (res.isFinal) finalText += res[0].transcript;
        else interimText += res[0].transcript;
      }
      if (interimText) setInterim(interimText);
      if (finalText.trim()) {
        setInterim("");
        void sendTurn(finalText.trim());
      }
    };
    rec.onerror = (ev: SpeechRecognitionErrorEventLike) => {
      if (ev.error === "not-allowed" || ev.error === "service-not-allowed") {
        setPhase("error");
        setErrorMessage("Microphone access was blocked. Allow the mic in your browser, then try again.");
      } else if (!stopListeningRef.current) {
        setPhase("error");
        setErrorMessage("The mic ran into a problem. Tap the mic to try again.");
      }
    };
    rec.onend = () => {
      if (!stopListeningRef.current && phaseRef.current === "listening") {
        try {
          rec.start();
        } catch {
          /* already running */
        }
      }
    };
    recognitionRef.current = rec;
    return rec;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [supported]);

  const phaseRef = useRef<Phase>("idle");
  phaseRef.current = phase;

  const startListening = useCallback(() => {
    const rec = ensureRecognition();
    if (!rec) return;
    stopListeningRef.current = false;
    setErrorMessage(null);
    setPhase("listening");
    try {
      rec.start();
    } catch {
      /* already running */
    }
  }, [ensureRecognition]);

  const stopListening = useCallback(() => {
    stopListeningRef.current = true;
    if (recognitionRef.current) {
      try {
        recognitionRef.current.stop();
      } catch {
        /* not running */
      }
    }
    setInterim("");
    setPhase("idle");
  }, []);

  // ---- conversation (fallback path: engine over SSE) --------------------

  const sendTurn = useCallback(
    async (text: string) => {
      stopListeningRef.current = true;
      if (recognitionRef.current) {
        try {
          recognitionRef.current.stop();
        } catch {
          /* not running */
        }
      }
      cancelSpeech();
      setPhase("thinking");
      appendTurn({ role: "user", text });
      try {
        const order = await ensureOrder();
        await streamChat(order.id, text, {
          onTurn: (updated) => {
            setOrderAndRef(updated);
            setPhase("thinking");
          },
          onWord: (w) => {
            setPhase("speaking");
            speak(w);
          },
          onDone: () => {
            setPhase("idle");
            setInterim("");
          },
        });
      } catch (err) {
        const message = err instanceof Error ? err.message : "The line dropped. Try again.";
        setErrorMessage(message);
        setPhase("error");
      }
    },
    [appendTurn, ensureOrder, setOrderAndRef, speak],
  );

  // ---- Vapi call (primary path when configured) -------------------------

  const startVapi = useCallback(async () => {
    if (!USE_VAPI) return;
    setErrorMessage(null);
    setPhase("listening");
    if (!vapiRef.current) {
      const vapi = new Vapi(VAPI_PUBLIC_KEY);
      vapi.on("call-start", () => {
        setPhase("listening");
      });
      vapi.on("speech-start", () => {
        setPhase("speaking");
      });
      vapi.on("speech-end", () => {
        setPhase("listening");
      });
      vapi.on("message", (msg: { type?: string; transcriptType?: string; role?: string; transcript?: string }) => {
        if (msg?.type === "transcript") {
          if (msg.role === "user" && msg.transcriptType === "final" && msg.transcript?.trim()) {
            appendTurn({ role: "user", text: msg.transcript.trim() });
          } else if (msg.role === "assistant" && msg.transcriptType === "partial") {
            setInterim(msg.transcript?.trim() ?? "");
          } else if (msg.role === "assistant" && msg.transcriptType === "final" && msg.transcript?.trim()) {
            setInterim("");
            appendTurn({ role: "agent", text: msg.transcript.trim() });
          }
        }
      });
      vapi.on("error", (e: { message?: string }) => {
        setErrorMessage(e?.message ?? "The Vapi call ran into a problem.");
        setPhase("error");
      });
      vapi.on("call-end", () => {
        setPhase("idle");
        setInterim("");
        callSpanRef.current = null;
      });
      vapiRef.current = vapi;
    }
    try {
      callSpanRef.current = [Date.now(), Date.now() + 60 * 60 * 1000];
      await vapiRef.current.start(VAPI_ASSISTANT);
    } catch (e) {
      setErrorMessage(e instanceof Error ? e.message : "Could not open the Vapi call.");
      setPhase("error");
    }
  }, [appendTurn]);

  const stopVapi = useCallback(async () => {
    if (vapiRef.current) {
      try {
        await vapiRef.current.stop();
      } catch {
        /* already ended */
      }
    }
    setPhase("idle");
  }, []);

  // ---- shared controls ---------------------------------------------------

  const handleMicClick = useCallback(async () => {
    if (USE_VAPI) {
      if (phase === "speaking" || phase === "listening") await stopVapi();
      else await startVapi();
      return;
    }
    if (phase === "speaking") {
      cancelSpeech();
      startListening(); // barge in: cut off the reply and take a new order
      return;
    }
    if (phase === "listening") {
      stopListening();
      return;
    }
    startListening();
  }, [phase, startListening, startVapi, stopListening, stopVapi]);

  const resetSession = useCallback(async () => {
    if (USE_VAPI) await stopVapi();
    cancelSpeech();
    stopListening();
    orderRef.current = null;
    setOrder(null);
    setTurnLog([]);
    logRef.current = [];
    setErrorMessage(null);
    callSpanRef.current = null;
    try {
      window.localStorage.removeItem("hk.orderKey");
    } catch {
      /* private mode */
    }
    setPhase("idle");
  }, [stopListening, stopVapi]);

  // ---- Vapi orders appear via the webhook; poll to show them ------------

  useEffect(() => {
    if (!USE_VAPI) return;
    let alive = true;
    let timer: number | undefined;
    async function poll() {
      try {
        const res = await fetch(`${API}/orders/active`, { cache: "no-store" });
        if (!res.ok) return;
        const data: Order[] = await res.json();
        if (!alive) return;
        const span = callSpanRef.current;
        const candidates = span
          ? data.filter((o) => {
              const t = o.created_at ? new Date(o.created_at).getTime() : 0;
              return t >= span[0] && t <= span[1];
            })
          : data;
        const sorted = candidates.sort(
          (a, b) => new Date(b.updated_at ?? 0).getTime() - new Date(a.updated_at ?? 0).getTime(),
        );
        if (sorted.length) setOrderAndRef(sorted[0]);
      } catch {
        /* transient; the next poll retries */
      }
    }
    void poll();
    timer = window.setInterval(poll, 4000);
    return () => {
      alive = false;
      if (timer) window.clearInterval(timer);
    };
  }, [setOrderAndRef]);

  useEffect(() => {
    setSupported(
      typeof window !== "undefined" &&
        Boolean(window.SpeechRecognition ?? window.webkitSpeechRecognition),
    );
  }, []);

  const totalLines = order?.lines ?? [];

  return (
    <div className="shell">
      <header className="topbar">
        <Link href="/" className="brand">
          Hukm Karo
          <small>Order by voice</small>
        </Link>
        <nav className="nav" aria-label="Primary">
          <Link href="/" className="navlink" aria-current="page">
            Voice demo
          </Link>
          <Link href="/kds" className="navlink">
            Kitchen
          </Link>
          <Link href={`${API}/docs`} className="navlink">
            API
          </Link>
        </nav>
      </header>

      <div className="pagehead">
        <h1>Place an order, hands free</h1>
        <span className="chip chip-ink">{USE_VAPI ? "Vapi call" : "Live demo"}</span>
      </div>

      <div className="voice-grid">
        <section className="panel talk-panel" aria-label="Voice conversation">
          <div className="mic-column">
            <button
              type="button"
              className={`mic is-${phase}`}
              onClick={() => void handleMicClick()}
              aria-label={
                phase === "listening" || phase === "speaking"
                  ? "End the call"
                  : "Start ordering by voice"
              }
            >
              <MicGlyph />
            </button>
            <div className="mic-status" role="status" aria-live="polite">
              {PHASE_LABEL[phase]}
            </div>
            <div className="mic-hint">{PHASE_HINT[phase]}</div>

            {USE_VAPI ? (
              <div className="banner banner-info" style={{ marginTop: 18 }}>
                Speech, language and listening run on the Vapi assistant. The order engine is
                still this server; tool calls arrive over the webhook.
              </div>
            ) : null}

            {!supported && !USE_VAPI ? (
              <div className="banner banner-info" style={{ marginTop: 18 }}>
                Speech recognition is not available in this browser. Type in the input below
                instead, or open this page in Chrome.
              </div>
            ) : null}

            {errorMessage ? (
              <div className="banner banner-error" style={{ marginTop: 18 }}>
                {errorMessage}
              </div>
            ) : null}
          </div>

          {!USE_VAPI ? (
            <div className="transcript" role="log" aria-live="polite" aria-label="Conversation">
              {turnLog.length === 0 ? (
                <p className="empty-transcript">
                  Your conversation will appear here as you order. Try saying something like
                  &ldquo;two chicken biryani and a cold drink for delivery&rdquo;.
                </p>
              ) : (
                turnLog.map((t, i) => (
                  <div key={i} className={`turn turn-${t.role}`}>
                    <div className="turn-label">{t.role === "user" ? "You" : "Hukm Karo"}</div>
                    <p>{t.text}</p>
                  </div>
                ))
              )}
              {interim ? <p className="interim">{interim}</p> : null}
            </div>
          ) : null}

          {!USE_VAPI && !supported ? (
            <TypedFallback onSend={sendTurn} disabled={phase === "thinking"} />
          ) : null}

          <div className="order-actions">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void handleMicClick()}
            >
              {USE_VAPI
                ? phase === "listening" || phase === "speaking"
                  ? "End call"
                  : "Start call"
                : phase === "listening"
                  ? "Stop"
                  : phase === "speaking"
                    ? "Interrupt"
                    : "Start"}
            </button>
            <button type="button" className="btn btn-ghost" onClick={() => void resetSession()}>
              Start over
            </button>
          </div>

          <p className="footer-note">
            {USE_VAPI
              ? "This call runs through the Vapi assistant. Your words are streamed to Vapi for speech recognition and back as speech."
              : "This is a prototype. Your words are heard only in this browser session."}
          </p>
        </section>

        <aside className="panel order-panel" aria-label="Order summary">
          <h2>Your order</h2>
          <div className="order-id">
            <strong>{order?.number ?? (USE_VAPI ? "On the line" : "New order")}</strong>
            {order ? (
              <span className="muted small">
                Status: {order.state.replace(/_/g, " ").toLowerCase()}
              </span>
            ) : (
              <span className="muted small">Nothing placed yet</span>
            )}
          </div>

          {totalLines.length === 0 ? (
            <p className="muted small">Add items by voice and they will appear here.</p>
          ) : (
            <>
              {totalLines.map((l) => (
                <div key={l.line_id} className="cart-line">
                  <span className="qty">{l.quantity}×</span>
                  <span className="name" title={l.name}>
                    {l.name}
                  </span>
                  <span className="price">{rupees(l.line_total)}</span>
                </div>
              ))}
              <div className="totals">
                <div className="row">
                  <span>Subtotal</span>
                  <span>{rupees(order?.subtotal)}</span>
                </div>
                <div className="row">
                  <span>Delivery</span>
                  <span>{rupees(order?.delivery_fee)}</span>
                </div>
                <div className="row grand">
                  <span>Total</span>
                  <span>{rupees(order?.total)}</span>
                </div>
              </div>
            </>
          )}
        </aside>
      </div>

      <footer className="pagefoot">
        Hukm Karo demo. The order engine runs at {API}. No fake numbers, no testimonials, just a
        working order line.
      </footer>
    </div>
  );
}

function MicGlyph() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="9" y="3" width="6" height="11" rx="3" stroke="currentColor" strokeWidth="1.8" />
      <path
        d="M5 11a7 7 0 0 0 14 0M12 18v3"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

function TypedFallback({ onSend, disabled }: { onSend: (t: string) => void; disabled: boolean }) {
  const [value, setValue] = useState("");
  return (
    <form
      className="typed"
      onSubmit={(e) => {
        e.preventDefault();
        if (!value.trim() || disabled) return;
        onSend(value.trim());
        setValue("");
      }}
    >
      <input
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="Type your order here"
        aria-label="Message"
        className="typed-input"
        disabled={disabled}
      />
      <button type="submit" className="btn btn-primary" disabled={disabled || !value.trim()}>
        Send
      </button>
    </form>
  );
}