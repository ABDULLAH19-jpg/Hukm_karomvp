// Thin client for the Hukm Karo order engine. The backend runs a FastAPI app
// on port 8000 during development.

export const API: string =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export interface Branch {
  id: string | null;
  name: string | null;
  radius_m: number | null;
}

/** One choice the customer made on a line. The price comes from the menu, never from a model. */
export interface LineOption {
  group: string;
  choice: string;
  price: string;
}

export interface OrderLine {
  line_id: string;
  item_id: number;
  name: string;
  quantity: number;
  options?: LineOption[];
  unit_price?: string;
  line_total: string;
}

export interface Order {
  id: number;
  number: string;
  state: string;
  created_at: string | null;
  updated_at: string | null;
  address: string | null;
  address_confirmed: boolean;
  address_precision?: string | null;
  address_place_id?: string | null;
  address_confidence?: number | null;
  distance_m?: number | null;
  branch: Branch | null;
  lines: OrderLine[];
  subtotal: string | null;
  delivery_fee: string | null;
  total: string | null;
  issues: string[];
  /** Why the order is where it is, set on board cards that need the reason in front of staff. */
  note?: string;
}

/** A button the board may offer. `to` is always a legal transition, asserted by the server's tests. */
export interface BoardAction {
  label: string;
  to: string;
  tone: "primary" | "danger" | "quiet";
}

export interface BoardColumn {
  key: string;
  title: string;
  states: string[];
}

/**
 * The kitchen board's whole contract. The server owns the columns and the buttons, so this screen
 * renders what it is handed instead of keeping a second copy that can drift out of date.
 */
export interface Board {
  columns: BoardColumn[];
  actions: Record<string, BoardAction[]>;
  orders: Order[];
  /** How long a completed order stays on the board, in hours. Owned by the server. */
  completed_window_h: number;
}

export class ApiError extends Error {
  code: string;
  status: number;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

async function throwError(res: Response): Promise<never> {
  let code = "request_failed";
  let message = `Request failed (${res.status})`;
  try {
    const body = await res.json();
    if (body?.error?.code) code = body.error.code;
    if (body?.error?.message) message = body.error.message;
  } catch {
    // Keep the defaults when the body is not JSON.
  }
  throw new ApiError(res.status, code, message);
}

/** Create an order once per browser session. The key makes it idempotent. */
export async function createOrder(idempotencyKey: string): Promise<Order> {
  const res = await fetch(`${API}/orders`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (!res.ok) await throwError(res);
  return res.json();
}

/** The kitchen board: its columns, its buttons, and the orders the kitchen can work on. */
export async function fetchBoard(): Promise<Board> {
  const res = await fetch(`${API}/orders/board`, { cache: "no-store" });
  if (!res.ok) await throwError(res);
  return res.json();
}

export interface StreamHandlers {
  onTurn: (order: Order) => void;
  onWord: (word: string) => void;
  onDone: () => void;
}

/** Send one user message and stream the assistant reply over SSE, word by word. */
export async function streamChat(
  orderId: number,
  message: string,
  h: StreamHandlers,
): Promise<void> {
  const res = await fetch(`${API}/orders/${orderId}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!res.ok) await throwError(res);
  if (!res.body) {
    h.onDone();
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let index = 0;

  async function pump(): Promise<void> {
    while (index < buffer.length) {
      const sep = buffer.indexOf("\n\n", index);
      if (sep === -1) {
        buffer = buffer.slice(index);
        index = 0;
        return;
      }
      handleFrame(buffer.slice(index, sep), h);
      index = sep + 2;
    }
    buffer = "";
    index = 0;
  }

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    await pump();
  }
  if (buffer.trim()) handleFrame(buffer, h);
}

function handleFrame(frame: string, h: StreamHandlers) {
  let event = "message";
  let data = "";
  for (const line of frame.split("\n")) {
    const trimmed = line.trim();
    if (trimmed.startsWith("event:")) event = trimmed.slice("event:".length).trim();
    else if (trimmed.startsWith("data:")) data = trimmed.slice("data:".length).trim();
  }
  if (!data) return;
  let payload: { ms?: number; order?: Order; w?: string } = {};
  try {
    payload = JSON.parse(data);
  } catch {
    return;
  }
  if (event === "turn" && payload.order) h.onTurn(payload.order);
  else if (event === "word") h.onWord(String(payload.w ?? ""));
  else if (event === "done") h.onDone();
}

/** Format a decimal price string as rupees. Unknown values render as an en dash. */
export function rupees(s: string | null | undefined): string {
  if (s == null || s === "") return "–";
  const n = Number(s);
  if (!Number.isFinite(n)) return s;
  return "Rs " + n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

/** Stop the assistant's speech (barge-in) and reset the TTS queue. */
export function cancelSpeech(): void {
  if ("speechSynthesis" in window) {
    window.speechSynthesis.cancel();
  }
}