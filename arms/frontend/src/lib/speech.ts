"use client";
// Browser Web Speech: SpeechRecognition (Chrome / Edge / Safari, often prefixed) for the mic button and
// speechSynthesis for spoken replies. Both are optional: callers check `supported` and fall back to typing / reading.
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

// lib.dom has no SpeechRecognition types yet: the small surface we use.
type RecognitionResult = { isFinal: boolean; 0: { transcript: string } };
type RecognitionEvent = { resultIndex: number; results: ArrayLike<RecognitionResult> };
type Recognition = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: ((e: RecognitionEvent) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
};
type RecognitionCtor = new () => Recognition;

function recognitionCtor(): RecognitionCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

const ERRORS: Record<string, string> = {
  "not-allowed": "Microphone access is blocked. Allow it in the browser, or type instead.",
  "service-not-allowed": "Speech recognition isn't allowed here. Type instead.",
  "no-speech": "I didn't hear anything. Tap the mic and try again.",
  "audio-capture": "No microphone found. Type instead.",
  network: "Speech recognition needs a network connection. Type instead.",
};

const noopSubscribe = () => () => {};

/** Push-to-talk: `start()` listens once; `interim` updates live, `onFinal(text)` fires with the final transcript. */
export function useSpeechInput(onFinal: (text: string) => void) {
  // server render + first client frame say "unsupported", then the real answer (no hydration mismatch)
  const supported = useSyncExternalStore(noopSubscribe, () => recognitionCtor() !== null, () => false);
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const [error, setError] = useState<string | null>(null);
  const rec = useRef<Recognition | null>(null);
  const finalCb = useRef(onFinal);
  useEffect(() => {
    finalCb.current = onFinal;
  }, [onFinal]);

  useEffect(() => () => rec.current?.abort(), []);

  const stop = useCallback(() => rec.current?.stop(), []);

  const start = useCallback(() => {
    const Ctor = recognitionCtor();
    if (!Ctor) {
      setError("Voice input isn't available in this browser. Type instead.");
      return;
    }
    rec.current?.abort();
    const r = new Ctor();
    r.lang = "en-US";
    r.continuous = false;
    r.interimResults = true;
    r.maxAlternatives = 1;
    let final = "";
    r.onresult = (e) => {
      let live = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const res = e.results[i];
        if (res.isFinal) final += res[0].transcript;
        else live += res[0].transcript;
      }
      setInterim((final + live).trim());
    };
    r.onerror = (e) => {
      if (e.error !== "aborted") setError(ERRORS[e.error] ?? `Voice input stopped (${e.error}). Type instead.`);
    };
    r.onend = () => {
      setListening(false);
      setInterim("");
      if (rec.current === r) rec.current = null;
      const text = final.trim();
      if (text) finalCb.current(text);
    };
    rec.current = r;
    setError(null);
    setInterim("");
    try {
      r.start();
      setListening(true);
    } catch {
      setError("Couldn't start the microphone. Type instead.");
    }
  }, []);

  return { supported, listening, interim, error, start, stop, clearError: () => setError(null) };
}

/** Same hydration-safe check for speechSynthesis. */
export function useCanSpeak() {
  return useSyncExternalStore(noopSubscribe, canSpeak, () => false);
}

export function canSpeak() {
  return typeof window !== "undefined" && "speechSynthesis" in window && typeof SpeechSynthesisUtterance !== "undefined";
}

// ---- a more human voice ------------------------------------------------------------------------------------------
// 1) a neural voice from /api/tts when the server has a TTS key (OpenAI / ElevenLabs / Deepgram), else
// 2) the most natural built-in voice this device has (Apple Premium / Enhanced, Microsoft "Natural", Google), spoken
//    sentence by sentence at a relaxed pace. Novelty / compact voices are never picked.
const NOVELTY = /albert|bad news|bahh|bells|boing|bubbles|cellos|deranged|fred|good news|hysterical|jester|junior|organ|pipe|ralph|superstar|trinoids|whisper|wobble|zarvox|grandma|grandpa|eddy|flo|reed|rocko|sandy|shelley/i;
const PREFERRED = [/ava/i, /zoe/i, /samantha/i, /allison/i, /evan/i, /nathan/i, /aria/i, /jenny/i, /google us english/i, /serena/i, /karen/i, /moira/i];

let ttsProvider: Promise<string | null> | null = null;
let audio: HTMLAudioElement | null = null;

function bestVoice(): SpeechSynthesisVoice | null {
  const voices = window.speechSynthesis.getVoices().filter((v) => /^en(-|_|$)/i.test(v.lang) && !NOVELTY.test(v.name));
  if (!voices.length) return null;
  const score = (v: SpeechSynthesisVoice) => {
    let s = 0;
    if (/premium/i.test(v.name)) s += 50;
    if (/enhanced|natural|neural/i.test(v.name)) s += 40;
    if (/google/i.test(v.name)) s += 20;
    const pref = PREFERRED.findIndex((re) => re.test(v.name));
    if (pref >= 0) s += 30 - pref;
    if (/en-us/i.test(v.lang)) s += 5;
    if (v.localService) s += 1;
    return s;
  };
  return voices.sort((a, b) => score(b) - score(a))[0];
}

function speakBrowser(text: string, done: () => void) {
  const synth = window.speechSynthesis;
  const voice = bestVoice();
  // short sentences sound more natural than one long utterance (and recover if one is cut off)
  const parts = text.match(/[^.!?]+[.!?]*/g)?.map((s) => s.trim()).filter(Boolean) ?? [text];
  parts.forEach((part, i) => {
    const u = new SpeechSynthesisUtterance(part);
    if (voice) u.voice = voice;
    u.lang = voice?.lang ?? "en-US";
    u.rate = 0.98;
    u.pitch = 1.05;
    if (i === parts.length - 1) {
      u.onend = done;
      u.onerror = done;
    }
    synth.speak(u);
  });
}

// "is the robot talking right now", for faces and hands-free listening (useSpeaking)
let talking = false;
let talkSeq = 0;
const talkListeners = new Set<() => void>();
function setTalking(v: boolean) {
  talking = v;
  talkListeners.forEach((l) => l());
}
export function useSpeaking() {
  return useSyncExternalStore(
    (cb) => (talkListeners.add(cb), () => talkListeners.delete(cb)),
    () => talking,
    () => false,
  );
}

/** Read a reply out loud (cancels whatever is still being said). Neural voice when available, else built-in.
 * Resolves when it finishes, is cut off, or can't be spoken. */
export function speak(text: string): Promise<void> {
  if (typeof window === "undefined" || !text.trim()) return Promise.resolve();
  stopSpeaking();
  const seq = ++talkSeq;
  ttsProvider ??= fetch("/api/tts")
    .then((r) => (r.ok ? r.json() : { provider: null }))
    .then((j: { provider: string | null }) => j.provider)
    .catch(() => null);
  return new Promise<void>((resolve) => {
    let settled = false;
    const done = () => {
      if (settled) return;
      settled = true;
      if (seq === talkSeq) setTalking(false);
      resolve();
    };
    setTalking(true);
    void ttsProvider!.then(async (prov) => {
      if (seq !== talkSeq) return done();
      if (prov) {
        try {
          const r = await fetch("/api/tts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
          if (r.status === 200 && seq === talkSeq) {
            const url = URL.createObjectURL(await r.blob());
            audio = new Audio(url);
            audio.onended = () => (URL.revokeObjectURL(url), done());
            audio.onpause = done;
            await audio.play();
            return;
          }
        } catch {
          /* fall back to the built-in voice */
        }
      }
      if (seq === talkSeq && canSpeak()) {
        try {
          speakBrowser(text, done);
          // some browsers never fire onend; don't leave the face talking forever
          setTimeout(done, 4000 + text.length * 110);
          return;
        } catch {
          /* speaking is best effort */
        }
      }
      done();
    });
  });
}

export function stopSpeaking() {
  talkSeq++;
  if (audio) {
    audio.pause();
    audio = null;
  }
  if (canSpeak()) window.speechSynthesis.cancel();
  if (talking) setTalking(false);
}
