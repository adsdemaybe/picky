// One component vocabulary for the whole app: bordered, square-ish, no shadows (see APP_PLAN.md §2).
import type { ComponentProps, ReactNode } from "react";
import { fmt } from "@/lib/money";

const cx = (...c: (string | false | null | undefined)[]) => c.filter(Boolean).join(" ");

type CardVariant = "default" | "inset" | "flat";
export function Card({
  variant = "default",
  accent = "tomato",
  className,
  ...rest
}: ComponentProps<"section"> & { variant?: CardVariant; accent?: "tomato" | "basil" | "turmeric" | "plum" }) {
  const accentCls = { tomato: "border-l-tomato", basil: "border-l-basil", turmeric: "border-l-turmeric", plum: "border-l-plum" }[accent];
  return (
    <section
      className={cx(
        variant === "default" && "border border-line bg-card",
        variant === "inset" && cx("border border-line border-l-[3px] bg-card", accentCls),
        variant === "flat" && "bg-sunk",
        "rounded-sm",
        className,
      )}
      {...rest}
    />
  );
}

export function CardHead({ title, meta, children }: { title: ReactNode; meta?: ReactNode; children?: ReactNode }) {
  return (
    <header className="flex items-baseline justify-between gap-4 border-b border-line px-4 py-2.5">
      <h2 className="font-display text-[15px] font-semibold tracking-tight">{title}</h2>
      {meta && <div className="label-caps">{meta}</div>}
      {children}
    </header>
  );
}

type BtnVariant = "primary" | "secondary" | "ghost" | "quiet";
export function Button({ variant = "secondary", className, ...rest }: ComponentProps<"button"> & { variant?: BtnVariant }) {
  return (
    <button
      className={cx(
        "inline-flex items-center justify-center gap-2 rounded-sm px-3.5 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-45",
        variant === "primary" && "bg-tomato text-white hover:bg-tomato-deep",
        variant === "secondary" && "border border-ink bg-card text-ink hover:bg-ink hover:text-paper",
        variant === "ghost" && "border border-line bg-transparent text-ink-2 hover:border-ink hover:text-ink",
        variant === "quiet" && "px-2 py-1 text-ink-2 underline decoration-line-strong underline-offset-4 hover:text-ink hover:decoration-ink",
        className,
      )}
      {...rest}
    />
  );
}

type Tone = "neutral" | "basil" | "tomato" | "turmeric" | "plum" | "ink";
export function Badge({ tone = "neutral", children, className }: { tone?: Tone; children: ReactNode; className?: string }) {
  const t = {
    neutral: "border-line text-ink-2 bg-paper",
    basil: "border-basil/40 text-basil bg-basil-wash",
    tomato: "border-tomato/40 text-tomato-deep bg-tomato-wash",
    turmeric: "border-turmeric/40 text-[#7a5408] bg-turmeric-wash",
    plum: "border-plum/40 text-plum bg-plum-wash",
    ink: "border-ink text-paper bg-ink",
  }[tone];
  return (
    <span className={cx("inline-flex items-center gap-1 rounded-sm border px-1.5 py-px font-mono text-[11px] leading-[18px] tracking-wide", t, className)}>
      {children}
    </span>
  );
}

export function Money({ cents, className, sign }: { cents: number; className?: string; sign?: boolean }) {
  return <span className={cx("num", className)}>{fmt(cents, { sign })}</span>;
}

/** Budget meter: a hairline track, ink fill, tomato past 100%. */
export function Meter({ value, max, className, marks }: { value: number; max: number; className?: string; marks?: number[] }) {
  const pct = Math.min(100, (value / max) * 100);
  const over = value > max;
  return (
    <div className={cx("relative h-2 w-full bg-sunk", className)} role="meter" aria-valuenow={value} aria-valuemax={max} aria-valuemin={0}>
      <div className={cx("absolute inset-y-0 left-0", over ? "bg-tomato" : "bg-ink")} style={{ width: `${pct}%` }} />
      {marks?.map((m) => (
        <div key={m} className="absolute inset-y-[-3px] w-px bg-ink-2" style={{ left: `${Math.min(100, (m / max) * 100)}%` }} />
      ))}
    </div>
  );
}

export function Stars({ n }: { n: number | null }) {
  if (!n) return <span className="text-muted">·</span>;
  return (
    <span className="num text-[12px] tracking-tight text-turmeric" aria-label={`${n} of 5`}>
      {"★".repeat(n)}
      <span className="text-line-strong">{"★".repeat(5 - n)}</span>
    </span>
  );
}

export function Swatch({ color, className }: { color: string; className?: string }) {
  return <span className={cx("inline-block size-2.5 shrink-0 rounded-[1px] border border-ink/15", className)} style={{ background: color }} />;
}

export function PageHead({ kicker, title, children }: { kicker: string; title: ReactNode; children?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4 border-b border-ink pb-4">
      <div>
        <div className="label-caps mb-1">{kicker}</div>
        <h1 className="font-display text-[34px] leading-[1.05] font-semibold tracking-[-0.02em]">{title}</h1>
      </div>
      {children}
    </div>
  );
}

export { cx };
