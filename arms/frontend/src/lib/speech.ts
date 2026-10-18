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

/** Read a reply out loud (cancels whatever is still being said). No-op where speechSynthesis is missing. */
export function speak(text: string) {
  if (!canSpeak() || !text.trim()) return;
  try {
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "en-US";
    u.rate = 1.02;
    window.speechSynthesis.speak(u);
  } catch {
    /* speaking is best effort */
  }
}

export function stopSpeaking() {
  if (canSpeak()) window.speechSynthesis.cancel();
}
