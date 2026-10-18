"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTrip } from "@/lib/trip";
import { cx } from "./ui";

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/ask", label: "Ask Picky" },
  { href: "/find", label: "Find & compare" },
  { href: "/purchases", label: "Spending" },
  { href: "/order", label: "Robot order" },
  { href: "/trip", label: "Live trip" },
  { href: "/permissions", label: "Permissions" },
];

export function Rail() {
  const path = usePathname();
  const phase = useTrip((s) => s.phase);
  const live = phase === "shopping" || phase === "checkout" || phase === "waiting_login";
  return (
    <aside className="sticky top-0 flex h-dvh w-[208px] shrink-0 flex-col border-r border-ink bg-paper max-md:hidden">
      <Link href="/" className="flex items-baseline gap-2 border-b border-line px-5 py-5">
        <span className="font-display text-[26px] leading-none font-bold tracking-[-0.03em]">picky</span>
        <span className="size-2 translate-y-[-2px] bg-tomato" aria-hidden />
      </Link>
      <nav className="flex flex-col py-3">
        {NAV.map((n) => {
          const active = n.href === "/" ? path === "/" : path.startsWith(n.href);
          return (
            <Link
              key={n.href}
              href={n.href}
              className={cx(
                "group flex items-center justify-between border-l-[3px] px-5 py-2 text-[14px]",
                active ? "border-tomato bg-card font-semibold text-ink" : "border-transparent text-ink-2 hover:bg-card hover:text-ink",
              )}
            >
              <span>{n.label}</span>
              {n.href === "/trip" && live ? (
                <span className="flex items-center gap-1.5 font-mono text-[10px] text-tomato uppercase">
                  <span className="live-dot size-1.5 rounded-full bg-tomato" />
                  live
                </span>
              ) : null}
            </Link>
          );
        })}
      </nav>
      <div className="mt-auto border-t border-line px-5 py-4 text-[13px]">
        <div className="flex items-center gap-2.5">
          <span className="grid size-8 place-items-center bg-ink font-display text-[14px] font-bold text-paper">M</span>
          <div className="leading-tight">
            <div className="font-semibold">Maya</div>
            <div className="text-[12px] text-muted">Household of 2 · Seattle</div>
          </div>
        </div>
        <div className="mt-3 flex items-center gap-1.5 font-mono text-[10.5px] text-muted">
          <span className="size-1.5 bg-turmeric" />
          DEMO DATA · NO BACKEND
        </div>
      </div>
    </aside>
  );
}

export function MobileBar() {
  const path = usePathname();
  return (
    <div className="sticky top-0 z-20 flex items-center gap-4 overflow-x-auto border-b border-ink bg-paper px-4 py-3 md:hidden">
      <span className="font-display text-[20px] font-bold">picky</span>
      {NAV.map((n) => (
        <Link key={n.href} href={n.href} className={cx("text-[13px] whitespace-nowrap", (n.href === "/" ? path === "/" : path.startsWith(n.href)) ? "font-semibold underline decoration-tomato decoration-2 underline-offset-4" : "text-ink-2")}>
          {n.label}
        </Link>
      ))}
    </div>
  );
}
