"use client";
import { Check, ShieldCheck, X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { Money, Panel, SectionTitle } from "@/components/picky";
import { Badge } from "@/components/ui/badge";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { fmtDate, maya } from "@/lib/data";
import { WIRELESS_MANDATE } from "@/lib/order";
import { useTrip } from "@/lib/trip";

const PREF_LABEL: Record<string, string> = {
  spicy: "Likes it spicy",
  vegetarian: "Leans vegetarian",
  mexican: "Cooks Mexican food often",
  fresh: "Prefers fresh over packaged",
  organic: "Buys some organic",
  plant_based: "Open to plant-based swaps",
  deals: "Watches for deals",
};

export default function RulesPage() {
  const m = maya.mandate;
  const [cap, setCap] = useState(m.cap_cents);
  const [ask, setAsk] = useState(m.ask_above_cents);
  const [techCap, setTechCap] = useState(WIRELESS_MANDATE.cap_cents);
  const [online, setOnline] = useState(m.allow_online);
  const [prefs, setPrefs] = useState(() => Object.entries(maya.profile.prefs).filter(([, v]) => v > 0.15));
  const approvals = useTrip((s) => s.approvalsLog);

  return (
    <div className="flex flex-col gap-8">
      <section className="animate-rise">
        <h1 className="text-[36px] leading-[1.05] font-bold md:text-[46px]">Rules and privacy</h1>
        <p className="mt-2 max-w-[62ch] text-[16px] text-muted-foreground">
          What Picky and the robot may do without asking you. These rules are checked by code before anything is bought, so the AI can&apos;t talk its way past
          them.
        </p>
      </section>

      <div className="grid gap-8 lg:grid-cols-12">
        <section className="lg:col-span-7">
          <SectionTitle>Spending rules</SectionTitle>
          <Panel className="divide-y divide-border">
            <SliderRow label="Grocery trip limit" help="Orders above this are blocked." value={cap} min={2000} max={15000} step={500} onChange={setCap} />
            <SliderRow label="Ask me first above" help="Any single item over this needs your yes, every time." value={ask} min={500} max={5000} step={100} onChange={setAsk} />
            <SliderRow label="Tech trip limit" help="For phones and accessories at T-Mobile." value={techCap} min={5000} max={50000} step={1000} onChange={setTechCap} />
            <div className="flex items-center justify-between gap-4 p-5">
              <div>
                <div className="font-semibold">Order out-of-stock items online</div>
                <div className="text-[13.5px] text-muted-foreground">They join the same checkout as the robot&apos;s basket.</div>
              </div>
              <Switch checked={online} onCheckedChange={setOnline} aria-label="Order out-of-stock items online" />
            </div>
            <div className="flex items-center justify-between gap-4 p-5">
              <div>
                <div className="font-semibold">Never buy</div>
                <div className="text-[13.5px] text-muted-foreground">Even if you ask in chat.</div>
              </div>
              <div className="flex gap-1.5">
                {m.blocked_categories.map((c) => (
                  <Badge key={c} variant="tomato" className="capitalize">
                    {c}
                  </Badge>
                ))}
              </div>
            </div>
            <div className="flex items-center gap-3 bg-leaf-wash p-5">
              <ShieldCheck className="size-6 shrink-0 text-leaf" aria-hidden />
              <p className="text-[14px]">
                <span className="font-semibold">Checkout always asks.</span> The robot never pays on its own, and payments are test-mode only in this demo. These
                rules expire {fmtDate(m.expires_at, { month: "long", day: "numeric", year: "numeric" })}.
              </p>
            </div>
          </Panel>
        </section>

        <div className="flex flex-col gap-8 lg:col-span-5">
          <Panel className="p-5">
            <h2 className="text-[17px] font-semibold">Your saved information</h2>
            <p className="mt-2 text-[14px] text-muted-foreground">Review, edit or delete the preferences and notes you saved by voice or text. They provide context for Ask Picky; spending rules still apply separately.</p>
            <Link href="/voice" className="mt-3 inline-flex min-h-10 items-center text-[14px] font-semibold text-leaf">Manage Voice & memory</Link>
          </Panel>
          <section>
            <SectionTitle>What Picky has learned</SectionTitle>
            <Panel className="p-5">
              <ul className="flex flex-wrap gap-2">
                {prefs.map(([k]) => (
                  <li key={k} className="flex items-center gap-1 rounded-full bg-muted py-1 pr-1 pl-3.5 text-[14px] font-medium">
                    {PREF_LABEL[k] ?? k}
                    <button
                      onClick={() => setPrefs((p) => p.filter(([x]) => x !== k))}
                      className="grid size-7 cursor-pointer place-items-center rounded-full text-muted-foreground hover:bg-tomato-wash hover:text-tomato"
                      aria-label={`Forget: ${PREF_LABEL[k] ?? k}`}
                    >
                      <X className="size-4" />
                    </button>
                  </li>
                ))}
                {prefs.length === 0 && <li className="text-muted-foreground">Nothing yet. Picky starts fresh.</li>}
              </ul>
              <p className="mt-4 text-[13.5px] text-muted-foreground">
                Learned from your ratings and follow-up texts. Tap the x to make Picky forget something. Nothing here is shared with stores, and sponsored
                suggestions are always labeled.
              </p>
            </Panel>
          </section>

          <section>
            <SectionTitle>Your recent answers</SectionTitle>
            <Panel className="p-2">
              {approvals.length === 0 ? (
                <p className="p-4 text-[14px] text-muted-foreground">When the robot asks you something on a trip, your answer shows up here.</p>
              ) : (
                approvals.map((a) => {
                  const yes = !(a.answer === "no" || a.answer === "decline");
                  return (
                    <div key={a.id + a.t} className="flex items-center gap-3 rounded-xl p-3">
                      <span className={yes ? "grid size-8 place-items-center rounded-full bg-leaf-wash text-leaf" : "grid size-8 place-items-center rounded-full bg-muted text-muted-foreground"}>
                        {yes ? <Check className="size-4" /> : <X className="size-4" />}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-[14px]">{a.label}</span>
                      <span className="text-[13px] font-medium text-muted-foreground">{a.answer.startsWith("inst_") ? "Picked one" : yes ? "Yes" : "No"}</span>
                    </div>
                  );
                })
              )}
            </Panel>
          </section>
        </div>
      </div>
    </div>
  );
}

function SliderRow(props: { label: string; help: string; value: number; min: number; max: number; step: number; onChange: (n: number) => void }) {
  return (
    <div className="p-5">
      <div className="flex items-start justify-between gap-6">
        <div>
          <div className="font-semibold">{props.label}</div>
          <div className="text-[13.5px] text-muted-foreground">{props.help}</div>
        </div>
        <Money cents={props.value} className="font-heading text-[24px] font-bold" />
      </div>
      <Slider
        className="mt-4"
        value={[props.value]}
        min={props.min}
        max={props.max}
        step={props.step}
        onValueChange={([v]) => props.onChange(v)}
        aria-label={props.label}
      />
    </div>
  );
}
