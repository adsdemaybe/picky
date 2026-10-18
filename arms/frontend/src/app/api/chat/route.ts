// Ask Picky: shopping + money assistant.
// With Anthropic credentials (ANTHROPIC_API_KEY in arms/frontend/.env.local), Claude answers using the deterministic
// tools in lib/assistant/tools.ts. Without credentials, or if the API call fails, the offline responder answers from
// the same tools, so the demo never dead-ends.
// Stand-in until the gateway exposes the agent arm over HTTP; then this route should proxy there.
import Anthropic from "@anthropic-ai/sdk";
import { betaTool } from "@anthropic-ai/sdk/helpers/beta/json-schema";
import { maya, type Purchase } from "@/lib/data";
import { budgetStatus, canIAfford, findProducts, findSavings, localAnswer, priceCheck, spendingSummary, type Card } from "@/lib/assistant/tools";

type ChatMsg = { role: "user" | "assistant"; content: string };

const SYSTEM = `You are Picky, Maya's personal shopping and money assistant. You help her decide what to buy, where to buy it, whether a price is good, and how her spending is tracking.
Today is Saturday, October 17, 2026. Maya shops at Picky Market (groceries, The Ave), a T-Mobile store, Picky Online, and other retailers you can see through the tools.
Rules:
- Every number you state (prices, totals, savings, budget) must come from a tool result in this conversation. Never estimate money yourself.
- Prefer one or two tool calls, then answer. Lead with the answer in the first sentence.
- Be honest: if waiting, a cheaper store, or not buying is better, say so plainly. Nothing is sponsored.
- Keep replies to 2–4 short sentences of plain text (no markdown headers, no bullet lists). The app shows cards for the tool results, so don't repeat whole tables.
- You can't place orders or move money; if she wants to buy, tell her to add it to a robot order or check out from the Find page.`;

export async function POST(req: Request) {
  const body = (await req.json()) as { messages: ChatMsg[]; extraPurchases?: Purchase[] };
  const purchases: Purchase[] = [...maya.purchases, ...(body.extraPurchases ?? [])];
  const last = body.messages[body.messages.length - 1]?.content ?? "";
  const cards: Card[] = [];
  const track = (r: { summary: string; card?: Card }) => {
    if (r.card) cards.push(r.card);
    return r.summary;
  };

  const hasCreds = !!(process.env.ANTHROPIC_API_KEY || process.env.ANTHROPIC_AUTH_TOKEN);
  if (!hasCreds) return Response.json({ ...localAnswer(last, purchases), source: "offline" });

  const tools = [
    betaTool({
      name: "spending_summary",
      description: "Maya's spending by category for a period, with trip count and biggest line items.",
      inputSchema: {
        type: "object",
        properties: { period: { type: "string", enum: ["this_month", "last_month", "last_30_days", "last_12_weeks"] } },
        required: ["period"],
        additionalProperties: false,
      },
      run: async ({ period }) => track(spendingSummary(purchases, String(period ?? "this_month"))),
    }),
    betaTool({
      name: "budget_status",
      description: "This month's grocery budget, spend so far, pace and projection, plus her spending-mandate limits.",
      inputSchema: { type: "object", properties: {}, additionalProperties: false },
      run: async () => track(budgetStatus(purchases)),
    }),
    betaTool({
      name: "price_check",
      description: "Compare one product across every retailer (stores, online, warehouse, open-box, delivery) and judge whether today's price is good, using 13 weeks of price history and announced sales. Pass a product name or SKU.",
      inputSchema: { type: "object", properties: { product: { type: "string" } }, required: ["product"], additionalProperties: false },
      run: async ({ product }) => track(priceCheck(product)),
    }),
    betaTool({
      name: "search_products",
      description: "Search groceries and wireless/electronics products by keywords. Returns SKUs and best prices.",
      inputSchema: { type: "object", properties: { query: { type: "string" } }, required: ["query"], additionalProperties: false },
      run: async ({ query }) => track(findProducts(query)),
    }),
    betaTool({
      name: "find_savings",
      description: "Concrete ways to spend less on what Maya already buys (cheaper retailer, waiting for an announced sale), with estimated monthly savings.",
      inputSchema: { type: "object", properties: {}, additionalProperties: false },
      run: async () => track(findSavings(purchases)),
    }),
    betaTool({
      name: "can_i_afford",
      description: "Whether a purchase fits Maya's remaining budget (groceries) or her situation (tech: owned items, phone installments). amount_cents is integer cents.",
      inputSchema: {
        type: "object",
        properties: { amount_cents: { type: "integer", minimum: 0 }, what: { type: "string" } },
        required: ["amount_cents", "what"],
        additionalProperties: false,
      },
      run: async ({ amount_cents, what }) => track(canIAfford(purchases, Math.max(0, Math.round(Number(amount_cents) || 0)), String(what ?? ""))),
    }),
  ];

  try {
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
      console.error(`[chat] API error ${err.status}: ${err.message}; answering offline`);
    } else {
      console.error("[chat] unexpected error; answering offline", err);
    }
    return Response.json({ ...localAnswer(last, purchases), source: "offline" });
  }
}
