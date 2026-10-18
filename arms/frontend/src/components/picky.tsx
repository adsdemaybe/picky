// Picky's product-level building blocks on top of shadcn/ui (see DESIGN.md).
import {
  Apple,
  Banana,
  Bean,
  Carrot,
  Citrus,
  Coffee,
  Droplet,
  Egg,
  Flame,
  GlassWater,
  IceCreamCone,
  Pizza,
  Popcorn,
  Wheat,
  Beef,
  Clock,
  Cookie,
  Croissant,
  CupSoda,
  Headphones,
  Milk,
  Package,
  PlugZap,
  Router,
  Sandwich,
  ShieldCheck,
  Smartphone,
  Snowflake,
  Soup,
  SprayCan,
  Star,
  ThumbsUp,
  Watch,
  Wine,
  X,
  type LucideIcon,
} from "lucide-react";
import { createElement, type ComponentProps, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { fmt } from "@/lib/money";
import type { Verdict } from "@/lib/prices";

const ICONS: Record<string, LucideIcon> = {
  produce: Apple,
  bakery: Croissant,
  pantry: Soup,
  meat: Beef,
  deli: Sandwich,
  dairy: Milk,
  frozen: Snowflake,
  snacks: Cookie,
  beverages: CupSoda,
  alcohol: Wine,
  household: SprayCan,
  phone: Smartphone,
  case: ShieldCheck,
  screen: Smartphone,
  charger: PlugZap,
  audio: Headphones,
  wearable: Watch,
  home_internet: Router,
};

/** More specific icons by product name, falling back to the category icon. */
const BY_NAME: [RegExp, LucideIcon][] = [
  [/coffee/i, Coffee],
  [/bean/i, Bean],
  [/egg/i, Egg],
  [/banana/i, Banana],
  [/lime|lemon|orange juice/i, Citrus],
  [/salsa|hot sauce|jalape|chile|chipotle/i, Flame],
  [/chips|popcorn|trail mix/i, Popcorn],
  [/water|sparkling/i, GlassWater],
  [/granola|cereal|oat|rice|pasta|spaghetti|tortilla|taco shells/i, Wheat],
  [/oil/i, Droplet],
  [/cheese|queso|cotija/i, Pizza],
  [/ice cream/i, IceCreamCone],
  [/onion|tomato|pepper|cilantro|lettuce|romaine/i, Carrot],
];

export function categoryIcon(category: string, name?: string): LucideIcon {
  if (name) for (const [re, Icon] of BY_NAME) if (re.test(name)) return Icon;
  return ICONS[category] ?? Package;
}

/** Relative luminance check so the icon on a product tile stays readable on light or dark label colors. */
function isLight(hex: string) {
  const h = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.62;
}

/** A product shown as a little colored packet: the catalog's label color plus a category icon. */
export function ProductTile({
  color,
  category,
  name,
  size = 44,
  className,
}: {
  color: string;
  category: string;
  name?: string;
  size?: number;
  className?: string;
}) {
  const light = isLight(color);
  return (
    <span
      aria-hidden
      className={cn("relative grid shrink-0 place-items-center rounded-xl ring-1", light ? "ring-black/15" : "ring-black/5", className)}
      style={{ width: size, height: size, background: color }}
    >
      {createElement(categoryIcon(category, name), {
        className: light ? "text-black/70" : "text-white/90",
        style: { width: size * 0.48, height: size * 0.48 },
        strokeWidth: 1.9,
      })}
    </span>
  );
}

export function Money({ cents, className, sign }: { cents: number; className?: string; sign?: boolean }) {
  return <span className={cn("tabular", className)}>{fmt(cents, { sign })}</span>;
}

const VERDICT_STYLE: Record<Verdict["label"], { cls: string; Icon: LucideIcon }> = {
  "Good price": { cls: "bg-lime text-lime-ink", Icon: ThumbsUp },
  Wait: { cls: "bg-amber-wash text-amber-ink", Icon: Clock },
  "Typical price": { cls: "bg-muted text-muted-foreground", Icon: Star },
  "High right now": { cls: "bg-tomato-wash text-tomato", Icon: Clock },
  "Skip it": { cls: "bg-tomato-wash text-tomato", Icon: X },
};

export function VerdictChip({ verdict, size = "sm", className }: { verdict: Verdict; size?: "sm" | "lg"; className?: string }) {
  const { cls, Icon } = VERDICT_STYLE[verdict.label];
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full font-semibold whitespace-nowrap",
        size === "lg" ? "h-8 px-3.5 text-[13.5px]" : "h-6 px-2.5 text-[12px]",
        cls,
        className,
      )}
    >
      <Icon className={size === "lg" ? "size-4" : "size-3.5"} strokeWidth={2.2} aria-hidden />
      {verdict.label}
    </span>
  );
}

/** Budget-style meter: rounded track, leaf fill, tomato when over; optional pace marker. */
export function Meter({ value, max, pace, className, tone = "leaf" }: { value: number; max: number; pace?: number; className?: string; tone?: "leaf" | "sky" }) {
  const pct = Math.min(100, (value / max) * 100);
  const over = value > max;
  return (
    <div className={cn("relative h-3 w-full rounded-full bg-muted", className)} role="meter" aria-valuenow={value} aria-valuemin={0} aria-valuemax={max}>
      <div
        className={cn("absolute inset-y-0 left-0 rounded-full transition-[width] duration-500", over ? "bg-tomato" : tone === "sky" ? "bg-sky" : "bg-leaf")}
        style={{ width: `${pct}%` }}
      />
      {pace !== undefined && (
        <div className="absolute -top-1 -bottom-1 w-0.5 rounded-full bg-foreground" style={{ left: `${Math.min(100, (pace / max) * 100)}%` }} aria-hidden />
      )}
    </div>
  );
}

export function SectionTitle({ children, action, className }: { children: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <div className={cn("mb-3 flex items-end justify-between gap-4", className)}>
      <h2 className="text-[20px] leading-tight font-semibold">{children}</h2>
      {action}
    </div>
  );
}

export function Panel({ className, ...rest }: ComponentProps<"section">) {
  return <section className={cn("rounded-2xl bg-card ring-1 ring-foreground/[0.07]", className)} {...rest} />;
}

export function Stars({ n }: { n: number | null }) {
  if (!n) return null;
  return (
    <span className="inline-flex items-center gap-0.5 text-amber" aria-label={`${n} of 5 stars`}>
      {Array.from({ length: 5 }, (_, i) => (
        <Star key={i} className={cn("size-3.5", i < n ? "fill-amber" : "fill-none text-input")} strokeWidth={1.8} />
      ))}
    </span>
  );
}
