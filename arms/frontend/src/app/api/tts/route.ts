// Natural-sounding speech for the robot screen: a neural TTS provider when a key is configured in .env.local,
// otherwise 204 so the browser falls back to its best built-in voice (lib/speech.ts).
//   OPENAI_API_KEY      -> OpenAI gpt-4o-mini-tts (voice PICKY_TTS_VOICE, default "coral")
//   ELEVENLABS_API_KEY  -> ElevenLabs eleven_turbo_v2_5 (voice id ELEVENLABS_VOICE_ID, default "Rachel")
//   DEEPGRAM_API_KEY    -> Deepgram Aura (model PICKY_TTS_VOICE, default "aura-2-thalia-en")
// GET  /api/tts           -> { provider: "openai" | "elevenlabs" | "deepgram" | null }
// POST /api/tts {text}    -> audio/mpeg
const MAX_CHARS = 1200;

function provider(): "openai" | "elevenlabs" | "deepgram" | null {
  if (process.env.OPENAI_API_KEY) return "openai";
  if (process.env.ELEVENLABS_API_KEY) return "elevenlabs";
  if (process.env.DEEPGRAM_API_KEY) return "deepgram";
  return null;
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
