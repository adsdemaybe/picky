"use client";
import { useState } from "react";
import { Badge, Card, CardHead, PageHead, cx } from "@/components/ui";
import { fmtDate, maya } from "@/lib/data";
import { fmt } from "@/lib/money";
import { useTrip } from "@/lib/trip";

const PREF_LABEL: Record<string, string> = {
  spicy: "Likes it spicy",
  vegetarian: "Leans vegetarian",
  mexican: "Cooks Mexican often",
  fresh: "Prefers fresh over packaged",
  organic: "Some organic",
  plant_based: "Open to plant-based swaps",
  deals: "Watches for deals",
};

export default function PermissionsPage() {
  const m = maya.mandate;
  const [cap, setCap] = useState(m.cap_cents);
  const [ask, setAsk] = useState(m.ask_above_cents);
  const [perItem, setPerItem] = useState(m.per_item_cap_cents);
  const [online, setOnline] = useState(m.allow_online);
  const [prefs, setPrefs] = useState(() => Object.entries(maya.profile.prefs).filter(([, v]) => v > 0.15));
  const approvals = useTrip((s) => s.approvalsLog);

  return (
    <>
      <PageHead kicker="Permissions" title="What Picky may do without asking">
        <Badge tone="basil">Enforced in code · the AI can&apos;t change these</Badge>
      </PageHead>

      <div className="grid grid-cols-12 gap-6">
        <Card className="col-span-12 lg:col-span-7">
          <CardHead title="Spending rules" meta={`mandate ${m.mandate_id} · valid to ${fmtDate(m.expires_at, { month: "short", day: "numeric", year: "numeric" })}`} />
          <div className="divide-y divide-line">
            <Slider label="Trip cap" help="Orders above this are blocked outright." value={cap} min={2000} max={15000} step={500} onChange={setCap} />
            <Slider label="Ask me above" help="Any single item over this needs a yes from you, every time." value={ask} min={500} max={5000} step={100} onChange={setAsk} />
            <Slider label="Per-item cap" help="Nothing pricier than this, even with a yes." value={perItem} min={1000} max={10000} step={500} onChange={setPerItem} />
            <Toggle label="Online orders for out-of-stock items" help="Split them into one combined checkout." on={online} onChange={setOnline} />
            <Row label="Blocked categories" help="Never added, even if you ask in chat.">
              {m.blocked_categories.map((c) => (
                <Badge key={c} tone="tomato">
                  {c}
                </Badge>
              ))}
            </Row>
            <Row label="Checkout" help="The robot never pays on its own.">
              <Badge tone="ink">always asks</Badge>
            </Row>
          </div>
        </Card>

        <div className="col-span-12 flex flex-col gap-6 lg:col-span-5">
          <Card>
            <CardHead title="What Picky has learned about you" meta="delete anything" />
            <ul className="flex flex-wrap gap-2 p-4">
              {prefs.map(([k, v]) => (
                <li key={k} className="flex items-center gap-2 border border-line bg-paper py-1 pr-1 pl-2.5 text-[13.5px]">
                  {PREF_LABEL[k] ?? k}
                  <span className="num text-[11px] text-muted">{v.toFixed(1)}</span>
                  <button onClick={() => setPrefs((p) => p.filter(([x]) => x !== k))} className="px-1.5 text-muted hover:text-tomato" aria-label={`Forget ${k}`}>
                    ×
                  </button>
                </li>
              ))}
            </ul>
            <p className="border-t border-line px-4 py-2.5 text-[12.5px] text-muted">
              Learned from ratings and follow-up texts. Nothing is shared with stores. Sponsored suggestions are always labeled.
            </p>
          </Card>

          <Card>
            <CardHead title="Approval history" meta="this session" />
            {approvals.length === 0 ? (
              <p className="px-4 py-3 text-[13.5px] text-muted">No approvals yet. Run a trip and every yes or no lands here.</p>
            ) : (
              <ul className="divide-y divide-line">
                {approvals.map((a) => (
                  <li key={a.id + a.t} className="flex items-center justify-between gap-3 px-4 py-2 text-[13.5px]">
                    <span className="min-w-0 truncate">{a.label}</span>
                    <Badge tone={a.answer === "no" || a.answer === "decline" ? "neutral" : "basil"}>{a.answer.startsWith("inst_") ? "picked lot" : a.answer}</Badge>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </>
  );
}

function Slider(props: { label: string; help: string; value: number; min: number; max: number; step: number; onChange: (n: number) => void }) {
  return (
    <div className="grid grid-cols-[1fr_auto] items-center gap-x-6 gap-y-2 px-4 py-3.5">
      <div>
        <div className="font-medium">{props.label}</div>
        <div className="text-[12.5px] text-muted">{props.help}</div>
      </div>
      <div className="num text-[22px] font-medium">{fmt(props.value)}</div>
      <input
        type="range"
        min={props.min}
        max={props.max}
        step={props.step}
        value={props.value}
        onChange={(e) => props.onChange(Number(e.target.value))}
        className="col-span-2 accent-[#1c2420]"
        aria-label={props.label}
      />
    </div>
  );
}

function Toggle({ label, help, on, onChange }: { label: string; help: string; on: boolean; onChange: (b: boolean) => void }) {
  return (
    <Row label={label} help={help}>
      <button onClick={() => onChange(!on)} className={cx("relative h-6 w-11 border border-ink", on ? "bg-ink" : "bg-paper")} role="switch" aria-checked={on}>
        <span className={cx("absolute top-0.5 size-[18px] transition-all", on ? "left-[22px] bg-paper" : "left-0.5 bg-ink")} />
      </button>
    </Row>
  );
}

function Row({ label, help, children }: { label: string; help: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 px-4 py-3.5">
      <div>
        <div className="font-medium">{label}</div>
        <div className="text-[12.5px] text-muted">{help}</div>
      </div>
      <div className="flex gap-1.5">{children}</div>
    </div>
  );
}
