"use client";
// "Where is…": product search in this robot's store (GET /api/products/search?q=&store_id=) with the shelf location
// from the store offer (catalog.ProductLocation: aisle, fixture, level, index). "Take me there" pins a one-slot
// shelf_scan goal on this robot (POST /api/fleet/robots/{id}/goals, end_at "stay"): the fleet has no "guide" goal
// kind, and a single-slot scan is the one that drives to that shelf and stays there without picking or charging.
import { Footprints, Loader2, MapPin, Search, ShoppingBasket, X } from "lucide-react";
import { motion } from "motion/react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Ingredients } from "@/components/Ingredients";
import { Money, ProductTile } from "@/components/picky";
import { productBySku } from "@/lib/data";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { errorText, gw, type S } from "@/lib/gateway/client";
import type { RobotInfo } from "@/lib/gateway/events";
import { useSpeechInput } from "@/lib/speech";
import { cn } from "@/lib/utils";
import { MicButton, type SpeakSetting } from "./chat";

export type Target = { sku: string; name: string; slotId: string; fixtureId: string; pos: { x: number; y: number } };

type Hit = S["SearchHit"];
type Found = { hit: Hit; offer: S["StoreOffer"] | null };

function aisleParts(label: string | null | undefined, aisleId: string | null | undefined) {
  const [name, ...rest] = (label ?? aisleId?.replace("aisle_", "Aisle ") ?? "").split(" - ");
  return { name: name || "Store", section: rest.join(" - ") || null };
}

export function WhereTab({ robot, onTarget, speech }: { robot: RobotInfo; onTarget: (t: Target | null) => void; speech: SpeakSetting }) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<Found[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const mic = useSpeechInput((heard) => setQ(heard.replace(/^(where (is|are)( the)?|find( me)?( the)?)\s+/i, "").replace(/\?$/, "")));

  // debounced search
  useEffect(() => {
    const text = q.trim();
    if (text.length < 2) return;
    let alive = true;
    const t = setTimeout(async () => {
      setLoading(true);
      setError(null);
      try {
        const { data } = await gw.search(text, robot.store_id, 8);
        if (!alive) return;
        setResults(data.map((hit) => ({ hit, offer: hit.offers.find((o) => o.store_id === robot.store_id) ?? null })));
      } catch (e) {
        if (alive) setError(errorText(e));
      } finally {
        if (alive) setLoading(false);
      }
    }, 300);
    return () => {
      alive = false;
      clearTimeout(t);
    };
  }, [q, robot.store_id]);

  const shown = q.trim().length >= 2 ? results : null;
  const sel = shown?.find((f) => f.hit.product.sku === selected) ?? null;

  const choose = (f: Found) => {
    setSelected(f.hit.product.sku);
    const loc = f.offer?.location;
    onTarget(
      loc
        ? { sku: f.hit.product.sku, name: f.hit.product.name, slotId: loc.slot_id, fixtureId: loc.fixture_id, pos: { x: loc.item_position.x, y: loc.item_position.y } }
        : null,
    );
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <form className="flex shrink-0 items-center gap-2 p-3" onSubmit={(e) => e.preventDefault()} role="search">
        <div className="relative flex min-w-0 flex-1 items-center">
          <Search className="pointer-events-none absolute left-4 size-5 text-muted-foreground" aria-hidden />
          <input
            value={mic.listening ? mic.interim : q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={mic.listening ? "Listening…" : "Salsa, oat milk, tortillas…"}
            aria-label="Search the store"
            className="h-12 w-full rounded-full bg-muted pr-12 pl-12 text-[16px] outline-none placeholder:text-muted-foreground focus:bg-card focus:ring-2 focus:ring-ring"
          />
          {q && (
            <button type="button" onClick={() => (setQ(""), setSelected(null), onTarget(null))} className="absolute right-2 grid size-9 cursor-pointer place-items-center rounded-full text-muted-foreground hover:bg-muted" aria-label="Clear search">
              <X className="size-4" />
            </button>
          )}
        </div>
        <MicButton mic={mic} />
      </form>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        {sel && <Location key={sel.hit.product.sku} found={sel} robot={robot} speech={speech} onClose={() => (setSelected(null), onTarget(null))} />}
        {mic.error && <p className="mb-2 rounded-xl bg-amber-wash px-3 py-1.5 text-[13.5px] text-amber-ink">{mic.error}</p>}
        {!shown ? (
          <p className="px-1 pt-2 text-[16px] text-muted-foreground">Type or say a product and I&apos;ll show you the aisle and shelf.</p>
        ) : error ? (
          <p role="alert" className="rounded-xl bg-tomato-wash px-3 py-2 text-[14px] text-tomato">
            Search isn&apos;t answering right now: {error}
          </p>
        ) : loading && !results?.length ? (
          <div className="flex flex-col gap-2">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-16 rounded-2xl" />
            ))}
          </div>
        ) : shown.length === 0 ? (
          <p className="px-1 pt-2 text-[16px] text-muted-foreground">I couldn&apos;t find “{q.trim()}” here. Try another word, or ask me in the chat.</p>
        ) : (
          <ul className={cn("flex flex-col gap-1.5", loading && "opacity-60")}>
            {shown.map((f, i) => {
              const p = f.hit.product;
              const loc = f.offer?.location;
              const { name } = aisleParts(loc?.aisle_label, loc?.aisle_id);
              return (
                <motion.li key={p.sku} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.03 * i }}>
                  <button
                    onClick={() => choose(f)}
                    aria-pressed={selected === p.sku}
                    className={cn(
                      "flex min-h-16 w-full cursor-pointer items-center gap-3 rounded-2xl px-3 py-2 text-left transition-colors hover:bg-muted",
                      selected === p.sku && "bg-leaf-wash ring-2 ring-leaf hover:bg-leaf-wash",
                    )}
                  >
                    <ProductTile color={p.asset?.label_color ?? "#d6dbd5"} category={p.category} name={p.name} size={44} />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[15.5px] font-semibold">{p.name}</span>
                      <span className="block truncate text-[13px] text-muted-foreground">{p.brand ?? p.size ?? ""}</span>
                    </span>
                    {f.offer ? (
                      f.offer.in_stock && loc ? (
                        <Badge variant="leaf" className="h-7 px-3 text-[13px]">
                          {name}
                        </Badge>
                      ) : (
                        <Badge variant="tomato">Out of stock here</Badge>
                      )
                    ) : (
                      <Badge variant="muted">Not sold here</Badge>
                    )}
                    {f.offer && <Money cents={f.offer.price_cents} className="w-16 text-right text-[15px] font-semibold" />}
                  </button>
                </motion.li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}

function Location({ found, robot, speech, onClose }: { found: Found; robot: RobotInfo; speech: SpeakSetting; onClose: () => void }) {
  const p = found.hit.product;
  const offer = found.offer;
  const loc = offer?.location ?? null;
  const [sending, setSending] = useState(false);
  const [sentFor, setSentFor] = useState<string | null>(null);
  const { name, section } = aisleParts(loc?.aisle_label, loc?.aisle_id);

  const going = sentFor === p.sku;
  const busyReason = going
    ? null
    : !robot.connected
    ? `${robot.name} is offline right now.`
    : robot.current_goal_id
      ? `${robot.name} is busy with an order. Follow the signs to ${name}.`
      : !robot.available
        ? robot.error ?? `${robot.name} can't leave right now${robot.battery_pct != null && robot.battery_pct < 20 ? " (battery low)" : ""}.`
        : null;

  const guide = async () => {
    if (!loc) return;
    setSending(true);
    try {
      await gw.robotGoal(robot.robot_id, {
        kind: "shelf_scan",
        slot_ids: [loc.slot_id],
        title: `Show a shopper ${p.name}`,
        instructions: `A shopper at my screen asked where ${p.name} is. Drive to slot ${loc.slot_id} and wait there with them.`,
        end_at: "stay",
        priority: "normal",
      });
      setSentFor(p.sku);
      const line = `Follow me to ${name}${section ? `, ${section}` : ""}.`;
      toast.success(line);
      speech.say(line);
    } catch (e) {
      toast.error(`I can't go there right now: ${errorText(e)}`);
    } finally {
      setSending(false);
    }
  };

  // walk-in shopper: the robot shops it into its basket; the checkout card then shows on this screen (Checkout tab)
  const [ordered, setOrdered] = useState<string | null>(null);
  const getIt = async () => {
    setSending(true);
    try {
      const plan = await gw.createRequest({
        text: `get me ${p.name}`,
        user_id: "guest",
        channel: "web",
        store_id: robot.store_id,
        role: "shopper",
      });
      setOrdered(p.sku);
      const line = plan.data.reply || `I will grab ${p.name}. Pay on my screen when I am back.`;
      toast.success(line);
      speech.say(line);
    } catch (e) {
      toast.error(`I can't get it right now: ${errorText(e)}`);
    } finally {
      setSending(false);
    }
  };

  return (
    <motion.section
      initial={{ opacity: 0, y: 10, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ type: "spring", stiffness: 380, damping: 30 }}
      className="mb-3 rounded-2xl bg-card p-4 shadow-md ring-2 ring-leaf"
      aria-label={`Where to find ${p.name}`}
    >
      <div className="flex items-start gap-3">
        <ProductTile color={p.asset?.label_color ?? "#d6dbd5"} category={p.category} name={p.name} size={56} />
        <div className="min-w-0 flex-1">
          <h3 className="text-[20px] leading-tight font-bold">{p.name}</h3>
          {offer && <Money cents={offer.price_cents} className="text-[15px] font-semibold text-muted-foreground" />}
        </div>
        <button onClick={onClose} className="grid size-9 shrink-0 cursor-pointer place-items-center rounded-full text-muted-foreground hover:bg-muted" aria-label="Close">
          <X className="size-4" />
        </button>
      </div>
      {loc && offer?.in_stock ? (
        <>
          <div className="mt-3 flex flex-wrap gap-2">
            {/^aisle\s/i.test(name) ? <Fact big label="Aisle" value={name.replace(/^aisle\s*/i, "")} /> : <Fact big label="Area" value={name} />}
            <Fact label="Shelf" value={`${loc.level + 1} from the bottom`} />
            <Fact label="Spot" value={`${loc.index + 1} along the shelf`} />
            <Fact label="On the shelf" value={`${offer.qty}`} />
          </div>
          {section && <p className="mt-2 text-[14px] text-muted-foreground">{section}</p>}
          {productBySku[p.sku] && <Ingredients p={productBySku[p.sku]} compact />}
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <Button size="lg" onClick={guide} disabled={sending || going || !!busyReason}>
              {sending ? <Loader2 className="animate-spin" /> : <Footprints />} {going ? "On my way, follow me" : "Take me there"}
            </Button>
            <Button size="lg" variant="outline" onClick={getIt} disabled={sending || ordered === p.sku || !robot.connected}>
              <ShoppingBasket /> {ordered === p.sku ? "Getting it, pay on Checkout" : "Get it for me"}
            </Button>
            <span className="flex items-center gap-1 text-[13.5px] text-muted-foreground">
              <MapPin className="size-4 text-tomato" aria-hidden /> {busyReason ?? (going ? `Heading to ${name}.` : "Marked on the map.")}
            </span>
          </div>
        </>
      ) : (
        <p className="mt-3 text-[15px] text-tomato">
          {offer ? "This one is out of stock in this store right now." : "This store doesn't carry it."} Ask me in the chat for something similar.
        </p>
      )}
    </motion.section>
  );
}

function Fact({ label, value, big }: { label: string; value: string; big?: boolean }) {
  return (
    <div className={cn("rounded-2xl px-3.5 py-2", big ? "bg-foreground text-background" : "bg-muted")}>
      <div className={cn("text-[12px]", big ? "text-background/70" : "text-muted-foreground")}>{label}</div>
      <div className={cn("tabular font-heading leading-tight font-bold", big ? "text-[26px]" : "text-[17px]")}>{value}</div>
    </div>
  );
}
