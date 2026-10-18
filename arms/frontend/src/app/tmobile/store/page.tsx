"use client";
// In store: the robot as the store's sales associate. The shopper walks up and talks to Magenta (mic or typing);
// Magenta answers out loud, walks them to the right fixture, fetches boxed phones from the back room, checks fit,
// says "skip it" / "wait" when that's the honest answer, pages a human for account work, and rings them up at the
// counter. Brain: /api/associate (Grok + deterministic tools when XAI_API_KEY is set, else the offline brain); if
// that route is unreachable the same offline brain runs here in the browser. The basket is shared with the Shop page.
import { Bell, BellOff, Check, Footprints, Keyboard, Mic, MicOff, PackageOpen, Send, ShoppingBag, UserRound, UserRoundCheck, Volume2, VolumeX, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { Money, Panel, ProductTile } from "@/components/picky";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import { tmobileStore } from "@/data/stores/tmobile";
import { approachOf, applyActions, backStock, chipsFor, dealOf, deviceOf, displaySlot, fixtureName, productOf, respond, stockOf, type AssocAction, type AssocReply } from "@/lib/associate";
import type { Pose } from "@/lib/data";
import { fmt } from "@/lib/money";
import { astar } from "@/lib/routing";
import { speak, stopSpeaking, useCanSpeak, useSpeaking, useSpeechInput } from "@/lib/speech";
import { useTmoCart } from "@/lib/tmoCart";
import { cn } from "@/lib/utils";
import { applySuggestion, cartTotal, shortName, suggest, unitPrice, type Suggestion } from "@/lib/whisperer";
import { useWhisper } from "@/lib/whisperStore";
import { WhisperList } from "../bits";
import { usePhrasing } from "../phrasing";
import { Face, type FaceMode } from "./face";
import { StoreFloor } from "./map";

type Msg = { id: string; role: "you" | "magenta" | "system"; text: string; skus?: string[] };
type Task = { kind: "guide" | "fetch" | "counter"; label: string; fixture: string; sku?: string } | null;

const SPEED = 1.5; // m/s on screen (a little brisker than the real rover)
const HOME: Pose = { ...tmobileStore.robot_home, theta: -Math.PI / 2 };
const SHOPPER_START: Pose = { x: tmobileStore.robot_home.x, y: tmobileStore.robot_home.y + 0.7 };
let seq = 0;
const mid = () => `m${++seq}`;
const nice = (n: string) => n.replace(/\s+\d+\s*GB$/i, "");
const ACCOUNT_CODES = new Set(["ALREADY_OWNED", "INSTALLMENTS_LEFT"]);

// ------------------------------------------------------------------------------------------------ walking

function pointAt(path: Pose[], cum: number[], s: number): Pose {
  if (path.length < 2) return path[0] ?? HOME;
  let i = cum.findIndex((c) => c >= s);
  if (i <= 0) i = 1;
  const a = path[i - 1], b = path[i];
  const seg = cum[i] - cum[i - 1] || 1;
  const k = Math.max(0, Math.min(1, (s - cum[i - 1]) / seg));
  return { x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k, theta: Math.atan2(b.y - a.y, b.x - a.x) };
}

/** Animates the robot (and optionally the shopper, a step behind) along A* paths through the store. */
function useWalker() {
  const [robot, setRobot] = useState<Pose>(HOME);
  const [you, setYou] = useState<Pose | null>(SHOPPER_START);
  const [path, setPath] = useState<Pose[]>([]);
  const pose = useRef<{ robot: Pose; you: Pose | null }>({ robot: HOME, you: SHOPPER_START });
  const token = useRef(0);

  const walk = useCallback((to: Pose, follow: boolean) => {
    const my = ++token.current;
    const p = astar(tmobileStore, pose.current.robot, to).path;
    const pts = p.length >= 2 ? [...p.slice(0, -1), { x: to.x, y: to.y }] : [pose.current.robot, to];
    const cum = [0];
    for (let i = 1; i < pts.length; i++) cum.push(cum[i - 1] + Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y));
    const L = cum.at(-1)!;
    setPath(pts);
    const t0 = performance.now();
    return new Promise<void>((resolve) => {
      const frame = (now: number) => {
        if (my !== token.current) return resolve();
        const s = Math.min(L, ((now - t0) / 1000) * SPEED);
        const r = pointAt(pts, cum, s);
        pose.current.robot = r;
        setRobot(r);
        if (follow) {
          const y = pointAt(pts, cum, Math.max(0, s - 0.55));
          pose.current.you = y;
          setYou(y);
        }
        if (s < L) requestAnimationFrame(frame);
        else {
          setPath([]);
          resolve();
        }
      };
      requestAnimationFrame(frame);
    });
  }, []);

  const reset = useCallback(() => {
    token.current++;
    pose.current = { robot: HOME, you: SHOPPER_START };
    setRobot(HOME);
    setYou(SHOPPER_START);
    setPath([]);
  }, []);

  return { robot, you, path, walk, reset, pose };
}

// ------------------------------------------------------------------------------------------------ page

export default function InStore() {
  const [started, setStarted] = useState(false);
  const [known, setKnown] = useState(false);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [chips, setChips] = useState<string[]>(chipsFor({ cart: [], budget_cents: 10000, known: false }));
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState("");
  const [typing, setTyping] = useState(false);
  const [voiceOn, setVoiceOn] = useState(true);
  const [handsFree, setHandsFree] = useState(true);
  const [brain, setBrain] = useState<"grok" | "offline">("offline");
  const [task, setTask] = useState<Task>(null);
  const [expert, setExpert] = useState<{ reason: string; at: number } | null>(null);
  const [checkout, setCheckout] = useState(false);
  const [receipt, setReceipt] = useState<{ total: number; items: number } | null>(null);
  const focus = useRef<string | null>(null);
  const [device, setDevice] = useState<string | null>(null);
  const live = useRef({ handsFree, voiceOn, started, checkout });
  useEffect(() => {
    live.current = { handsFree, voiceOn, started, checkout };
  }, [handsFree, voiceOn, started, checkout]);

  const cart = useTmoCart((s) => s.cart);
  const budget = useTmoCart((s) => s.budget_cents);
  const touched = useTmoCart((s) => s.touched);
  const [fromWeb] = useState(() => (touched === "web" && cart.length ? cart.reduce((n, l) => n + l.qty, 0) : 0));
  const w = useWhisper();
  const walker = useWalker();
  const talking = useSpeaking();
  const canSpeak = useCanSpeak();
  const list = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetch("/api/associate")
      .then((r) => r.json())
      .then((j: { brain: "grok" | "offline" }) => setBrain(j.brain))
      .catch(() => {});
  }, []);
  useEffect(() => {
    list.current?.scrollTo({ top: list.current.scrollHeight, behavior: "smooth" });
  }, [msgs, busy]);
  useEffect(() => () => stopSpeaking(), []);

  // ---- speech in / out
  const askRef = useRef<(t: string) => void>(() => {});
  const mic = useSpeechInput((heard) => askRef.current(heard));
  const say = useCallback(
    async (t: string) => {
      if (!live.current.voiceOn) return;
      await speak(t);
      // hands-free: listen again once Magenta is done talking
      const l = live.current;
      if (l.handsFree && l.started && !l.checkout && mic.supported && document.visibilityState === "visible") mic.start();
    },
    [mic],
  );
  const post = useCallback((m: Omit<Msg, "id">) => setMsgs((x) => [...x, { ...m, id: mid() }]), []);

  // ---- robot actions
  const fetchFromBack = useCallback(
    async (sku: string) => {
      const name = nice(productOf(sku)?.name ?? sku);
      setTask({ kind: "fetch", label: `Getting a sealed ${name} from the back room`, fixture: "BACK-STOCK", sku });
      const meet = { ...walker.pose.current.robot };
      await walker.walk(approachOf("BACK-STOCK"), false);
      await new Promise((r) => setTimeout(r, 1300));
      const y = walker.pose.current.you ?? meet;
      await walker.walk({ x: y.x + 0.5, y: y.y }, false);
      setTask(null);
      const line = `Here's your sealed ${name}. It's in your basket.`;
      post({ role: "magenta", text: line });
      toast(`Magenta brought a sealed ${name}`, { icon: <PackageOpen className="size-4" /> });
      if (!mic.listening) void say(line);
    },
    [walker, post, say, mic.listening],
  );

  const run = useCallback(
    async (actions: AssocAction[]) => {
      const st = useTmoCart.getState();
      for (const a of actions) {
        if (a.type === "add") st.add(a.sku, a.qty, "store");
        if (a.type === "remove") st.setQty(a.sku, 0, "store");
        if (a.type === "expert") setExpert({ reason: a.reason, at: Date.now() });
        if (a.type === "checkout") setTimeout(() => setCheckout(true), 600);
      }
      const guide = actions.find((a) => a.type === "guide") as Extract<AssocAction, { type: "guide" }> | undefined;
      const fetch = actions.find((a) => a.type === "fetch") as Extract<AssocAction, { type: "fetch" }> | undefined;
      if (guide) {
        setTask({ kind: "guide", label: `Walking you to ${fixtureName(guide.fixture_id)}`, fixture: guide.fixture_id, sku: guide.sku });
        await walker.walk(approachOf(guide.fixture_id), true);
        setTask((t) => (t?.kind === "guide" ? { ...t, label: `Here we are: ${fixtureName(guide.fixture_id)}` } : t));
      }
      if (fetch) await fetchFromBack(fetch.sku);
    },
    [walker, fetchFromBack],
  );

  // ---- one conversational turn
  const ask = useCallback(
    async (raw: string, opts: { local?: boolean; focus?: string; shown?: string } = {}) => {
      const t = raw.trim();
      if (!t || busy) return;
      stopSpeaking();
      if (mic.listening) mic.stop();
      setText("");
      post({ role: "you", text: opts.shown ?? t });
      setBusy(true);
      const ctx = { cart: useTmoCart.getState().cart, budget_cents: useTmoCart.getState().budget_cents, focus: opts.focus ?? focus.current, device, known, declined: useWhisper.getState().declined };
      let r: AssocReply;
      if (opts.local) {
        await new Promise((res) => setTimeout(res, 250));
        r = respond(t, ctx);
      } else {
        const history = [...msgs.filter((m) => m.role !== "system").map((m) => ({ role: m.role === "you" ? ("user" as const) : ("assistant" as const), content: m.text })), { role: "user" as const, content: t }];
        try {
          const res = await fetch("/api/associate", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ messages: history.slice(-12), ctx }), signal: AbortSignal.timeout(15000) });
          if (!res.ok) throw new Error(String(res.status));
          r = (await res.json()) as AssocReply;
        } catch {
          r = respond(t, ctx);
        }
      }
      setBusy(false);
      focus.current = r.focus ?? focus.current;
      setDevice(r.device ?? device);
      const shown = r.actions.filter((a): a is Extract<AssocAction, { type: "show" }> => a.type === "show").flatMap((a) => a.skus);
      const added = r.actions.filter((a) => a.type === "add").map((a) => (a as { sku: string }).sku);
      post({ role: "magenta", text: r.text, skus: [...new Set([...shown, ...added])].slice(0, 4) });
      setChips(r.chips?.length ? r.chips : chipsFor({ ...ctx, cart: applyActions(ctx.cart, r.actions), focus: focus.current }));
      void say(r.text);
      void run(r.actions);
    },
    [busy, known, device, msgs, mic, post, say, run],
  );
  useEffect(() => {
    askRef.current = (t) => void ask(t);
  }, [ask]);

  // ---- start / check in
  const begin = () => {
    setStarted(true);
    live.current.started = true;
    const r = respond("hi", { cart, budget_cents: budget, known });
    const hello = fromWeb ? `${r.text.replace(/ What are you looking for( today)?\?$/, "")} I see you started a basket on the website with ${fromWeb} item${fromWeb > 1 ? "s" : ""}, so we can pick up from there.` : r.text;
    post({ role: "magenta", text: hello });
    void say(hello);
  };
  const toggleKnown = (v: boolean) => {
    setKnown(v);
    const line = v
      ? "Thanks for checking in, Maya. I can see your Pixel 7 and what you already own, so I'll tell you when something is a repeat or worth waiting for."
      : "You're checked out. I won't use your account for the rest of this visit.";
    post({ role: "system", text: v ? "Checked in with the Picky app as Maya. Magenta can now see her phone, payments and what she owns." : "Checked out. Account details hidden." });
    if (started) {
      post({ role: "magenta", text: line });
      void say(line);
    }
    setChips(chipsFor({ cart, budget_cents: budget, known: v, focus: focus.current }));
  };
  const endVisit = () => {
    stopSpeaking();
    if (mic.listening) mic.stop();
    setStarted(false);
    setMsgs([]);
    setTask(null);
    setExpert(null);
    setReceipt(null);
    focus.current = null;
    setDevice(null);
    walker.reset();
    setChips(chipsFor({ cart: [], budget_cents: budget, known }));
  };

  // ---- basket + Cart Whisperer (account-based cards only after check-in)
  const raw = useMemo(
    () =>
      suggest({ cart, intent: device ? `my new ${shortName(device)}` : undefined, budget_cents: budget, phase: "in_trip", declined: new Set(w.declined) }).filter(
        (c) => known || !c.reason_codes.some((r) => ACCOUNT_CODES.has(r)),
      ),
    [cart, budget, w.declined, known, device],
  );
  const { cards } = usePhrasing(raw);
  const accept = (s: Suggestion) => {
    w.accept(s);
    useTmoCart.getState().set({ cart: applySuggestion(useTmoCart.getState().cart, s) }, "store");
    toast(s.action === "remind" ? `I'll remind you on ${s.remind_on}` : "Done", { description: s.title });
  };
  const decline = (s: Suggestion) => {
    w.decline(s);
    toast("Okay, keeping it");
  };
  const total = cartTotal(cart);
  const dev = deviceOf({ cart, budget_cents: budget, known, device });

  const pay = async () => {
    const items = cart.reduce((n, l) => n + l.qty, 0);
    setCheckout(false);
    setReceipt({ total, items });
    useTmoCart.getState().set({ cart: [] }, "store");
    const line = `You're all set, ${known ? "Maya" : "thanks"}. Your receipt is in the Picky app. Follow me to the counter and I'll hand everything over.`;
    post({ role: "magenta", text: line });
    void say(line);
    setTask({ kind: "counter", label: "Walking you to the counter for pickup", fixture: "COUNTER" });
    await walker.walk(approachOf("COUNTER"), true);
    setTask({ kind: "counter", label: "At the counter. Have a great day!", fixture: "COUNTER" });
  };

  const mode: FaceMode = mic.listening ? "listening" : busy ? "thinking" : talking ? "talking" : task && walker.path.length ? "walking" : "idle";
  const status =
    mode === "listening" ? (mic.interim ? `“${mic.interim}”` : "Listening…") : mode === "thinking" ? "Thinking…" : mode === "walking" ? task?.label : mode === "talking" ? "Talking" : task?.label ?? (started ? "Ready. Ask me anything." : "Waiting by the door");

  return (
    <div className="flex flex-col gap-5 pb-16">
      <section className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-[14px] font-semibold text-magenta">In store · robot associate</p>
          <h1 className="mt-1 text-[30px] leading-tight font-bold md:text-[38px]">Talk to Magenta, right on the floor</h1>
          <p className="mt-1 max-w-[680px] text-[15px] text-muted-foreground">
            The robot greets you, answers out loud, walks you to the right wall, brings boxed phones out from the back, and tells you when to skip or wait. It gets a person for anything account-related.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <CheckIn known={known} onChange={toggleKnown} />
          <span className="rounded-full bg-muted px-3 py-1.5 text-[12px] font-medium text-muted-foreground" title={brain === "grok" ? "Grok picks the tools and phrases replies; every price is checked against the store data" : "Built-in answer engine over the store data (add XAI_API_KEY for Grok)"}>
            Brain: {brain === "grok" ? "Grok + store tools" : "built-in (offline)"}
          </span>
        </div>
      </section>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_400px]">
        {/* ------------------------------------------------ the robot's screen */}
        <Panel className="flex min-h-[620px] flex-col overflow-hidden">
          <div className="flex items-center gap-4 border-b border-border bg-[#17141a] px-5 py-4 text-white">
            <Face mode={mode} size={120} />
            <div className="min-w-0 flex-1">
              <div className="font-heading text-[22px] font-bold">Magenta</div>
              <div className="text-[13px] text-white/70">Robot associate · T-Mobile University Way NE</div>
              <div className="mt-2 flex items-center gap-2 text-[14px] font-medium" aria-live="polite">
                <span className={cn("size-2.5 shrink-0 rounded-full", mode === "listening" ? "live-dot bg-lime" : mode === "idle" ? "bg-white/40" : "live-dot bg-magenta")} aria-hidden />
                <span className="truncate">{status}</span>
              </div>
            </div>
            {started && (
              <Button variant="ghost" size="sm" className="shrink-0 text-white/80 hover:bg-white/10 hover:text-white" onClick={endVisit}>
                <X className="size-4" aria-hidden /> End visit
              </Button>
            )}
          </div>

          {!started ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-5 p-8 text-center">
              <Face mode="idle" size={200} />
              <div>
                <h2 className="text-[26px] font-bold">Hi! I&apos;m Magenta.</h2>
                <p className="mx-auto mt-1 max-w-[440px] text-[15px] text-muted-foreground">I can find anything in the store, walk you to it, check what fits your phone and bring sealed phones from the back.</p>
              </div>
              <Button size="lg" className="h-14 rounded-full bg-magenta px-8 text-[17px] text-white hover:bg-magenta-deep" onClick={begin}>
                <Mic className="size-5" aria-hidden /> Tap to talk to me
              </Button>
              {fromWeb > 0 && (
                <p className="rounded-full bg-magenta-wash px-4 py-1.5 text-[13px] font-medium text-magenta">
                  Your basket from the website ({fromWeb} item{fromWeb > 1 ? "s" : ""}) is ready to pick up here
                </p>
              )}
              <p className="text-[12px] text-muted-foreground">{canSpeak ? "Turn your sound on. " : ""}Voice works best in Chrome; you can always type.</p>
            </div>
          ) : (
            <>
              <div ref={list} className="min-h-[300px] flex-1 overflow-y-auto px-4 py-4 md:px-5" aria-live="polite">
                <ul className="flex flex-col gap-3">
                  <AnimatePresence initial={false}>
                    {msgs.map((m) => (
                      <motion.li key={m.id} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className={cn("flex flex-col gap-2", m.role === "you" ? "items-end" : "items-start")}>
                        {m.role === "system" ? (
                          <span className="self-center rounded-full bg-muted px-3 py-1 text-[12px] text-muted-foreground">{m.text}</span>
                        ) : (
                          <div className={cn("max-w-[86%] rounded-2xl px-4 py-2.5 text-[15.5px] leading-snug", m.role === "you" ? "rounded-br-md bg-foreground text-background" : "rounded-bl-md bg-magenta-wash text-foreground")}>{m.text}</div>
                        )}
                        {m.skus?.length ? (
                          <div className="grid w-full max-w-[640px] gap-2 sm:grid-cols-2">
                            {m.skus.map((s) => (
                              <ProductCard
                                key={s}
                                sku={s}
                                inCart={cart.some((l) => l.sku === s)}
                                fits={dev ? fitsLabel(s, dev.sku, dev.name) : null}
                                disabled={busy}
                                onWhere={() => void ask("Where is it?", { local: true, focus: s, shown: `Where's the ${nice(productOf(s)!.name)}?` })}
                                onAdd={() => void ask("Add it", { local: true, focus: s, shown: productOf(s)?.category === "phone" ? `I'll take the ${nice(productOf(s)!.name)}` : `Add the ${nice(productOf(s)!.name)}` })}
                              />
                            ))}
                          </div>
                        ) : null}
                      </motion.li>
                    ))}
                  </AnimatePresence>
                  {busy && (
                    <li className="flex gap-1.5 self-start rounded-2xl rounded-bl-md bg-magenta-wash px-4 py-3" aria-label="Magenta is thinking">
                      {[0, 1, 2].map((i) => (
                        <motion.span key={i} className="size-2 rounded-full bg-magenta" animate={{ opacity: [0.3, 1, 0.3] }} transition={{ duration: 0.9, repeat: Infinity, delay: i * 0.15 }} />
                      ))}
                    </li>
                  )}
                </ul>
              </div>

              <div className="border-t border-border p-3 md:p-4">
                <div className="mb-3 flex flex-wrap gap-2">
                  {chips.map((c) => (
                    <button key={c} disabled={busy} onClick={() => void ask(c, { local: /^(Add it|I'll take it|Where is it\?)/.test(c) })} className="cursor-pointer rounded-full bg-card px-3.5 py-2 text-[14px] font-medium ring-1 ring-foreground/10 hover:bg-muted disabled:opacity-50">
                      {c}
                    </button>
                  ))}
                </div>
                {mic.error && <p className="mb-2 rounded-xl bg-amber-wash px-3 py-1.5 text-[13px] text-amber-ink">{mic.error}</p>}
                <form
                  className="flex items-center gap-2"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void ask(text);
                  }}
                >
                  {mic.supported && (
                    <Button
                      type="button"
                      onClick={() => (mic.listening ? mic.stop() : (stopSpeaking(), mic.start()))}
                      className={cn("size-14 shrink-0 rounded-full text-white", mic.listening ? "bg-tomato hover:bg-tomato/90" : "bg-magenta hover:bg-magenta-deep")}
                      aria-label={mic.listening ? "Stop listening" : "Talk"}
                    >
                      {mic.listening ? <MicOff className="size-6" /> : <Mic className="size-6" />}
                    </Button>
                  )}
                  {(typing || !mic.supported) ? (
                    <>
                      <input
                        autoFocus={typing}
                        value={mic.listening ? mic.interim : text}
                        onChange={(e) => setText(e.target.value)}
                        placeholder={mic.listening ? "Listening…" : "Ask Magenta anything"}
                        aria-label="Message Magenta"
                        className="h-12 min-w-0 flex-1 rounded-full bg-muted px-5 text-[15.5px] outline-none focus:bg-card focus:ring-2 focus:ring-magenta"
                      />
                      <Button type="submit" disabled={!text.trim() || busy} className="size-12 shrink-0 rounded-full bg-foreground text-background" aria-label="Send">
                        <Send className="size-5" />
                      </Button>
                    </>
                  ) : (
                    <button type="button" onClick={() => setTyping(true)} className="flex h-12 flex-1 cursor-pointer items-center gap-2 rounded-full bg-muted px-5 text-left text-[15px] text-muted-foreground hover:bg-muted/70">
                      <Keyboard className="size-4" aria-hidden /> {mic.listening ? mic.interim || "Listening… just talk" : "Tap the mic and talk, or type here"}
                    </button>
                  )}
                </form>
                <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-[13px] text-muted-foreground">
                  <label className="flex cursor-pointer items-center gap-2">
                    <Switch checked={voiceOn} onCheckedChange={(v) => (setVoiceOn(v), v || stopSpeaking())} aria-label="Magenta speaks out loud" />
                    {voiceOn ? <Volume2 className="size-4" aria-hidden /> : <VolumeX className="size-4" aria-hidden />} Speak replies
                  </label>
                  {mic.supported && (
                    <label className="flex cursor-pointer items-center gap-2">
                      <Switch checked={handsFree} onCheckedChange={setHandsFree} aria-label="Hands-free conversation" />
                      Hands-free (listens after each reply)
                    </label>
                  )}
                </div>
              </div>
            </>
          )}
        </Panel>

        {/* ------------------------------------------------ floor, basket, people */}
        <aside className="flex flex-col gap-4">
          <Panel className="p-4">
            <div className="mb-2 flex items-center justify-between">
              <h2 className="text-[16px] font-semibold">On the floor</h2>
              <span className="flex items-center gap-3 text-[12px] text-muted-foreground">
                <span className="flex items-center gap-1">
                  <span className="size-2.5 rounded-full bg-magenta" aria-hidden /> Magenta
                </span>
                <span className="flex items-center gap-1">
                  <span className="size-2.5 rounded-full bg-sky" aria-hidden /> You
                </span>
              </span>
            </div>
            <StoreFloor robot={walker.robot} you={started ? walker.you : null} path={walker.path} target={task?.fixture ?? null} />
            <AnimatePresence>
              {task && (
                <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="mt-2 flex items-center gap-2 rounded-xl bg-magenta-wash px-3 py-2 text-[13.5px] font-medium text-magenta-deep">
                  {task.kind === "fetch" ? <PackageOpen className="size-4 shrink-0" aria-hidden /> : <Footprints className="size-4 shrink-0" aria-hidden />}
                  {task.label}
                </motion.div>
              )}
            </AnimatePresence>
          </Panel>

          <AnimatePresence>
            {expert && (
              <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
                <Panel className="flex items-start gap-3 border-l-4 border-sky p-4">
                  <Bell className="mt-0.5 size-5 shrink-0 text-sky" aria-hidden />
                  <div className="min-w-0 flex-1">
                    <div className="text-[15px] font-semibold">A T-Mobile expert is on the way</div>
                    <div className="text-[13px] text-muted-foreground">
                      About 3 minutes · paged {new Date(expert.at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })} · {expert.reason}
                    </div>
                  </div>
                  <Button size="sm" variant="ghost" onClick={() => (setExpert(null), toast("Expert request cancelled"))} aria-label="Cancel the expert request">
                    <BellOff className="size-4" />
                  </Button>
                </Panel>
              </motion.div>
            )}
          </AnimatePresence>

          <Panel className="p-4">
            <div className="flex items-center justify-between">
              <h2 className="flex items-center gap-2 text-[16px] font-semibold">
                <ShoppingBag className="size-4" aria-hidden /> Your basket
              </h2>
              <Link href="/tmobile/shop" className="text-[12.5px] font-semibold text-magenta hover:underline">
                Same basket online
              </Link>
            </div>
            {receipt && !cart.length ? (
              <div className="mt-3 flex items-start gap-2 rounded-xl bg-leaf-wash p-3 text-[13.5px] text-leaf-deep">
                <Check className="mt-0.5 size-4 shrink-0" aria-hidden />
                Paid {fmt(receipt.total)} for {receipt.items} item{receipt.items > 1 ? "s" : ""}. Receipt sent to the Picky app.
              </div>
            ) : !cart.length ? (
              <p className="mt-2 text-[13.5px] text-muted-foreground">Empty. Say &quot;I&apos;ll take it&quot; about anything and it lands here.</p>
            ) : (
              <>
                <ul className="mt-2 divide-y divide-border">
                  {cart.map((l) => {
                    const p = productOf(l.sku);
                    if (!p) return null;
                    return (
                      <li key={l.sku} className="flex items-center gap-3 py-2">
                        <ProductTile color={p.label_color} category={p.category} name={p.name} size={34} />
                        <div className="min-w-0 flex-1">
                          <div className="text-[13.5px] leading-tight font-semibold">{nice(p.name)}</div>
                          <div className="text-[12px] text-muted-foreground">
                            {l.qty > 1 ? `${l.qty} × ${fmt(unitPrice(l.sku))} · ` : ""}
                            {backStock(l.sku) ? "sealed box from the back" : `from ${fixtureName(displaySlot(l.sku)?.fixture_id ?? "")}`}
                          </div>
                        </div>
                        <Money cents={l.qty * unitPrice(l.sku)} className="text-[13.5px] font-semibold" />
                        <button onClick={() => useTmoCart.getState().setQty(l.sku, 0, "store")} className="grid size-7 cursor-pointer place-items-center rounded-full text-muted-foreground hover:bg-muted" aria-label={`Remove ${p.name}`}>
                          <X className="size-3.5" />
                        </button>
                      </li>
                    );
                  })}
                </ul>
                <div className="mt-2 flex items-center justify-between border-t border-border pt-2">
                  <span className="text-[13px] text-muted-foreground">Total</span>
                  <span className="text-[18px] font-bold tabular">{fmt(total)}</span>
                </div>
                <Button className="mt-3 w-full bg-magenta text-white hover:bg-magenta-deep" onClick={() => setCheckout(true)}>
                  Check out at the counter
                </Button>
              </>
            )}
          </Panel>

          {cart.length > 0 && (
            <div>
              <div className="mb-2 flex items-center justify-between">
                <h2 className="text-[16px] font-semibold">Cart Whisperer</h2>
                <span className="text-[11.5px] text-muted-foreground">{known ? "uses your account" : "check in for account-aware tips"}</span>
              </div>
              <WhisperList cards={cards} onAccept={accept} onDecline={decline} compact empty="Nothing to change." />
            </div>
          )}
        </aside>
      </div>

      <Dialog open={checkout} onOpenChange={setCheckout}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Check out with Magenta</DialogTitle>
            <DialogDescription>You&apos;re paying in person, so this is your call, not the robot&apos;s. Magenta walks you to the counter to hand everything over.</DialogDescription>
          </DialogHeader>
          <ul className="divide-y divide-border text-[14px]">
            {cart.map((l) => (
              <li key={l.sku} className="flex justify-between gap-3 py-2">
                <span>
                  {l.qty > 1 ? `${l.qty} × ` : ""}
                  {nice(productOf(l.sku)?.name ?? l.sku)}
                </span>
                <Money cents={l.qty * unitPrice(l.sku)} className="font-semibold" />
              </li>
            ))}
          </ul>
          {cards.some((c) => c.kind === "skip" || c.kind === "wait") && (
            <p className="rounded-xl bg-amber-wash px-3 py-2 text-[13px] text-amber-ink">
              Before you pay: {cards.filter((c) => c.kind === "skip" || c.kind === "wait").map((c) => c.title).join(" · ")}.
            </p>
          )}
          <div className="flex items-center justify-between text-[16px] font-bold">
            <span>Total</span>
            <span className="tabular">{fmt(total)}</span>
          </div>
          <p className="text-[11.5px] text-muted-foreground">Demo checkout: no real payment is taken. Tax and activation fees are added at the counter.</p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCheckout(false)}>
              Keep shopping
            </Button>
            <Button className="bg-magenta text-white hover:bg-magenta-deep" onClick={() => void pay()} disabled={!cart.length}>
              Pay {fmt(total)} with the Picky app
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function fitsLabel(sku: string, device: string, deviceName: string): { ok: boolean; text: string } | null {
  const c = productOf(sku)?.compatible_with ?? [];
  if (!c.length || productOf(sku)?.category === "phone") return null;
  return c.includes(device) ? { ok: true, text: `fits your ${deviceName}` } : { ok: false, text: `won't fit your ${deviceName}` };
}

function CheckIn({ known, onChange }: { known: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      onClick={() => onChange(!known)}
      className={cn("flex cursor-pointer items-center gap-2 rounded-full px-3.5 py-1.5 text-[13px] font-semibold ring-1 transition-colors", known ? "bg-leaf-wash text-leaf-deep ring-leaf/30" : "bg-card ring-foreground/15 hover:bg-muted")}
      aria-pressed={known}
    >
      {known ? <UserRoundCheck className="size-4" aria-hidden /> : <UserRound className="size-4" aria-hidden />}
      {known ? "Checked in as Maya · check out" : "Guest · check in with the Picky app"}
    </button>
  );
}

function ProductCard({ sku, inCart, fits, disabled, onWhere, onAdd }: { sku: string; inCart: boolean; fits: { ok: boolean; text: string } | null; disabled: boolean; onWhere: () => void; onAdd: () => void }) {
  const p = productOf(sku);
  if (!p) return null;
  const deal = dealOf(sku);
  const stock = stockOf(sku);
  const phone = p.category === "phone";
  const slot = displaySlot(sku);
  return (
    <div className="flex flex-col gap-2 rounded-2xl bg-card p-3 ring-1 ring-foreground/[0.08]">
      <div className="flex items-start gap-3">
        <ProductTile color={p.label_color} category={p.category} name={p.name} size={44} />
        <div className="min-w-0 flex-1">
          <div className="text-[14px] leading-tight font-semibold">{nice(p.name)}</div>
          <div className="mt-0.5 flex flex-wrap items-baseline gap-x-2 text-[13px]">
            <Money cents={unitPrice(sku)} className="font-semibold" />
            {deal && <s className="text-[11.5px] text-muted-foreground tabular">{fmt(deal.original_cents)}</s>}
            <span className={cn("text-[12px]", stock > 0 ? "text-muted-foreground" : "text-tomato")}>{stock > 0 ? `${stock} in stock` : "out of stock"}</span>
          </div>
          <div className="mt-0.5 text-[12px] text-muted-foreground">
            {phone ? `Demo unit on ${fixtureName(slot?.fixture_id ?? "")}${backStock(sku) ? " · sealed boxes in the back" : ""}` : `On ${fixtureName(slot?.fixture_id ?? "")}`}
            {fits && <span className={cn("ml-1 font-semibold", fits.ok ? "text-leaf-deep" : "text-tomato")}>· {fits.text}</span>}
            {p.compatible_with?.length && !fits ? <span className="ml-1">· for {p.compatible_with.map(shortName).join(", ")}</span> : null}
          </div>
        </div>
      </div>
      <div className="flex gap-2">
        <Button size="sm" variant="outline" className="flex-1" onClick={onWhere} disabled={disabled}>
          <Footprints className="size-4" aria-hidden /> Take me there
        </Button>
        <Button size="sm" className="flex-1 bg-magenta text-white hover:bg-magenta-deep" onClick={onAdd} disabled={disabled || inCart || stock <= 0}>
          {inCart ? (
            <>
              <Check className="size-4" aria-hidden /> In basket
            </>
          ) : phone ? (
            "I'll take it"
          ) : (
            "Add"
          )}
        </Button>
      </div>
    </div>
  );
}
