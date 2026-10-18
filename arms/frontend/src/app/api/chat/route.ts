// Ask Picky: shopping + money assistant, now with the Cart Whisperer.
// Provider order: Gemini (GEMINI_API_KEY, the repo's default LLM; also understands spoken audio natively)
// → Claude (ANTHROPIC_API_KEY) → the offline responder. All three answer from the deterministic tools in
// lib/assistant/tools.ts, so the demo never dead-ends and every number comes from code, never the model.
// Stand-in until the gateway exposes the agent arm over HTTP; then this route should proxy there.
import Anthropic from "@anthropic-ai/sdk";
import { betaTool } from "@anthropic-ai/sdk/helpers/beta/json-schema";
import { maya, type Purchase } from "@/lib/data";
import { geminiChat, type GeminiTool, type JsonSchema } from "@/lib/assistant/gemini";
import { budgetStatus, canIAfford, cartWhisperer, findProducts, findSavings, localAnswer, priceCheck, shopperContext, spendingSummary, type Card, type ToolResult } from "@/lib/assistant/tools";

type ChatMsg = { role: "user" | "assistant"; content: string };
type Body = { messages: ChatMsg[]; extraPurchases?: Purchase[]; audio_b64?: string };

const SYSTEM = `You are Picky, Maya's personal shopping and money assistant, and the voice of her Cart Whisperer. You help her decide what to buy, where to buy it, whether a price is good, what to skip or wait on, and how her spending is tracking.
Today is Sunday, October 18, 2026. Maya shops at Picky Market (groceries, The Ave), a T-Mobile store, Picky Online, and other retailers you can see through the tools.
Rules:
- Every number you state (prices, totals, savings, budget) must come from a tool result in this conversation. Never estimate money yourself.
- For any question about what to buy, skip, wait on, add to a cart or trip — groceries OR the T-Mobile store — call cart_whisperer with her request; it returns the deterministic advice cards (SKIP / WAIT / optional stops) with reason codes, signals and the mandate verdict. Present those cards as Picky's advice; never invent a card, a price or a reason yourself.
- If she asks what you know about her or why you suggested something, call shopper_context and answer from it.
- Prefer one or two tool calls, then answer. Lead with the answer in the first sentence.
- Be honest: if waiting, a cheaper store, or not buying is better, say so plainly. Nothing is sponsored.
- Keep replies to 2–4 short sentences of plain text (no markdown headers, no bullet lists). The app shows cards for the tool results, so don't repeat whole tables.
- The newest user message may be spoken AUDIO instead of text: answer it directly from what you hear; never mention transcripts.
- You can't place orders or move money; if she wants to buy, tell her to add it to a robot order or check out from the Find page.`;

export async function POST(req: Request) {
  const body = (await req.json()) as Body;
  const purchases: Purchase[] = [...maya.purchases, ...(body.extraPurchases ?? [])];
  const last = body.messages[body.messages.length - 1]?.content ?? "";
  const audio = (body.audio_b64 ?? "").trim() || undefined;
  const cards: Card[] = [];
  const track = (r: ToolResult) => {
    if (r.card) cards.push(r.card);
    return r.summary;
  };
  const offline = () => localAnswer(last, purchases);

  type ToolDef = { name: string; description: string; schema: JsonSchema; run: (args: Record<string, unknown>) => Promise<string> };
  const TOOLS: ToolDef[] = [
    {
      name: "cart_whisperer",
      description: "Picky's Cart Whisperer pass for a shopping request: the list with prices and sourcing, SKIP / WAIT advice cards with savings, optional detour stops with reasons and signals, the cart total vs budget, and the deterministic mandate verdict with reason codes. Works for groceries (e.g. 'tacos for 6, under $40') and the T-Mobile wireless store (e.g. 'case and screen protector for my new Pixel 9, under $90', 'earbuds for running').",
      schema: { type: "object", properties: { request: { type: "string", description: "The shopper's request in her words" } }, required: ["request"] },
      run: async ({ request }) => track(cartWhisperer(String(request ?? last))),
    },
    {
      name: "shopper_context",
      description: "Everything the Cart Whisperer may look at for Maya: preferences, owned devices, phone installments left, usual purchases, budgets and mandate rules (grocery + tech).",
      schema: { type: "object", properties: {} },
      run: async () => track(shopperContext()),
    },
    {
      name: "spending_summary",
      description: "Maya's spending by category for a period, with trip count and biggest line items.",
      schema: { type: "object", properties: { period: { type: "string", enum: ["this_month", "last_month", "last_30_days", "last_12_weeks"] } } },
      run: async ({ period }) => track(spendingSummary(purchases, String(period ?? "this_month"))),
    },
    {
      name: "budget_status",
      description: "This month's grocery budget, spend so far, pace and projection, plus her spending-mandate limits.",
      schema: { type: "object", properties: {} },
      run: async () => track(budgetStatus(purchases)),
    },
    {
      name: "price_check",
      description: "Compare one product across every retailer (stores, online, warehouse, open-box, delivery) and judge whether today's price is good, using 13 weeks of price history and announced sales. Pass a product name or SKU.",
      schema: { type: "object", properties: { product: { type: "string" } }, required: ["product"] },
      run: async ({ product }) => track(priceCheck(String(product ?? ""))),
    },
    {
      name: "search_products",
      description: "Search groceries and wireless/electronics products by keywords. Returns SKUs and best prices.",
      schema: { type: "object", properties: { query: { type: "string" } }, required: ["query"] },
      run: async ({ query }) => track(findProducts(String(query ?? ""))),
    },
    {
      name: "find_savings",
      description: "Concrete ways to spend less on what Maya already buys (cheaper retailer, waiting for an announced sale), with estimated monthly savings.",
      schema: { type: "object", properties: {} },
      run: async () => track(findSavings(purchases)),
    },
    {
      name: "can_i_afford",
      description: "Whether a purchase fits Maya's remaining budget (groceries) or her situation (tech: owned items, phone installments). amount_cents is integer cents.",
      schema: { type: "object", properties: { amount_cents: { type: "integer" }, what: { type: "string" } }, required: ["amount_cents", "what"] },
      run: async ({ amount_cents, what }) => track(canIAfford(purchases, Math.max(0, Math.round(Number(amount_cents) || 0)), String(what ?? ""))),
    },
  ];

  // 1) Gemini (repo default; the only path that understands the spoken audio)
  if (process.env.GEMINI_API_KEY) {
    const history = body.messages.slice(-12).map((m) => ({ role: m.role, content: m.content }));
    const gTools: GeminiTool[] = TOOLS.map((t) => ({ name: t.name, description: t.description, parameters: t.schema, run: t.run }));
    const g = await geminiChat({ system: SYSTEM, tools: gTools, messages: audio && !last ? [...history, { role: "user", content: "" }] : history, audioWavB64: audio });
    if (g.ok) return Response.json({ text: g.text || "Done.", cards, source: "gemini", model: g.model });
    console.error(`[chat] Gemini failed: ${g.error}${process.env.ANTHROPIC_API_KEY ? "; trying Claude" : "; answering offline"}`);
  }

  // 2) Claude (text only)
  if (process.env.ANTHROPIC_API_KEY || process.env.ANTHROPIC_AUTH_TOKEN) {
    try {
      // the shared plain schemas feed Gemini directly; Claude's betaTool wants its own JSONSchema shape
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const tools = TOOLS.map((t) => betaTool({ name: t.name, description: t.description, inputSchema: t.schema as any, run: t.run as any }));
      const client = new Anthropic();
      const final = await client.beta.messages.toolRunner({
        model: "claude-opus-5-5",
        max_tokens: 4000,
        max_iterations: 6,
        system: SYSTEM,
        output_config: { effort: "low" },
        betas: ["server-side-fallback-2026-07-01"],
        fallbacks: "default",
        tools,
        messages: body.messages.slice(-12).map((m) => ({ role: m.role, content: m.content })),
      });
      if (final.stop_reason === "refusal") {
        return Response.json({ text: "I can't help with that one, but I can check prices, compare stores, or look at your spending.", cards, source: "claude" });
      }
      const text = final.content
        .filter((b) => b.type === "text")
        .map((b) => (b as { text: string }).text)
        .join("\n")
        .trim();
      return Response.json({ text: text || "Done.", cards, source: "claude" });
    } catch (err) {
      if (err instanceof Anthropic.AuthenticationError || err instanceof Anthropic.PermissionDeniedError) {
        console.error("[chat] Anthropic credentials rejected; answering offline");
      } else if (err instanceof Anthropic.RateLimitError) {
        console.error("[chat] rate limited; answering offline");
      } else if (err instanceof Anthropic.APIError) {
        console.error(`[chat] API error ${err instanceof Anthropic.APIError ? err.status : "?"}: ${err instanceof Error ? err.message : err}; answering offline`);
      } else {
        console.error("[chat] unexpected error; answering offline", err);
      }
    }
  }

  // 3) Offline: same tools, deterministic routing
  return Response.json({ ...offline(), source: "offline" });
}
