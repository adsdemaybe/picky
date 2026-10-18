"use client";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";

type SpeechResult = { isFinal: boolean; 0: { transcript: string } };
type Recognizer = {
  lang: string; continuous: boolean; interimResults: boolean;
  onstart: (() => void) | null;
  onend: (() => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onresult: ((event: { results: ArrayLike<SpeechResult> }) => void) | null;
  start: () => void; stop: () => void; abort: () => void;
};
type SpeechWindow = Window & { SpeechRecognition?: new () => Recognizer; webkitSpeechRecognition?: new () => Recognizer };
function constructor() {
  if (typeof window === "undefined") return undefined;
  const browser = window as SpeechWindow;
  return browser.SpeechRecognition ?? browser.webkitSpeechRecognition;
}
const subscribe = () => () => {};
const isSupported = () => !!constructor();
const serverSupported = () => false;

export function speechErrorMessage(code: string) {
  const messages: Record<string, string> = {
    "not-allowed": "Microphone access was denied. Allow it in your browser settings, or type your note below.",
    "service-not-allowed": "Your browser blocked speech recognition. You can still type your note.",
    "audio-capture": "No microphone was found. Connect one or type your note below.",
    "no-speech": "No speech was detected. Try again, or type your note.",
    network: "Speech recognition lost its connection. Your transcript is still here; retry or keep typing.",
    "language-not-supported": "English speech recognition isn't available in this browser. You can type instead.",
  };
  return messages[code] ?? "Voice input stopped. Your transcript is still here; try again or type your note.";
}

export function mergeTranscript(prefix: string, results: ArrayLike<SpeechResult>) {
  const spoken = Array.from(results).map((result) => result[0].transcript).join(" ").trim();
  return [prefix.trim(), spoken].filter(Boolean).join(" ");
}

export function useVoiceInput(onTranscript: (text: string) => void, onComplete?: (text: string) => void) {
  const supported = useSyncExternalStore(subscribe, isSupported, serverSupported);
  const [status, setStatus] = useState<"idle" | "starting" | "listening" | "stopping">("idle");
  const [error, setError] = useState<string | null>(null);
  const recognition = useRef<Recognizer | null>(null);
  const timeout = useRef<ReturnType<typeof setTimeout> | null>(null);
  const callback = useRef(onTranscript);
  const complete = useRef(onComplete);
  useEffect(() => { callback.current = onTranscript; complete.current = onComplete; }, [onTranscript, onComplete]);
  const clearTimer = () => { if (timeout.current) clearTimeout(timeout.current); timeout.current = null; };

  useEffect(() => () => {
    clearTimer();
    const active = recognition.current;
    if (active) {
      active.onstart = active.onend = active.onerror = active.onresult = null;
      active.abort();
    }
  }, []);

  const stop = () => {
    clearTimer();
    if (recognition.current) {
      try { setStatus("stopping"); recognition.current.stop(); }
      catch { recognition.current.abort(); recognition.current = null; setStatus("idle"); }
    }
  };
  const start = (existing: string) => {
    if (recognition.current) return;
    setError(null);
    const Speech = constructor();
    if (!Speech) { setError("This browser doesn't support voice input. Type your note below."); return; }
    if (!window.isSecureContext) { setError("Voice input needs HTTPS or localhost. You can still type your note."); return; }
    const active = new Speech();
    const prefix = existing.trim();
    let latest = prefix;
    let receivedSpeech = false;
    recognition.current = active;
    active.lang = "en-US";
    active.continuous = true;
    active.interimResults = true;
    active.onstart = () => {
      if (recognition.current !== active) return;
      setStatus("listening");
      timeout.current = setTimeout(stop, 60000);
    };
    active.onresult = (event) => {
      // Rebuild from the whole result list: interim words can change, so appending would duplicate them.
      if (recognition.current === active) {
        latest = mergeTranscript(prefix, event.results);
        receivedSpeech = true;
        callback.current(latest);
      }
    };
    active.onerror = (event) => {
      if (recognition.current !== active) return;
      clearTimer();
      if (event.error !== "aborted") setError(speechErrorMessage(event.error));
      setStatus("idle");
      recognition.current = null;
    };
    active.onend = () => {
      if (recognition.current !== active) return;
      clearTimer(); setStatus("idle"); recognition.current = null;
      if (receivedSpeech && latest.trim()) complete.current?.(latest);
    };
    try { setStatus("starting"); active.start(); }
    catch { recognition.current = null; setStatus("idle"); setError("Couldn't start the microphone. Try again or type your note."); }
  };
  return { supported, status, error, start, stop };
}
