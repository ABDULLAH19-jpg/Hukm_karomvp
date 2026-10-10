"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { API, fetchBoard, rupees, type Board, type BoardAction, type Order } from "../../lib/api";

function elapsedSince(iso: string | null, now: number): string {
  if (!iso) return "unknown";
  const then = new Date(iso).getTime();
  const diff = Math.max(0, now - then);
  const m = Math.floor(diff / 60_000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function tickChime() {
  try {
    const Ctor = window.AudioContext ?? window.webkitAudioContext;
    if (!Ctor) return;
    const ctx = new Ctor();
    const note = (freq: number, at: number) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, ctx.currentTime + at);
      gain.gain.exponentialRampToValueAtTime(0.3, ctx.currentTime + at + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + at + 0.4);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(ctx.currentTime + at);
      osc.stop(ctx.currentTime + at + 0.45);
    };
    note(880, 0);
    note(1174, 0.12);
  } catch {
    /* audio not available, the card still shows */
  }
}

export default function KdsPage() {
  const [board, setBoard] = useState<Board | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [chimeOn, setChimeOn] = useState(true);
  const [now, setNow] = useState(Date.now());
  const [busy, setBusy] = useState<Record<number, boolean>>({});
  const seenRef = useRef<Set<string>>(new Set());

  const chimeOnRef = useRef(chimeOn);
  chimeOnRef.current = chimeOn;

  const refresh = useCallback(async (first: boolean) => {
    try {
      const data = await fetchBoard();
      setBoard(data);
      setError(null);
      if (!first && chimeOnRef.current && seenRef.current.size > 0) {
        const fresh = data.orders.filter((o) => !seenRef.current.has(o.number));
        if (fresh.length) tickChime();
      }
      seenRef.current = new Set(data.orders.map((o) => o.number));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not reach the order engine.");
    }
  }, []);

  useEffect(() => {
    void refresh(true);
    const poll = window.setInterval(() => void refresh(false), 4000);
    const clock = window.setInterval(() => setNow(Date.now()), 15_000);
    return () => {
      window.clearInterval(poll);
      window.clearInterval(clock);
    };
  }, [refresh]);

  const advance = useCallback(
    async (order: Order, action: BoardAction) => {
      setBusy((b) => ({ ...b, [order.id]: true }));
      try {
        const res = await fetch(`${API}/orders/${order.id}/transition`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ to: action.to, reason: `kitchen display: ${action.label}` }),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => null);
          throw new Error(body?.error?.message ?? "The order engine refused that move.");
        }
        await refresh(false);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not update the order.");
      } finally {
        setBusy((b) => ({ ...b, [order.id]: false }));
      }
    },
    [refresh],
  );

  const orders = board?.orders ?? [];
  const columns = board?.columns ?? [];

  return (
    <div className="shell">
      <header className="topbar">
        <Link href="/" className="brand">
          Hukm Karo
          <small>Kitchen display</small>
        </Link>
        <nav className="nav" aria-label="Primary">
          <Link href="/" className="navlink">
            Voice demo
          </Link>
          <Link href="/kds" className="navlink" aria-current="page">
            Kitchen
          </Link>
          <Link href={`${API}/docs`} className="navlink">
            API
          </Link>
        </nav>
      </header>

      <div className="pagehead">
        <h1>Kitchen</h1>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <span className="chip chip-ink" aria-live="polite">
            {board == null ? "Connecting" : `${orders.length} on the board`}
          </span>
          <button
            type="button"
            className="icon-btn"
            onClick={() => setChimeOn((c) => !c)}
            aria-pressed={chimeOn}
          >
            {chimeOn ? "Chime on" : "Chime off"}
          </button>
          <button type="button" className="icon-btn" onClick={() => void refresh(false)}>
            Refresh
          </button>
        </div>
      </div>

      {error ? (
        <div className="banner banner-error" role="alert">
          {error}
        </div>
      ) : null}

      {board == null ? (
        <div className="empty">
          <h2>Connecting to the order engine</h2>
          <p>Waiting for {API} so the kitchen can see live orders.</p>
          <button type="button" className="btn btn-primary" onClick={() => void refresh(true)}>
            Try again
          </button>
        </div>
      ) : orders.length === 0 ? (
        <div className="empty">
          <h2>Nothing on the board</h2>
          <p>
            An order appears here the moment the customer confirms it, with a chime. This screen
            updates on its own.
          </p>
        </div>
      ) : (
        <div className="kds-cols">
          {columns.map((col) => {
            const items = orders
              .filter((o) => col.states.includes(o.state))
              .sort(
                (a, b) =>
                  new Date(a.created_at ?? 0).getTime() - new Date(b.created_at ?? 0).getTime(),
              );
            return (
              <section key={col.key} className="kds-col" aria-label={`${col.title} orders`}>
                <div className="kds-col-head">
                  <h2>{col.title}</h2>
                  <span className="badge">{items.length}</span>
                </div>
                {items.length === 0 ? (
                  <p className="muted small" style={{ margin: 8 }}>
                    Empty
                  </p>
                ) : (
                  items.map((o) => (
                    <OrderCard
                      key={o.id}
                      order={o}
                      now={now}
                      busy={Boolean(busy[o.id])}
                      actions={board.actions[o.state] ?? []}
                      onAdvance={advance}
                    />
                  ))
                )}
              </section>
            );
          })}
        </div>
      )}

      <footer className="pagefoot">
        {board
          ? `Every order the kitchen can work on. Completed orders stay for ${board.completed_window_h} hours, then drop off. Orders still being taken stay with the agent until the customer confirms.`
          : "Every order the kitchen can work on, from the moment the customer confirms it."}
      </footer>
    </div>
  );
}

function OrderCard({
  order,
  now,
  actions,
  busy,
  onAdvance,
}: {
  order: Order;
  now: number;
  actions: BoardAction[];
  busy: boolean;
  onAdvance: (order: Order, action: BoardAction) => void;
}) {
  // Only a confirmed delivery order carries an address, so no address means it is collected here.
  const fulfilment = order.address ? `Delivery: ${order.address}` : "Pickup at the restaurant";
  const toneClass = (tone: BoardAction["tone"]): string =>
    tone === "primary" ? "btn-primary" : tone === "danger" ? "btn-danger" : "btn-ghost";

  return (
    <article className="kcard">
      <div className="kcard-head">
        <span className="kcard-num">{order.number}</span>
        <span className="kcard-time">{elapsedSince(order.created_at, now)}</span>
      </div>
      <ul className="kcard-lines">
        {order.lines.map((l) => (
          <li key={l.line_id}>
            {l.quantity} x {l.name}
            {l.options?.length ? (
              <ul className="kcard-options">
                {l.options.map((o, i) => (
                  <li key={`${l.line_id}-${i}`}>
                    {o.group}: {o.choice}
                  </li>
                ))}
              </ul>
            ) : null}
          </li>
        ))}
      </ul>
      <div className="kcard-total">{rupees(order.total)}</div>
      <div className="kcard-meta">{fulfilment}</div>
      {order.note ? <p className="kcard-note">{order.note}</p> : null}
      {actions.length ? (
        <div className="kcard-actions" aria-busy={busy}>
          {actions.map((a) => (
            <button
              key={a.to}
              type="button"
              className={`btn ${toneClass(a.tone)}`}
              disabled={busy}
              onClick={() => onAdvance(order, a)}
            >
              {a.label}
            </button>
          ))}
        </div>
      ) : null}
    </article>
  );
}
