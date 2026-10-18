// Cart Whisperer phrasing (docs/cart-whisperer.md §5 step 4): the cards are already chosen, ordered and priced by
// deterministic code (lib/whisperer.ts). An LLM (Grok when XAI_API_KEY is set, else Claude) only rewrites each `reason`
// from the card's derived signals. Every
// number in a rewritten reason must already appear in that card's own text or signals, or the template is kept.
// No credentials, a timeout, or bad JSON → templates for all (source "template"). Interim, like /api/chat, until the
// recommender's suggest() runs behind the gateway.
import Anthropic from "@anthropic-ai/sdk";
import { grok, hasGrok } from "@/lib/grok";

type In = { id: string; kind: string; title: string; reason: string; reason_codes: string[]; signals: Record<string, string | number> };

const SYSTEM = `You phrase checkout suggestions for a phone-store shopping assistant. Each card is already decided by code.
For each card, rewrite "reason" as ONE friendly sentence (max 30 words) in second person.
Rules: use only facts from that card's title, reason, reason_codes and signals; never invent numbers, dates, prices or product names; keep every dollar amount exactly as written; no exclamation marks; never say "sponsored".
Reply with JSON only: {"reasons": {"<id>": "<sentence>"}}`;

const nums = (s: string) => (s.match(/\d+(?:[.,]\d+)?/g) ?? []).map((n) => n.replace(/,/g, ""));

function valid(card: In, text: string) {
  if (!text || text.length > 260) return false;
  const allowed = new Set([...nums(card.reason), ...nums(card.title), ...Object.values(card.signals).flatMap((v) => nums(String(v))), ...Object.values(card.signals).flatMap((v) => (typeof v === "number" ? nums((v / 100).toFixed(2)) : []))]);
  return nums(text).every((n) => allowed.has(n));
}

async function phrase(cards: In[]): Promise<{ text: string; by: "grok" | "claude" }> {
  if (hasGrok()) {
    const msg = await grok(
      [
        { role: "system", content: SYSTEM },
        { role: "user", content: JSON.stringify(cards) },
      ],
      { maxTokens: 600, timeoutMs: 4000, json: true },
    );
    return { text: msg.content ?? "", by: "grok" };
  }
  const client = new Anthropic();
  const msg = await client.messages.create(
    {
      model: "claude-sonnet-5-5",
      max_tokens: 600,
      system: SYSTEM,
      messages: [{ role: "user", content: JSON.stringify(cards) }],
    },
    { timeout: 4000 },
  );
  return { text: msg.content.map((b) => (b.type === "text" ? b.text : "")).join(""), by: "claude" };
}

export async function POST(req: Request) {
  const { cards } = (await req.json()) as { cards: In[] };
  const hasCreds = hasGrok() || !!(process.env.ANTHROPIC_API_KEY || process.env.ANTHROPIC_AUTH_TOKEN);
  if (!hasCreds || !cards?.length) return Response.json({ reasons: {}, source: "template" });
  try {
    const { text, by } = await phrase(cards);
    const parsed = JSON.parse(text.slice(text.indexOf("{"), text.lastIndexOf("}") + 1)) as { reasons: Record<string, string> };
    const reasons: Record<string, string> = {};
    for (const c of cards) if (valid(c, parsed.reasons?.[c.id] ?? "")) reasons[c.id] = parsed.reasons[c.id];
    return Response.json({ reasons, source: Object.keys(reasons).length ? by : "template" });
  } catch {
    return Response.json({ reasons: {}, source: "template" });
  }
}
