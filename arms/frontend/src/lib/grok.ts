// Grok (xAI) over its OpenAI-compatible chat API, server side only. XAI_API_KEY in arms/frontend/.env.local (or the
// Vercel project env); PICKY_GROK_MODEL overrides the model (same knob as the team's /api/chat); XAI_BASE_URL points
// at another OpenAI-compatible endpoint (tests use a local mock).
export type GrokMsg = { role: "system" | "user" | "assistant" | "tool"; content: string; tool_calls?: GrokCall[]; tool_call_id?: string };
export type GrokCall = { id: string; type?: "function"; function: { name: string; arguments: string } };
export type GrokTool = { name: string; description: string; parameters: Record<string, unknown> };

// once xAI rejects the key, stop calling it for this server process (every answer would just fall back anyway)
let rejected = false;
export const hasGrok = () => !!process.env.XAI_API_KEY && !rejected;
export const grokStatus = (): "ready" | "rejected" | "missing" => (!process.env.XAI_API_KEY ? "missing" : rejected ? "rejected" : "ready");
export const grokModel = () => process.env.PICKY_GROK_MODEL || "grok-4.20-0309-non-reasoning";

export async function grok(messages: GrokMsg[], opts: { tools?: GrokTool[]; maxTokens?: number; timeoutMs?: number; json?: boolean } = {}) {
  const r = await fetch(`${process.env.XAI_BASE_URL || "https://api.x.ai/v1"}/chat/completions`, {
    method: "POST",
    headers: { Authorization: `Bearer ${process.env.XAI_API_KEY}`, "Content-Type": "application/json" },
    signal: AbortSignal.timeout(opts.timeoutMs ?? 12000),
    body: JSON.stringify({
      model: grokModel(),
      messages,
      max_tokens: opts.maxTokens ?? 600,
      temperature: 0.4,
      ...(opts.tools?.length ? { tools: opts.tools.map((t) => ({ type: "function", function: t })) } : {}),
      ...(opts.json ? { response_format: { type: "json_object" } } : {}),
    }),
  });
  if (!r.ok) {
    const body = (await r.text()).slice(0, 300);
    if (r.status === 400 || r.status === 401 || r.status === 403) if (/api key/i.test(body)) rejected = true;
    throw new Error(`grok ${r.status}: ${body}`);
  }
  return ((await r.json()) as { choices: { message: GrokMsg }[] }).choices[0].message;
}
