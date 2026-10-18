// Magenta's brain for the in-store conversation (T-Mobile "In store" tab).
// XAI_API_KEY set: Grok reads the conversation, calls the deterministic tools in lib/associate.ts and phrases a short
// spoken reply. Every dollar amount in that reply must appear in a tool result from this turn, or we answer with the
// offline brain instead (same tools, templates). No key, a timeout or any error: offline. The client also falls back
// to the offline brain locally if this route can't be reached, so the robot never goes silent.
import { applyActions, chipsFor, conversationDevice, respond, tools, type AssocAction, type AssocCtx, type AssocReply, type ToolResult } from "@/lib/associate";
import { grok, hasGrok, type GrokMsg, type GrokTool } from "@/lib/grok";
import { tmobileProducts } from "@/data/stores/tmobile";

type In = { messages: { role: "user" | "assistant"; content: string }[]; ctx: AssocCtx };

const SKUS = tmobileProducts.map((p) => `${p.sku}=${p.name}`).join("; ");

const SYSTEM = `You are Magenta, the robot sales associate standing on the floor of the T-Mobile store on University Way NE in Seattle. You talk out loud with shoppers who walked up to you, and your words are spoken by a voice, so:
- Reply in 1 to 3 short, warm, natural sentences. Plain speech: no lists, no markdown, no emoji, no SKUs, no "GB" unless asked.
- Every price, saving, date or count you say must come from a tool result in THIS turn. Never guess money. If you don't know, say so.
- Use tools before answering product questions. To move the shopper, call where_is (you walk them there) or add_to_basket (you fetch boxed phones from the back yourself).
- Be the honest associate: say when to skip something (they already own it, it doesn't fit) or wait (a payoff or an expected promo). Never upsell. Nothing is sponsored. Line offers are bill credits that need a new line or upgrade, never a shelf price.
- For "should I upgrade?" use upgrade_advice; for "am I on the right plan?" use plan_check; for "which phone should I get?" use phone_match. They are honest by design: say "wait" or "keep it" when they do.
- Account changes (plans, bills, porting a number, activation, unlocking, trade-in values) need a human: call call_expert and say so.
- Only use the shopper's account (their phone, payments, what they own) if my_account returns it; otherwise they haven't checked in.
- Phones on the tables are demo units; the boxed stock is in the back and only you or staff can get it.
- Today is Saturday, October 17, 2026. Prices are from t-mobile.com that day; the account and the promo calendar are synthetic demo data.
Catalog (sku=name): ${SKUS}`;

const sku = { type: "string", description: "product SKU from the catalog" };
const DEFS: GrokTool[] = [
  { name: "search_products", description: "Search this store's catalog by words (product type, brand, phone model, use). Returns matches with price, stock, where they are and what they fit.", parameters: { type: "object", properties: { query: { type: "string" } }, required: ["query"] } },
  { name: "product_details", description: "Price, sale, stock, location, fit, description, line offer and expected promo for one product.", parameters: { type: "object", properties: { sku }, required: ["sku"] } },
  { name: "where_is", description: "Walk the shopper to where a product is displayed (or fetch it if it's only in the back room).", parameters: { type: "object", properties: { sku }, required: ["sku"] } },
  { name: "accessories_for", description: "In-stock accessories made for a phone, cheapest first.", parameters: { type: "object", properties: { device_sku: sku, category: { type: "string", enum: ["case", "screen", "charger"] } }, required: ["device_sku"] } },
  { name: "check_fit", description: "Does an accessory fit a phone? PX7 is the Pixel 7 the shopper owns (if checked in).", parameters: { type: "object", properties: { accessory_sku: sku, device_sku: sku }, required: ["accessory_sku", "device_sku"] } },
  { name: "my_account", description: "The checked-in shopper's phone, payments left, lines and what they own (empty if not checked in).", parameters: { type: "object", properties: {} } },
  { name: "basket_advice", description: "The shopper's basket, total vs budget, and Cart Whisperer's verdicts (skip / wait / swap / complete).", parameters: { type: "object", properties: {} } },
  { name: "deals", description: "Shelf deals in stock, line offers and the promo calendar.", parameters: { type: "object", properties: {} } },
  { name: "add_to_basket", description: "Add to the basket (checks fit; refuses an accessory that doesn't fit unless force=true after the shopper insists). Boxed phones are fetched from the back.", parameters: { type: "object", properties: { sku, qty: { type: "integer", minimum: 1, maximum: 5 }, force: { type: "boolean" } }, required: ["sku"] } },
  { name: "remove_from_basket", description: "Take something out of the basket.", parameters: { type: "object", properties: { sku }, required: ["sku"] } },
  { name: "upgrade_advice", description: "The Timekeeper: honest 'should I upgrade now?' for the checked-in shopper (payoff, promo calendar, trade-in decay, phone health) with today-vs-waiting costs.", parameters: { type: "object", properties: {} } },
  { name: "plan_check", description: "Household Mix: is the checked-in shopper's household on the cheapest plan setup that fits every line's usage?", parameters: { type: "object", properties: {} } },
  { name: "phone_match", description: "The Matchmaker: best phone + plan bundles for what the shopper cares about and a monthly budget.", parameters: { type: "object", properties: { priorities: { type: "array", items: { type: "string", enum: ["price", "camera", "battery", "performance", "size"] } }, budget_cents: { type: "integer" }, lines: { type: "integer", minimum: 1, maximum: 5 } }, required: ["priorities", "budget_cents"] } },
  { name: "call_expert", description: "Page a human T-Mobile expert to the shopper.", parameters: { type: "object", properties: { reason: { type: "string" } }, required: ["reason"] } },
  { name: "start_checkout", description: "Shopper wants to pay: start checkout at the counter.", parameters: { type: "object", properties: {} } },
];

function run(name: string, a: Record<string, unknown>, ctx: AssocCtx): ToolResult {
  const s = (k: string) => String(a[k] ?? "");
  switch (name) {
    case "search_products": return tools.search_products(s("query"));
    case "product_details": return tools.product_details(s("sku"), ctx);
    case "where_is": return tools.where_is(s("sku"));
    case "accessories_for": return tools.accessories_for(s("device_sku"), a.category ? s("category") : undefined);
    case "check_fit": return tools.check_fit(s("accessory_sku"), s("device_sku"));
    case "my_account": return tools.my_account(ctx);
    case "basket_advice": return tools.basket_advice(ctx);
    case "deals": return tools.deals();
    case "add_to_basket": return tools.add_to_basket(s("sku"), Math.max(1, Math.min(5, Number(a.qty) || 1)), ctx, !!a.force);
    case "remove_from_basket": return tools.remove_from_basket(s("sku"), ctx);
    case "upgrade_advice": return tools.upgrade_advice(ctx);
    case "plan_check": return tools.plan_check(ctx);
    case "phone_match": return tools.phone_match(Array.isArray(a.priorities) ? (a.priorities as string[]) : [], Number(a.budget_cents) || 10000, Number(a.lines) || 1);
    case "call_expert": return tools.call_expert(s("reason") || "asked for a person");
    case "start_checkout": return tools.start_checkout(ctx);
    default: return { summary: `unknown tool ${name}` };
  }
}

const dollars = (s: string) => (s.match(/\$\s?\d[\d,]*(?:\.\d+)?/g) ?? []).map((d) => Number(d.replace(/[$,\s]/g, "")));

async function grokTurn(body: In): Promise<AssocReply> {
  const ctx: AssocCtx = { ...body.ctx, cart: [...body.ctx.cart], device: conversationDevice(body.messages.at(-1)?.content ?? "", body.ctx) };
  const facts: string[] = [];
  const actions: AssocAction[] = [];
  let focus = ctx.focus ?? null;
  const state = `Shopper ${ctx.known ? "checked in as Maya" : "not checked in"}. Basket: ${ctx.cart.map((l) => `${l.qty}×${l.sku}`).join(", ") || "empty"}. Budget $${(ctx.budget_cents / 100).toFixed(2)}. Last product discussed: ${focus ?? "none"}. Phone we're shopping for: ${ctx.device ?? "not said yet"}.`;
  const convo: GrokMsg[] = [{ role: "system", content: `${SYSTEM}\n\nRight now: ${state}` }, ...body.messages.slice(-10)];
  for (let i = 0; i < 5; i++) {
    const msg = await grok(convo, { tools: DEFS, maxTokens: 400 });
    if (!msg.tool_calls?.length) {
      const text = (msg.content ?? "").replace(/[*_#`]/g, "").trim();
      if (!text) throw new Error("empty reply");
      // the trust check: every $ amount must come from this turn's tool results
      const allowed = new Set(dollars(facts.join("\n")).map((d) => d.toFixed(2)));
      const bad = dollars(text).filter((d) => !allowed.has(d.toFixed(2)));
      if (bad.length) throw new Error(`unsupported amounts ${bad.join(", ")}`);
      return { text, actions, focus, device: ctx.device ?? null, chips: chipsFor({ ...ctx, focus }, actions), source: "grok" };
    }
    convo.push({ role: "assistant", content: msg.content ?? "", tool_calls: msg.tool_calls });
    for (const c of msg.tool_calls) {
      let res: ToolResult;
      try {
        res = run(c.function.name, JSON.parse(c.function.arguments || "{}"), ctx);
      } catch (e) {
        res = { summary: `error: ${String(e)}` };
      }
      facts.push(res.summary);
      actions.push(...(res.actions ?? []));
      ctx.cart = applyActions(ctx.cart, res.actions ?? []);
      if (res.focus) focus = res.focus;
      convo.push({ role: "tool", tool_call_id: c.id, content: res.summary });
    }
  }
  throw new Error("too many tool rounds");
}

export async function POST(req: Request) {
  let body: In;
  try {
    body = (await req.json()) as In;
  } catch {
    return Response.json({ error: "bad json" }, { status: 400 });
  }
  const last = body.messages?.at(-1)?.content ?? "";
  const ctx: AssocCtx = { cart: body.ctx?.cart ?? [], budget_cents: body.ctx?.budget_cents ?? 10000, focus: body.ctx?.focus ?? null, device: body.ctx?.device ?? null, known: !!body.ctx?.known, declined: body.ctx?.declined ?? [] };
  if (hasGrok() && last.trim()) {
    try {
      return Response.json(await grokTurn({ messages: body.messages, ctx }));
    } catch (e) {
      console.warn("[associate] grok failed, answering offline:", String(e).slice(0, 200));
    }
  }
  return Response.json(respond(last, ctx));
}

export async function GET() {
  return Response.json({ brain: hasGrok() ? "grok" : "offline" });
}
