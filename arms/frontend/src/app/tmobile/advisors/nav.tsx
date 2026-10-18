"use client";
// The five challenge directions, side by side: Cart Whisperer (Shop) + the four advisors.
import { CalendarClock, HeartHandshake, MessagesSquare, ShoppingCart, Users } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

export const DIRECTIONS = [
  { href: "/tmobile/shop", name: "Cart Whisperer", line: "Checkout suggestions that actually help", Icon: ShoppingCart },
  { href: "/tmobile/match", name: "The Matchmaker", line: "Chat your way to the right phone", Icon: MessagesSquare },
  { href: "/tmobile/moment", name: "Next Best Moment", line: "Catch someone before they walk", Icon: HeartHandshake },
  { href: "/tmobile/upgrade", name: "The Timekeeper", line: "Say “don’t upgrade yet” and mean it", Icon: CalendarClock },
  { href: "/tmobile/household", name: "Household Mix", line: "Fix the whole family, not one line", Icon: Users },
];

export function AdvisorNav() {
  const path = usePathname();
  return (
    <nav aria-label="Challenge directions" className="-mx-4 flex gap-2 overflow-x-auto px-4 pb-1 md:mx-0 md:px-0">
      {DIRECTIONS.map((d) => {
        const on = path.startsWith(d.href);
        return (
          <Link
            key={d.href}
            href={d.href}
            aria-current={on ? "page" : undefined}
            className={cn("flex shrink-0 items-center gap-2 rounded-full px-3.5 py-2 text-[13.5px] font-semibold ring-1 transition-colors", on ? "bg-magenta text-white ring-magenta" : "bg-card ring-foreground/10 hover:bg-muted")}
          >
            <d.Icon className="size-4" aria-hidden />
            {d.name}
          </Link>
        );
      })}
    </nav>
  );
}

export function AdvisorHeader({ kicker, title, sub }: { kicker: string; title: string; sub: string }) {
  return (
    <section className="flex flex-col gap-3">
      <AdvisorNav />
      <div>
        <p className="text-[14px] font-semibold text-magenta">{kicker}</p>
        <h1 className="mt-1 text-[30px] leading-tight font-bold md:text-[38px]">{title}</h1>
        <p className="mt-1 max-w-[760px] text-[15px] text-muted-foreground">{sub}</p>
      </div>
    </section>
  );
}
