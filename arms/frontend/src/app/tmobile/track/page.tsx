"use client";
import { CheckCircle2 } from "lucide-react";
import Link from "next/link";
import { Panel, SectionTitle } from "@/components/picky";
import { Button } from "@/components/ui/button";
import { tmobileProducts, wireless } from "@/data/stores/tmobile";
import { MAX_CARDS, MIN_SCORE } from "@/lib/whisperer";
import { DIRECTIONS } from "../advisors/nav";

const RUBRIC = [
  {
    name: "Customer value",
    weight: 30,
    proof: [
      `At most ${MAX_CARDS} cards per phase, and zero is a valid answer ("Nothing worth adding today")`,
      "Explicit SKIP (you own a charger that works; that Pixel 7 case won't fit) and WAIT (Galaxy Buds4 $40 off Fri Oct 23; 4 payments left on the Pixel 7 and $200 off expected Nov 27)",
      "Every card shows the dollars saved, avoided (estimate) or added, and one tap accepts or declines it",
      "In store, Magenta does the floor associate's job: walks you to the right wall, brings your phone over from the table, and pages a person for account work",
      "Every advisor handles the no-good-recommendation case: keep your phone (Timekeeper, Matchmaker), you're already on the best plan (Household Mix), leave them alone (Next Best Moment)",
    ],
  },
  {
    name: "Use of signals",
    weight: 25,
    proof: [
      `Commerce: cart, ${tmobileProducts.length}-product catalog with real prices, shelf deals, stock, compatibility (fits: tags), promo calendar, real line offers`,
      "Customer: the phone she owns (34 months, 4 installments left), items she owns + purchase dates, lines on the account, budget, tech mandate, past declines",
      "Account & household: per-line usage and hotspot, current plan, tenure, support contacts, bill change, promo credits ending, phone health (battery, storage, security updates), trade-in values",
      "Physical: the slot each item sits in (the same planogram the Isaac Sim store is built from), where the shopper is standing, detour from the planned route (+0 m when it's on the same wall), what the robot just picked, checkout leg",
    ],
  },
  {
    name: "Explainability & trust",
    weight: 20,
    proof: [
      "Deterministic reason codes on every card (ALREADY_OWNED, INCOMPATIBLE, PROMO_SOON, INSTALLMENTS_LEFT, OVER_BUDGET, ...) plus a \"Why am I seeing this?\" signal list and the score",
      "The LLM (Grok, or Claude) only re-phrases the sentence; code checks every number against the card's signals and falls back to the template",
      "Talking in store, Grok chooses the tools, but every dollar amount in a spoken reply must appear in a tool result, or Magenta uses the built-in answer instead",
      "Nothing is sponsored, nothing is ranked by margin, and accepting a card is a normal cart change that still goes through the mandate and checkout approval",
    ],
  },
  {
    name: "Feasibility & demo",
    weight: 25,
    proof: [
      "Runs with no API key and no backend: the same cards every run (deterministic engine in lib/whisperer.ts) and a built-in in-store brain (lib/associate.ts); add XAI_API_KEY for Grok",
      "Voice in and out in the browser (speech recognition + natural TTS), hands-free mode, works on a phone or the robot's screen",
      "The advisors are real algorithms, not prompts: an exhaustive plan-assignment optimizer, a trade-in decay curve fitted to the trade-in table, an explained weighted risk score with a fix-first action policy",
      "The robot shops the store in the web app replay and in Isaac Sim with physics (rover + lift + SO-101 picking phones into the basket)",
      "Same data the backend uses: data/planograms/store_tmobile.yaml + data/fixtures/wireless.yaml, generated into the app",
    ],
  },
];

const EXAMPLES = [
  "“New Pixel 9 setup”: skip the charger, wait on the phone, two-cases catch",
  "Tap “Priya, student” or “Ruth, 55+” (keep your phone)",
  "Garcias: right-size, no discount · Tom: leave alone",
  "Maya: wait, save $262 · Teo: security updates ended",
  "Garcias −$82/mo · Nguyens: 3 accounts → 1 · Dev: +$30 that fits",
];

const CHECK = [
  "Leads with one direction, Cart Whisperer, and also ships the other four with worked examples (Advisors)",
  `Shows a phone-shopping store: T-Mobile, University Way NE (demo layout, ${tmobileProducts.length} real products, the same floor as the Isaac Sim store)`,
  "Shows SKIP and WAIT moments live, on the shop page and during the robot trip",
  "Says it plainly: synthetic shoppers, households, usage, support histories, plan prices and promo calendar",
  "In person too: a talking robot associate on the floor (In store), same brain, data and basket as the website",
];

const SCRIPT = [
  ["Shop → \"New Pixel 9 setup\"", "GoTo clear case $24.99 + GoTo glass $31.99 (20% off with a Pixel 9 case). Cards: SKIP a new charger (she owns a Samsung 25W from March), WAIT on the phone itself (4 payments left, $200 off expected Nov 27)."],
  ["Add the GoTo rugged case", "Quantity card: two cases for one Pixel 9? Keep the $24.99 clear case, drop the $39.99 rugged one. One tap: the cart and the metrics update."],
  ["Add the $9.99 Pixel 7 clearance case", "SKIP: it's made for the Pixel 7, it won't fit the Pixel 9. Clearance isn't a deal if it doesn't fit."],
  ["Add a Belkin power bank and AirPods", "Over budget: Cart Whisperer suggests the add that hurts least to drop, with the exact overage."],
  ["\"Running earbuds\"", "Galaxy Buds4 $179.99 + WAIT: $139.99 in the weekend sale from Fri Oct 23. Accept → \"I'll text you\". Then zero cards: Cart Whisperer stays quiet."],
  ["Shop with robot", "Magenta drives the store; tap \"grab the clearance case too\" → live SKIP. At checkout, the Before-you-pay pass and the metrics: accepted, saved, avoided."],
  ["In store → \"Tap to talk to me\"", "Ask \"where are the AirPods?\" out loud. Magenta answers and walks you to the audio wall on the live floor map."],
  ["Check in, then \"I'll take the Pixel 9\"", "She brings it over from the table, then is honest: 4 payments left and $200 off expected Nov 27, so wait. \"Do I need a charger?\" Skip it, you own one."],
  ["\"Can I port my number?\" → \"Check out\"", "Account work needs a person, so she pages a T-Mobile expert. Checkout: pay with the app, she walks you to the counter."],
];

export default function TmoTrack() {
  return (
    <div className="flex flex-col gap-8 pb-16">
      <section>
        <p className="text-[14px] font-semibold text-magenta">DubHacks 2026 · T-Mobile track</p>
        <h1 className="mt-1 text-[36px] leading-tight font-bold md:text-[46px]">Cart Whisperer: checkout suggestions that actually help</h1>
        <p className="mt-2 max-w-[760px] text-[16px] text-muted-foreground">
          A suggestion only counts if it completes what you&apos;re setting up, saves money for the same need, prevents a mistake, or stops a purchase that doesn&apos;t help. Adding more stuff isn&apos;t a suggestion.
        </p>
        <div className="mt-4 flex gap-2">
          <Button asChild className="bg-magenta text-white hover:bg-magenta-deep">
            <Link href="/tmobile/shop">Try it</Link>
          </Button>
          <Button asChild variant="outline">
            <Link href="/tmobile/store">Talk to the robot</Link>
          </Button>
          <Button asChild variant="outline">
            <Link href="/tmobile/trip">Robot trip</Link>
          </Button>
        </div>
      </section>

      <section>
        <SectionTitle>Track checklist</SectionTitle>
        <ul className="grid gap-2 md:grid-cols-2">
          {CHECK.map((c) => (
            <li key={c} className="flex gap-2 rounded-xl bg-card p-3 text-[14px] ring-1 ring-foreground/[0.07]">
              <CheckCircle2 className="size-5 shrink-0 text-leaf" aria-hidden />
              {c}
            </li>
          ))}
        </ul>
      </section>

      <section>
        <SectionTitle>One agent, five skills: open each skill&apos;s engine and its worked examples</SectionTitle>
        <div className="grid gap-2 md:grid-cols-5">
          {DIRECTIONS.map((d, i) => (
            <Link key={d.href} href={d.href} className="rounded-xl bg-card p-3 ring-1 ring-foreground/[0.07] hover:ring-magenta">
              <div className="flex items-center gap-2 text-[14px] font-semibold">
                <d.Icon className="size-4 text-magenta" aria-hidden /> {d.name}
              </div>
              <div className="mt-1 text-[12.5px] text-muted-foreground">{EXAMPLES[i]}</div>
            </Link>
          ))}
        </div>
      </section>

      <section>
        <SectionTitle>Where each rubric point comes from</SectionTitle>
        <div className="grid gap-3 md:grid-cols-2">
          {RUBRIC.map((r) => (
            <Panel key={r.name} className="p-5">
              <div className="flex items-baseline justify-between">
                <h3 className="text-[18px] font-semibold">{r.name}</h3>
                <span className="text-[24px] font-bold text-magenta tabular">{r.weight}%</span>
              </div>
              <ul className="mt-2 space-y-1.5 text-[14px]">
                {r.proof.map((p) => (
                  <li key={p} className="flex gap-2">
                    <span className="mt-2 size-1.5 shrink-0 rounded-full bg-magenta" aria-hidden />
                    {p}
                  </li>
                ))}
              </ul>
            </Panel>
          ))}
        </div>
        <p className="mt-2 text-[12px] text-muted-foreground">Weights per the team&apos;s copy of the challenge brief.</p>
      </section>

      <section>
        <SectionTitle>How a card gets shown</SectionTitle>
        <ol className="grid gap-2 md:grid-cols-5">
          {[
            ["1 Candidates", "complete · swap/save · qty fix · over budget · skip · wait, each from code over the cart, account and store"],
            ["2 Hard gates", "in stock, fits the device, not owned, mandate per-item cap, not already declined, not already in the cart"],
            ["3 Score + reason codes", `deterministic [0, 1]; below ${MIN_SCORE} nothing is shown; ties break by SKU`],
            ["4 Phrasing", "Grok or Claude rewrites the sentence from derived signals only; numbers are validated, else the template"],
            [`5 Top 0–${MAX_CARDS}`, "one card per SKU; accept = normal cart change → mandate → approval; decline = never re-asked this trip"],
          ].map(([h, b]) => (
            <li key={h} className="rounded-xl bg-card p-3 ring-1 ring-foreground/[0.07]">
              <div className="text-[14px] font-semibold">{h}</div>
              <div className="mt-1 text-[12.5px] text-muted-foreground">{b}</div>
            </li>
          ))}
        </ol>
      </section>

      <section>
        <SectionTitle>Demo script (about 3 minutes)</SectionTitle>
        <Panel className="divide-y divide-border">
          {SCRIPT.map(([a, b], i) => (
            <div key={a} className="grid gap-1 p-4 md:grid-cols-[28px_260px_1fr]">
              <span className="font-bold text-magenta tabular">{i + 1}</span>
              <span className="text-[14px] font-semibold">{a}</span>
              <span className="text-[14px] text-muted-foreground">{b}</span>
            </div>
          ))}
        </Panel>
      </section>

      <section>
        <SectionTitle>Data, honestly</SectionTitle>
        <Panel className="p-5 text-[14px]">
          <ul className="space-y-1.5">
            <li><b>Synthetic:</b> the shopper (Maya), her devices, owned items, installments and lines; the promo calendar; the store floor plan.</li>
            <li><b>Real (public snapshot, Oct 17 2026):</b> product names, brands and prices; the line offers, which are bill credits that need a new line or upgrade, so we never show them as shelf prices.</li>
            <li><b>Privacy:</b> cards carry derived signals only, and that is all the LLM sees. In store, Magenta uses your account only after you check in with the app, and forgets it when you check out or end the visit. Model calls go to xAI (Grok) or Anthropic when a key is set.</li>
            <li><b>Sources:</b> {wireless.sources.join(" · ")}. Trademarks belong to their owners; no affiliation or sponsorship.</li>
          </ul>
        </Panel>
      </section>
    </div>
  );
}
