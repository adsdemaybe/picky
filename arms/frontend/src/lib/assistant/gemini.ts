// Gemini REST client for Ask Picky (no SDK): one hand-written tool loop, the same shape the backend's
// utils.llm runs. Model fallback mirrors the repo convention (utils/gemini fallbacks, quotas on demo day):
// try the configured/fast models in order, next one on 429 / 503 / quota errors. Audio arrives as base64
// WAV and is sent inline — Gemini hears the question and answers it directly.
export type JsonSchema = {
  type: "object";
  properties: Record<string, JsonSchema | { type: string; enum?: string[]; description?: string }>;
  required?: string[];
  additionalProperties?: boolean;
};
export type GeminiTool = { name: string; description: string; parameters: JsonSchema; run: (args: Record<string, unknown>) => Promise<string> | string };
export type GeminiPart = { text?: string; inlineData?: { mimeType: string; data: string }; functionCall?: { name: string; args?: Record<string, unknown> }; functionResponse?: { name: string; response: Record<string, unknown> } };
type Content = { role: "user" | "model"; parts: GeminiPart[] };

const BASE = "https://generativelanguage.googleapis.com/v1beta/models";
const MAX_TURNS = 7;

function defaultModels(): string[] {
  const override = (process.env.GEMINI_MODEL ?? "").trim();
  if (override) return [override];
  return ["gemini-3.8-flash", "gemini-3-flash-preview", "gemini-3.5-flash-lite", "gemini-2.5-flash"];
}

/** OpenAI-ish lowercase schema (what the tools declare) → Gemini's uppercase Type enum. */
function toGeminiSchema(s: JsonSchema): Record<string, unknown> {
  const up = (t: string | undefined) => (t ? t.replace(/^(string|object|integer|number|boolean|array)$/, (m) => m.toUpperCase()) : undefined);
  const props: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(s.properties)) {
    if (v && typeof v === "object" && "type" in v) {
      const p = v as { type: string; enum?: string[]; description?: string };
      props[k] = { type: up(p.type), ...(p.enum ? { enum: p.enum } : {}), ...(p.description ? { description: p.description } : {}) };
    }
  }
  return { type: "OBJECT", properties: props, ...(s.required?.length ? { required: s.required } : {}) };
}

export type GeminiResult = { ok: true; text: string; model: string } | { ok: false; error: string };

export async function geminiChat(opts: { system: string; tools: GeminiTool[]; messages: { role: "user" | "assistant"; content: string }[]; audioWavB64?: string }): Promise<GeminiResult> {
  const key = (process.env.GEMINI_API_KEY ?? "").trim();
  if (!key) return { ok: false, error: "no GEMINI_API_KEY" };

  // history → contents; the newest user turn may carry the spoken audio inline
  const contents: Content[] = opts.messages.map((m, i) => {
    const last = i === opts.messages.length - 1;
    const parts: GeminiPart[] = [];
    if (m.role === "user" && last && opts.audioWavB64) parts.push({ inlineData: { mimeType: "audio/wav", data: opts.audioWavB64 } });
    parts.push({ text: m.content || (m.role === "user" && last && opts.audioWavB64 ? "(the user asked a spoken question — answer from the audio above)" : m.content) });
    return { role: m.role === "assistant" ? "model" : "user", parts };
  });

  for (const model of defaultModels()) {
    try {
      const text = await runToolLoop(model, key, opts.system, opts.tools, contents);
      if (text) return { ok: true, text, model };
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      // quota / overloaded → try the next model; anything else → give up to the fallback path
      if (!/429|quota|503|overloaded|RESOURCE_EXHAUSTED/i.test(msg)) return { ok: false, error: `${model}: ${msg}` };
    }
  }
  return { ok: false, error: "all Gemini models unavailable (quota/overloaded)" };
}

async function call(model: string, key: string, body: Record<string, unknown>): Promise<{ candidates?: { content?: { parts?: GeminiPart[] } }[] }> {
  const res = await fetch(`${BASE}/${model}:generateContent`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-goog-api-key": key },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(45_000),
  });
  if (!res.ok) {
    const t = await res.text();
    throw new Error(`HTTP ${res.status} ${t.slice(0, 300)}`);
  }
  return (await res.json()) as { candidates?: { content?: { parts?: GeminiPart[] } }[] };
}

async function runToolLoop(model: string, key: string, system: string, tools: GeminiTool[], contents: Content[]): Promise<string> {
  const decls = tools.map((t) => ({ name: t.name, description: t.description, parameters: toGeminiSchema(t.parameters) }));
  for (let turn = 0; turn < MAX_TURNS; turn++) {
    const last = turn === MAX_TURNS - 1; // final turn: no tools, force the answer
    const body: Record<string, unknown> = {
      system_instruction: { parts: [{ text: system }] },
      contents,
      generationConfig: { temperature: 0.3, maxOutputTokens: 1024 },
      ...(decls.length && !last ? { tools: [{ function_declarations: decls }] } : {}),
    };
    const out = await call(model, key, body);
    const parts = out.candidates?.[0]?.content?.parts ?? [];
    const calls = parts.flatMap((p) => (p.functionCall ? [p.functionCall] : []));
    const text = parts.map((p) => p.text ?? "").join("").trim();
    if (!calls.length) return text;
    contents.push({ role: "model", parts });
    const responses: GeminiPart[] = [];
    for (const c of calls) {
      const tool = tools.find((t) => t.name === c.name);
      const result = tool ? await tool.run(c.args ?? {}) : `unknown tool ${c.name}`;
      responses.push({ functionResponse: { name: c.name, response: { result } } });
    }
    contents.push({ role: "user", parts: responses });
  }
  return "";
}
