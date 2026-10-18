// Ask Picky's wireless tools (server side, used by app/api/chat/route.ts). Each one calls the gateway's wireless arm
// (/api/wireless/*): plans, household optimizer, trade-in, financing, compatible accessories, full quote. The numbers
// come only from there ("the LLM proposes; code decides"); with no gateway the tool says so instead of guessing.
const BASE = (process.env.NEXT_PUBLIC_GATEWAY_URL ?? "").trim().replace(/\/$/, "");

async function call(path: string, body?: unknown): Promise<string> {
  if (!BASE) return "UNAVAILABLE: the backend isn't configured (NEXT_PUBLIC_GATEWAY_URL), so no wireless estimate is possible. Say so; don't estimate.";
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 8000);
  try {
    const r = await fetch(BASE + path, {
      method: body === undefined ? "GET" : "POST",
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctl.signal,
      cache: "no-store",
    });
    const text = await r.text();
    if (!r.ok) return `ERROR ${r.status}: ${text.slice(0, 400)}`;
    // money fields are integer cents; the model is told to format them as dollars
    return text.length > 6000 ? `${text.slice(0, 6000)}…(truncated)` : text;
  } catch (e) {
    return `UNAVAILABLE: the backend didn't answer (${(e as Error).message}). Say so; don't estimate.`;
  } finally {
    clearTimeout(timer);
  }
}

const q = (o: Record<string, string | number | boolean | undefined>) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(o)) if (v !== undefined && v !== "") p.set(k, String(v));
  return p.toString() ? `?${p}` : "";
};

export const wirelessTools = {
  comparePlans: (lines: number, user_id = "maya", autopay = true) => call(`/api/wireless/plans${q({ lines, user_id, autopay })}`),
  household: (user_id = "maya") => call("/api/wireless/household", { user_id }),
  tradeIn: (model_id: string, condition: string, target_sku?: string, plan_id?: string, user_id = "maya") =>
    call("/api/wireless/trade-in", { model_id, condition, user_id, target_sku: target_sku || null, plan_id: plan_id || null }),
  financing: (sku: string, months = 24, plan_id?: string, new_line = false, trade_in = false, port_in = false) =>
    call("/api/wireless/financing", { sku, months, plan_id: plan_id || null, new_line, trade_in, port_in }),
  accessories: (device_sku: string) => call(`/api/wireless/compatible/${encodeURIComponent(device_sku)}${q({ store_id: "store_tmobile" })}`),
  quote: (body: unknown) => call("/api/wireless/quote", body),
};
