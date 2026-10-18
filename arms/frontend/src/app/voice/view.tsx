"use client";
import { ArrowRight, Bot, Check, LoaderCircle, Mic, Pencil, Save, Square, Trash2, Volume2 } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { TripGuide } from "@/components/TripGuide";
import { useTripGuide } from "@/lib/trip-guide";
import { Panel } from "@/components/picky";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { MEMORY_KINDS, MEMORY_LABELS, MEMORY_TEXT_LIMIT, type MemoryKind, type SavedMemory } from "@/lib/memory/model";
import { useMemory } from "@/lib/memory/store";
import { useVoiceInput } from "@/lib/useVoiceInput";
import { cn } from "@/lib/utils";

const EXAMPLES = [
  { kind: "preference" as const, text: "I like spicy food, and I prefer oat milk to regular milk." },
  { kind: "shopping" as const, text: "I need breakfast for the week, under $25." },
  { kind: "note" as const, text: "We usually cook dinner for two, but four friends are coming on Friday." },
];

export default function VoicePage() {
  const { entries, loaded, loading, saving, error, load, save, remove } = useMemory();
  const [text, setText] = useState("");
  const [inputMode, setInputMode] = useState<"trip" | "memory">("trip");
  const [readReplies, setReadReplies] = useState(true);
  const guide = useTripGuide();
  const activeGuide = ["review", "addons", "confirm"].includes(guide.phase);
  const [kind, setKind] = useState<MemoryKind>("preference");
  const [source, setSource] = useState<"voice" | "typed">("typed");
  const [editing, setEditing] = useState<string | undefined>();
  const [filter, setFilter] = useState<MemoryKind | "all">("all");
  const [message, setMessage] = useState("");
  function sendCommand(command: string) {
    const response = useTripGuide.getState().handle(command);
    setMessage(response ?? "Try ‘let’s do a weekly restock’, ‘plan breakfast’ or ‘start a pantry refill’. Nothing has started.");
    if (response) setText("");
  }
  const voice = useVoiceInput((transcript) => { setText(transcript.slice(0, MEMORY_TEXT_LIMIT)); setSource("voice"); setMessage(""); }, (transcript) => { if (inputMode === "trip") sendCommand(transcript); });
  const recording = voice.status !== "idle";
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (inputMode !== "trip" || !readReplies || recording || !("speechSynthesis" in window) || typeof SpeechSynthesisUtterance === "undefined") return;
    const reply = guide.error ?? guide.prompt;
    if (!reply) return;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(reply);
    utterance.lang = "en-US";
    try { window.speechSynthesis.speak(utterance); } catch { /* The visible confirmation remains usable if playback is blocked. */ }
    return () => window.speechSynthesis.cancel();
  }, [guide.prompt, guide.error, inputMode, readReplies, recording]);
  const clear = () => { setText(""); setEditing(undefined); setSource("typed"); setMessage(""); };
  const edit = (entry: SavedMemory) => { setInputMode("memory"); setText(entry.text); setKind(entry.kind); setSource(entry.source); setEditing(entry.id); setMessage(""); };
  const shown = entries.filter((entry) => filter === "all" || entry.kind === filter);

  return (
    <div className="flex flex-col gap-8">
      <section>
        <div className="mb-3 flex items-center gap-2 text-[14px] font-semibold text-leaf"><Mic className="size-4" aria-hidden />Voice & memory</div>
        <h1 className="text-[36px] leading-[1.05] font-bold md:text-[46px]">Tell Picky what you need.</h1>
        <p className="mt-3 max-w-[64ch] text-[16px] text-muted-foreground">Say “let’s do a weekly restock.” Picky checks your list, asks about add-ons, and waits for your confirmation before sending the robot. You can also save preferences for later.</p>
      </section>

      <div className="flex flex-wrap items-center gap-3">
        <div className="flex rounded-full bg-muted p-1" role="group" aria-label="Voice action">
          {([ ["trip", "Plan a trip"], ["memory", "Remember information"] ] as const).map(([mode, label]) => <button key={mode} type="button" aria-pressed={inputMode === mode} disabled={recording || saving} onClick={() => { setInputMode(mode); clear(); }} className={cn("min-h-11 cursor-pointer rounded-full px-4 text-[14px] font-semibold", inputMode === mode && "bg-card text-leaf shadow-sm")}>{label}</button>)}
        </div>
        {inputMode === "trip" && <label className="flex min-h-11 cursor-pointer items-center gap-2 text-[13px] text-muted-foreground"><Volume2 className="size-4" aria-hidden /><input type="checkbox" checked={readReplies} onChange={(event) => setReadReplies(event.target.checked)} className="accent-leaf" />Read replies aloud</label>}
      </div>

      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
        <Panel className="overflow-hidden">
          <div className={cn("flex flex-col items-center border-b border-border px-6 py-7 text-center", recording ? "bg-tomato-wash" : "bg-leaf-wash")}>
            <span className={cn("mb-4 grid size-16 place-items-center rounded-full text-white", recording ? "bg-tomato" : "bg-leaf")}>
              {recording ? <span className="flex h-7 items-center gap-1" aria-hidden>{[12, 24, 18, 28, 14].map((height, i) => <span key={i} className="live-dot w-1 rounded-full bg-white" style={{ height, animationDelay: `${i * 120}ms` }} />)}</span> : <Mic className="size-7" aria-hidden />}
            </span>
            <h2 className="text-[22px] font-semibold">{voice.status === "starting" ? "Waiting for your microphone…" : voice.status === "listening" ? "Listening to you…" : voice.status === "stopping" ? "Finishing your transcript…" : "Speak in your own words"}</h2>
            <p className="mt-1 text-[13.5px] text-muted-foreground">{recording ? inputMode === "trip" ? "Stop when you're done. The robot waits for confirmation." : "Stop when you're done. Nothing is saved until you choose Save." : "Tap to start. You can always type instead."}</p>
            <Button type="button" size="lg" variant={recording ? "destructive" : "default"} className="mt-4" disabled={saving || (!voice.supported && !recording) || voice.status === "stopping"} onClick={() => { if ("speechSynthesis" in window) window.speechSynthesis.cancel(); if (recording) voice.stop(); else voice.start(inputMode === "trip" ? "" : text); }}>
              {recording ? <><Square className="size-4" />Stop listening</> : <><Mic className="size-4" />Start speaking</>}
            </Button>
            {!voice.supported && <p className="mt-3 text-[13px] text-muted-foreground">Voice input isn&apos;t available in this browser. Use the text box below.</p>}
            {voice.error && <p role="alert" className="mt-3 max-w-[45ch] text-[13px] text-tomato">{voice.error}</p>}
          </div>
          <form className="p-5" onSubmit={async (event) => {
            event.preventDefault();
            if (recording || !text.trim()) return;
            if (inputMode === "trip") { sendCommand(text); return; }
            const updated = !!editing;
            if (await save({ text, kind, source }, editing)) { clear(); setMessage(updated ? "Updated. Picky will use the revised note." : "Saved. Ask Picky can recall this next time."); }
          }}>
            <label htmlFor="voice-transcript" className="text-[15px] font-semibold">{inputMode === "trip" ? activeGuide ? "Your reply" : "What should Picky do?" : editing ? "Edit saved information" : "Review your words"}</label>
            <textarea id="voice-transcript" value={text} maxLength={MEMORY_TEXT_LIMIT} disabled={recording || saving} onChange={(event) => { setText(event.target.value); setMessage(""); }} rows={5} placeholder={inputMode === "trip" ? activeGuide ? "Say yes, choose an add-on, or cancel…" : "Voice, let’s do a weekly restock" : "For example: I like spicy food and prefer oat milk."} className="mt-2 w-full resize-y rounded-xl border border-input bg-background p-3 text-[15px] outline-none focus:ring-2 focus:ring-ring disabled:opacity-70" aria-describedby="transcript-help" />
            <div id="transcript-help" className="mt-1 flex justify-between gap-3 text-[12px] text-muted-foreground"><span>{inputMode === "trip" ? "Spoken commands are sent when listening ends. You can type replies too." : "Check the transcript before saving."}</span><span>{text.length}/{MEMORY_TEXT_LIMIT}</span></div>
            {inputMode === "memory" && <fieldset className="mt-4" disabled={recording || saving}>
              <legend className="mb-2 text-[13.5px] font-semibold">Save this as</legend>
              <div className="flex flex-wrap gap-2">{MEMORY_KINDS.map((option) => <label key={option} className={cn("flex min-h-10 cursor-pointer items-center gap-2 rounded-full px-3 text-[13px] font-medium ring-1", kind === option ? "bg-leaf-wash text-leaf ring-leaf" : "ring-border")}><input type="radio" name="memory-kind" value={option} checked={kind === option} onChange={() => setKind(option)} className="accent-leaf" />{MEMORY_LABELS[option]}</label>)}</div>
            </fieldset>}
            <div className="mt-5 flex flex-wrap items-center gap-2">
              <Button type="submit" disabled={recording || saving || (inputMode === "memory" && (loading || !loaded)) || !text.trim()}>{inputMode === "trip" ? <Bot className="size-4" /> : <Save className="size-4" />}{inputMode === "trip" ? activeGuide ? "Send reply" : "Plan my trip" : saving ? "Saving…" : editing ? "Save changes" : "Save information"}</Button>
              {(text || editing) && <Button type="button" variant="ghost" disabled={recording || saving} onClick={clear}>{editing ? "Cancel edit" : "Clear"}</Button>}
            </div>
            {message && <p role="status" className="mt-3 flex items-center gap-2 text-[13.5px] text-leaf"><Check className="size-4 shrink-0" />{message}</p>}
            <p className="mt-4 text-[12px] leading-relaxed text-muted-foreground">Speech may be processed by your browser&apos;s speech provider. Picky never stores raw audio. Memory notes are saved only when you choose Save; trip commands prepare a proposal for you to confirm. <a href="https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition" target="_blank" rel="noreferrer" className="underline">How browser speech works</a></p>
          </form>
          <div className="px-4">{inputMode === "trip" && <TripGuide />}</div>
        </Panel>

        <div className="flex flex-col gap-5">
          <Panel className="p-5">
            <div className="flex items-center justify-between gap-3"><h2 className="text-[20px] font-semibold">Saved information</h2><Badge variant="leaf">{entries.length} saved</Badge></div>
            <p className="mt-2 text-[13px] text-muted-foreground">Saved to Maya&apos;s shared demo profile on this server. Ask Picky reads these notes; they don&apos;t change your spending rules.</p>
            <div className="mt-4 flex flex-wrap gap-1.5" role="group" aria-label="Filter saved information">{(["all", ...MEMORY_KINDS] as const).map((value) => <button key={value} type="button" aria-pressed={filter === value} onClick={() => setFilter(value)} className={cn("min-h-10 cursor-pointer rounded-full px-3 text-[12.5px] font-medium", filter === value ? "bg-foreground text-background" : "bg-muted text-muted-foreground")}>{value === "all" ? "All" : MEMORY_LABELS[value]}</button>)}</div>
            {error && <div role="alert" className="mt-3 rounded-xl bg-tomato-wash p-3 text-[13px] text-tomato">{error}<button type="button" onClick={() => void load()} className="ml-2 cursor-pointer underline">Retry loading</button></div>}
            {loading && !loaded ? <p role="status" className="mt-6 flex items-center gap-2 text-[14px] text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />Loading saved notes…</p> : shown.length === 0 ? <p className="mt-6 rounded-xl bg-muted p-4 text-[14px] text-muted-foreground">{entries.length ? "No notes in this category yet." : "Nothing saved yet. Tell Picky something to get started."}</p> : <ul className="mt-4 space-y-3">{shown.map((entry) => <li key={entry.id} className={cn("rounded-xl border p-4", editing === entry.id ? "border-leaf bg-leaf-wash" : "border-border")}>
              <div className="flex items-center justify-between gap-2"><Badge variant={entry.kind === "shopping" ? "sky" : "muted"}>{MEMORY_LABELS[entry.kind]}</Badge><span className="text-[11.5px] text-muted-foreground">{entry.source === "voice" ? "Spoken" : "Typed"}</span></div>
              <p className="mt-2 whitespace-pre-wrap break-words text-[14.5px]">{entry.text}</p>
              <div className="mt-3 flex flex-wrap items-center gap-1">
                {entry.kind === "shopping" && <Button variant="ghost" size="sm" disabled={recording} onClick={() => { setInputMode("trip"); sendCommand(entry.text); }}>Plan this trip<ArrowRight className="size-3.5" /></Button>}
                <Button variant="ghost" size="sm" disabled={recording || saving || loading} onClick={() => edit(entry)} aria-label={`Edit note: ${entry.text}`}><Pencil className="size-3.5" />Edit</Button>
                <Button variant="ghost" size="sm" className="text-tomato" disabled={recording || saving || loading} onClick={async () => { if (await remove(entry.id)) { if (editing === entry.id) clear(); setMessage("Deleted from saved information."); } }} aria-label={`Delete note: ${entry.text}`}><Trash2 className="size-3.5" />Delete</Button>
              </div>
            </li>)}</ul>}
          </Panel>
          <Panel className="p-5"><h2 className="text-[16px] font-semibold">Try a natural-language prompt</h2><button type="button" disabled={recording} onClick={() => { setInputMode("trip"); sendCommand("Voice, let’s do a weekly restock"); }} className="mt-3 min-h-11 w-full cursor-pointer rounded-xl bg-leaf-wash p-3 text-left text-[14px] font-semibold text-leaf">Let’s do a weekly restock</button><div className="mt-3 flex flex-col gap-2">{EXAMPLES.map((example) => <button key={example.kind} type="button" disabled={recording || saving} onClick={() => { setInputMode(example.kind === "shopping" ? "trip" : "memory"); setText(example.text); setKind(example.kind); setEditing(undefined); setSource("typed"); setMessage(""); }} className="cursor-pointer rounded-xl bg-muted p-3 text-left text-[13.5px] hover:bg-leaf-wash disabled:opacity-50">&ldquo;{example.text}&rdquo;</button>)}</div><Link href="/ask" className="mt-4 inline-flex min-h-10 items-center gap-2 text-[14px] font-semibold text-sky">Ask what Picky remembers<ArrowRight className="size-4" /></Link></Panel>
        </div>
      </div>
    </div>
  );
}
