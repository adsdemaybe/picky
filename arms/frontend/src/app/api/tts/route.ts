// Natural-sounding speech for the robot screen: a neural TTS provider when a key is configured in .env.local,
// otherwise 204 so the browser falls back to its best built-in voice (lib/speech.ts).
//   OPENAI_API_KEY      -> OpenAI gpt-4o-mini-tts (voice PICKY_TTS_VOICE, default "coral")
//   ELEVENLABS_API_KEY  -> ElevenLabs eleven_turbo_v2_5 (voice id ELEVENLABS_VOICE_ID, default "Rachel")
//   DEEPGRAM_API_KEY    -> Deepgram Aura (model PICKY_TTS_VOICE, default "aura-2-thalia-en")
//   GEMINI_API_KEY      -> Gemini TTS (gemini-3.8-flash-tts, voice PICKY_GEMINI_VOICE, default "Aoede"), WAV
// GET  /api/tts           -> { provider: "openai" | "elevenlabs" | "deepgram" | "gemini" | null }
// POST /api/tts {text}    -> audio (mp3, or wav for Gemini)
const MAX_CHARS = 1200;

function provider(): "openai" | "elevenlabs" | "deepgram" | "gemini" | null {
  if (process.env.OPENAI_API_KEY) return "openai";
  if (process.env.ELEVENLABS_API_KEY) return "elevenlabs";
  if (process.env.DEEPGRAM_API_KEY) return "deepgram";
  if (process.env.GEMINI_API_KEY) return "gemini";
  return null;
}

/** 16-bit mono PCM -> WAV (Gemini may return bare samples). */
function wav(pcm: Uint8Array, rate = 24000): Uint8Array {
  const out = new Uint8Array(44 + pcm.length);
  const v = new DataView(out.buffer);
  const str = (o: number, s: string) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  str(0, "RIFF");
  v.setUint32(4, 36 + pcm.length, true);
  str(8, "WAVE");
  str(12, "fmt ");
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true);
  v.setUint16(22, 1, true);
  v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true);
  v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  str(36, "data");
  v.setUint32(40, pcm.length, true);
  out.set(pcm, 44);
  return out;
}

async function geminiSpeech(text: string): Promise<Response> {
  const model = process.env.PICKY_GEMINI_TTS_MODEL || "gemini-3.8-flash-tts";
  const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-goog-api-key": process.env.GEMINI_API_KEY! },
    body: JSON.stringify({
      contents: [{ parts: [{ text: `Say this warmly and naturally, like a friendly grocery store helper talking to a shopper: ${text}` }] }],
      generationConfig: {
        responseModalities: ["AUDIO"],
        speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: process.env.PICKY_GEMINI_VOICE || "Aoede" } } },
      },
    }),
  });
  if (!r.ok) return Response.json({ error: `tts gemini ${r.status}` }, { status: 502 });
  const j = (await r.json()) as { candidates?: { content?: { parts?: { inlineData?: { data: string; mimeType: string } }[] } }[] };
  const inline = j.candidates?.[0]?.content?.parts?.find((p) => p.inlineData)?.inlineData;
  if (!inline) return Response.json({ error: "tts gemini: no audio" }, { status: 502 });
  const bytes = Uint8Array.from(Buffer.from(inline.data, "base64"));
  const isWav = bytes[0] === 0x52 && bytes[1] === 0x49 && bytes[2] === 0x46 && bytes[3] === 0x46;
  const rate = Number(/rate=(\d+)/.exec(inline.mimeType)?.[1] ?? 24000);
  const body = isWav ? bytes : wav(bytes, rate);
  return new Response(body as unknown as BodyInit, { headers: { "Content-Type": "audio/wav", "Cache-Control": "no-store" } });
}

export async function GET() {
  return Response.json({ provider: provider() });
}

export async function POST(req: Request) {
  const which = provider();
  if (!which) return new Response(null, { status: 204 });
  let text = "";
  try {
    text = String(((await req.json()) as { text?: string }).text ?? "").slice(0, MAX_CHARS);
  } catch {
    return Response.json({ error: "bad json" }, { status: 400 });
  }
  if (!text.trim()) return Response.json({ error: "empty text" }, { status: 400 });
  if (which === "gemini") {
    try {
      return await geminiSpeech(text);
    } catch (e) {
      return Response.json({ error: `tts provider unreachable: ${String(e)}` }, { status: 502 });
    }
  }
  let upstream: Response;
  try {
    if (which === "openai") {
      upstream = await fetch("https://api.openai.com/v1/audio/speech", {
        method: "POST",
        headers: { Authorization: `Bearer ${process.env.OPENAI_API_KEY}`, "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "gpt-4o-mini-tts",
          voice: process.env.PICKY_TTS_VOICE || "coral",
          input: text,
          instructions: "Warm, friendly store helper. Natural, upbeat, conversational; not robotic.",
          response_format: "mp3",
        }),
      });
    } else if (which === "elevenlabs") {
      const voice = process.env.ELEVENLABS_VOICE_ID || "21m00Tcm4TlvDq8ikWAM";
      upstream = await fetch(`https://api.elevenlabs.io/v1/text-to-speech/${voice}`, {
        method: "POST",
        headers: { "xi-api-key": process.env.ELEVENLABS_API_KEY!, "Content-Type": "application/json", Accept: "audio/mpeg" },
        body: JSON.stringify({ text, model_id: "eleven_turbo_v2_5", voice_settings: { stability: 0.45, similarity_boost: 0.8 } }),
      });
    } else {
      const model = process.env.PICKY_TTS_VOICE || "aura-2-thalia-en";
      upstream = await fetch(`https://api.deepgram.com/v1/speak?model=${encodeURIComponent(model)}&encoding=mp3`, {
        method: "POST",
        headers: { Authorization: `Token ${process.env.DEEPGRAM_API_KEY}`, "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
    }
  } catch (e) {
    return Response.json({ error: `tts provider unreachable: ${String(e)}` }, { status: 502 });
  }
  if (!upstream.ok || !upstream.body) {
    return Response.json({ error: `tts ${which} ${upstream.status}` }, { status: 502 });
  }
  return new Response(upstream.body, { headers: { "Content-Type": "audio/mpeg", "Cache-Control": "no-store" } });
}
