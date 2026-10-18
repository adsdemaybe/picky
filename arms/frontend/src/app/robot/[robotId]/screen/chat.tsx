"use client";
// "Ask me": talk to this robot's brain (POST /api/fleet/robots/{id}/messages -> fleet.BrainReply). Mic = browser
// SpeechRecognition where available; replies can be read out with speechSynthesis (toggle, remembered per screen).
import { Mic, MicOff, Send, Shuffle, Volume2, VolumeX } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { errorText, gw, type S } from "@/lib/gateway/client";
import type { RobotInfo } from "@/lib/gateway/events";
import { speak, stopSpeaking, useCanSpeak, useSpeechInput } from "@/lib/speech";
import { cn } from "@/lib/utils";
import { presetsFor } from "./presets";

// ------------------------------------------------------------------------------------------------ speak setting

const SPEAK_KEY = "picky.robotScreen.speak";
const speakListeners = new Set<() => void>();
function readSpeak() {
  try {
    return localStorage.getItem(SPEAK_KEY) !== "0";
  } catch {
    return true;
  }
}

export type SpeakSetting = { on: boolean; available: boolean; set: (v: boolean) => void; say: (text: string) => void };

/** Spoken replies on/off (default on), shared by the chat and "Where is". */
export function useSpeakSetting(): SpeakSetting {
  const available = useCanSpeak();
  const on = useSyncExternalStore(
    (cb) => (speakListeners.add(cb), () => speakListeners.delete(cb)),
    readSpeak,
    () => true,
  );
  const set = useCallback((v: boolean) => {
    try {
      localStorage.setItem(SPEAK_KEY, v ? "1" : "0");
    } catch {
      /* private mode: the toggle still works for this page view */
    }
    if (!v) stopSpeaking();
    speakListeners.forEach((l) => l());
  }, []);
  const say = useCallback((text: string) => {
    if (available && readSpeak()) speak(text);
  }, [available]);
  return { on: available && on, available, set, say };
}

// ------------------------------------------------------------------------------------------------ chat

type Msg = { id: string; role: "user" | "robot" | "error"; text: string; retry?: string };

const ALWAYS = ["What are you doing?", "What's in your basket?"];

let nextId = 0;
const mid = () => `m${++nextId}`;

export function ChatTab({
  robot,
  userId,
  speech,
  planogram,
}: {
  robot: RobotInfo;
  userId: string | null;
  speech: SpeakSetting;
  planogram: S["Planogram"] | null;
}) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [round, setRound] = useState(() => new Date().getDate());
  const presets = useMemo(() => presetsFor(planogram, round), [planogram, round]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const list = useRef<HTMLDivElement>(null);

  const send = useCallback(
    async (raw: string, channel: "web" | "voice" = "web") => {
      const t = raw.trim();
      if (!t || busy) return;
      setText("");
      setMsgs((m) => [...m.filter((x) => x.role !== "error"), { id: mid(), role: "user", text: t }]);
      setBusy(true);
      try {
        const { data } = await gw.robotMessage(robot.robot_id, { text: t, user_id: userId ?? undefined, channel });
        const reply = data.text?.trim() || "Okay.";
        setMsgs((m) => [...m, { id: mid(), role: "robot", text: reply }]);
        speech.say(reply);
      } catch (e) {
        setMsgs((m) => [...m, { id: mid(), role: "error", text: `I couldn't get that to my brain: ${errorText(e)}`, retry: t }]);
      } finally {
        setBusy(false);
      }
    },
    [busy, robot.robot_id, userId, speech],
  );

  const mic = useSpeechInput((heard) => void send(heard, "voice"));

  useEffect(() => {
    list.current?.scrollTo({ top: list.current.scrollHeight, behavior: "smooth" });
  }, [msgs, busy]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div ref={list} className="min-h-0 flex-1 overflow-y-auto px-4 py-3" aria-live="polite">
        {msgs.length === 0 ? (
          <div className="flex h-full flex-col justify-center gap-3 py-4">
            <p className="font-heading text-[26px] leading-tight font-bold">Ask me anything about the store, your order, or what I&apos;m up to.</p>
            <p className="text-[15px] text-muted-foreground">
              {mic.supported ? "Tap the mic and talk, or type below." : "Type your question below."}
            </p>
            <div className="flex flex-wrap gap-2">
              {[...presets.map((p) => p.text), ...ALWAYS].map((s) => (
                <Button key={s} variant="outline" size="lg" onClick={() => void send(s)} disabled={busy}>
                  {s}
                </Button>
              ))}
              {presets.length > 0 && (
                <Button variant="ghost" size="lg" onClick={() => setRound((r) => r + 1)} disabled={busy}>
                  <Shuffle /> More questions
                </Button>
              )}
            </div>
          </div>
        ) : (
          <ul className="flex flex-col gap-2.5">
            <AnimatePresence initial={false}>
              {msgs.map((m) => (
                <motion.li
                  key={m.id}
                  initial={{ opacity: 0, y: 10, scale: 0.98 }}
                  animate={{ opacity: 1, y: 0, scale: 1 }}
                  transition={{ type: "spring", stiffness: 420, damping: 32 }}
                  className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}
                >
                  <div
                    className={cn(
                      "max-w-[85%] rounded-3xl px-4 py-2.5 text-[16px] leading-snug",
                      m.role === "user" && "rounded-br-lg bg-foreground text-background",
                      m.role === "robot" && "rounded-bl-lg bg-leaf-wash text-foreground",
                      m.role === "error" && "rounded-bl-lg bg-tomato-wash text-tomato",
                    )}
                  >
                    {m.role === "robot" && <span className="mb-0.5 block text-[12px] font-semibold text-leaf-deep">{robot.name}</span>}
                    {m.text}
                    {m.retry && (
                      <button className="ml-2 cursor-pointer font-semibold underline underline-offset-2" onClick={() => void send(m.retry!)}>
                        Try again
                      </button>
                    )}
                  </div>
                </motion.li>
              ))}
              {busy && (
                <motion.li key="typing" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="flex">
                  <span className="flex items-center gap-1 rounded-3xl rounded-bl-lg bg-leaf-wash px-4 py-3.5" aria-label={`${robot.name} is thinking`}>
                    {[0, 1, 2].map((i) => (
                      <motion.span key={i} className="size-2 rounded-full bg-leaf" animate={{ opacity: [0.3, 1, 0.3] }} transition={{ duration: 1, repeat: Infinity, delay: i * 0.18 }} />
                    ))}
                  </span>
                </motion.li>
              )}
            </AnimatePresence>
          </ul>
        )}
      </div>

      {mic.error && (
        <p role="alert" className="mx-4 mb-1 rounded-xl bg-amber-wash px-3 py-1.5 text-[13.5px] text-amber-ink">
          {mic.error}
        </p>
      )}
      <form
        className="flex shrink-0 items-center gap-2 border-t border-border/70 p-3"
        onSubmit={(e) => {
          e.preventDefault();
          void send(text);
        }}
      >
        <MicButton mic={mic} disabled={busy} />
        <input
          value={mic.listening ? mic.interim : text}
          onChange={(e) => setText(e.target.value)}
          placeholder={mic.listening ? "Listening…" : `Ask ${robot.name}…`}
          aria-label={`Message ${robot.name}`}
          readOnly={mic.listening}
          className="h-12 min-w-0 flex-1 rounded-full bg-muted px-5 text-[16px] outline-none placeholder:text-muted-foreground focus:bg-card focus:ring-2 focus:ring-ring"
        />
        <Button type="submit" size="icon" className="size-12" disabled={busy || !text.trim() || mic.listening} aria-label="Send">
          <Send className="size-5" />
        </Button>
        <SpeakToggle speech={speech} />
      </form>
    </div>
  );
}

export function MicButton({ mic, disabled }: { mic: ReturnType<typeof useSpeechInput>; disabled?: boolean }) {
  if (!mic.supported)
    return (
      <Button type="button" variant="outline" size="icon" className="size-12" disabled aria-label="Voice input isn't available in this browser" title="Voice input isn't available in this browser">
        <MicOff className="size-5" />
      </Button>
    );
  return (
    <Button
      type="button"
      size="icon"
      variant={mic.listening ? "default" : "outline"}
      className={cn("relative size-12", mic.listening && "bg-tomato hover:bg-tomato")}
      onClick={() => (mic.listening ? mic.stop() : mic.start())}
      disabled={disabled}
      aria-pressed={mic.listening}
      aria-label={mic.listening ? "Stop listening" : "Talk"}
    >
      {mic.listening && <span className="absolute inset-0 animate-ping rounded-full bg-tomato/40" aria-hidden />}
      <Mic className="relative size-5" />
    </Button>
  );
}

function SpeakToggle({ speech }: { speech: SpeakSetting }) {
  if (!speech.available) return null;
  return (
    <label className="flex shrink-0 cursor-pointer items-center gap-1.5 pl-1 text-[13px] text-muted-foreground" title="Read replies out loud">
      {speech.on ? <Volume2 className="size-5 text-foreground" aria-hidden /> : <VolumeX className="size-5" aria-hidden />}
      <Switch checked={speech.on} onCheckedChange={speech.set} aria-label="Read replies out loud" />
    </label>
  );
}
