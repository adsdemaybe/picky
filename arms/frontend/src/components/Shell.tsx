"use client";
import { Bot, ChartPie, House, LogOut, ScanSearch, Search, ShieldHalf, Sparkles, Store, Video } from "lucide-react";
import { motion } from "motion/react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
import { GatewayChip } from "@/components/GatewayBits";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAsk } from "@/lib/assistant/store";
import { useTrip } from "@/lib/trip";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/", label: "Home", icon: House, match: (p: string) => p === "/" },
  { href: "/find", label: "Find", icon: ScanSearch, match: (p: string) => p.startsWith("/find") },
  { href: "/purchases", label: "Spending", icon: ChartPie, match: (p: string) => p.startsWith("/purchases") },
  { href: "/order", label: "Robot", icon: Bot, match: (p: string) => p.startsWith("/order") || p.startsWith("/trip") || p.startsWith("/watch") },
  { href: "/live", label: "Live", icon: Video, match: (p: string) => p.startsWith("/live") },
  { href: "/ask", label: "Ask", icon: Sparkles, match: (p: string) => p.startsWith("/ask") },
];
// phones get five tabs; Ask stays one tap away in the search box
const MOBILE_NAV = NAV.filter((n) => n.href !== "/ask");

const QUESTION = /\?$|^(is|are|can|could|should|where|what|how|why|when|do|does|did|will|am|show|tell|help)\b/i;

function Omnibox({ className }: { className?: string }) {
  const [q, setQ] = useState("");
  const router = useRouter();
  const ask = useAsk((s) => s.ask);
  return (
    <form
      role="search"
      className={cn("relative flex items-center", className)}
      onSubmit={(e) => {
        e.preventDefault();
        const text = q.trim();
        if (!text) return;
        if (QUESTION.test(text) || text.split(/\s+/).length > 4) ask(text);
        else router.push(`/find?q=${encodeURIComponent(text)}`);
        setQ("");
      }}
    >
      <Search className="pointer-events-none absolute left-4 size-[18px] text-muted-foreground" aria-hidden />
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search a product, or ask Picky anything"
        aria-label="Search a product or ask Picky"
        className="h-11 w-full rounded-full bg-muted pr-24 pl-11 text-[15px] outline-none placeholder:text-muted-foreground focus:bg-card focus:ring-2 focus:ring-ring"
      />
      <Button type="submit" size="sm" variant="dark" className="absolute right-1.5">
        Go
      </Button>
    </form>
  );
}

export function TopBar() {
  const path = usePathname();
  const phase = useTrip((s) => s.phase);
  const live = phase === "shopping" || phase === "checkout" || phase === "waiting_login";
  return (
    <header className="sticky top-0 z-30 border-b border-border/70 bg-background/85 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-[1240px] items-center gap-6 px-4 md:px-8">
        <Link href="/" className="flex items-center gap-2" aria-label="Picky home">
          <span className="grid size-9 place-items-center rounded-xl bg-leaf text-white">
            <svg viewBox="0 0 24 24" className="size-5" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M5 9h14l-1.4 9.2a2 2 0 0 1-2 1.8H8.4a2 2 0 0 1-2-1.8L5 9Z" />
              <path d="M9 9V7a3 3 0 0 1 6 0v2" />
              <circle cx="16.5" cy="5" r="1.6" fill="var(--color-lime)" stroke="none" />
            </svg>
          </span>
          <span className="font-heading text-[23px] font-bold tracking-tight">picky</span>
        </Link>

        <Omnibox className="hidden max-w-[460px] flex-1 md:flex" />

        <nav className="ml-auto hidden items-center gap-1 md:flex" aria-label="Main">
          {NAV.map((n) => {
            const active = n.match(path);
            return (
              <Link
                key={n.href}
                href={n.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "relative flex h-10 items-center gap-2 rounded-full px-3.5 text-[14px] font-medium transition-colors",
                  active ? "text-background" : "text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
              >
                {active && (
                  <motion.span layoutId="nav-pill" className="absolute inset-0 -z-0 rounded-full bg-foreground" transition={{ type: "spring", stiffness: 500, damping: 38 }} />
                )}
                <span className="relative">{n.label}</span>
                {n.label === "Robot" && live && <span className="live-dot relative size-2 rounded-full bg-lime" aria-label="robot is shopping" />}
              </Link>
            );
          })}
        </nav>

        <GatewayChip />

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button className="ml-auto cursor-pointer rounded-full md:ml-0" aria-label="Account">
              <Avatar className="size-10">
                <AvatarFallback className="bg-sky-wash font-heading text-[15px] font-semibold text-sky">M</AvatarFallback>
              </Avatar>
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-60">
            <DropdownMenuLabel>
              <div className="font-semibold">Maya</div>
              <div className="text-[12.5px] font-normal text-muted-foreground">Household of 2 in Seattle</div>
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem asChild>
              <Link href="/permissions">
                <ShieldHalf /> Rules and privacy
              </Link>
            </DropdownMenuItem>
            <DropdownMenuItem asChild>
              <Link href="/staff">
                <Store /> Store staff (live stock)
              </Link>
            </DropdownMenuItem>
            <DropdownMenuItem asChild>
              <Link href="/fleet">
                <Bot /> Fleet (robots and goals)
              </Link>
            </DropdownMenuItem>
            <DropdownMenuItem disabled>
              <LogOut /> Demo account (fictional data)
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      <div className="px-4 pb-3 md:hidden">
        <Omnibox />
      </div>
    </header>
  );
}

export function BottomTabs() {
  const path = usePathname();
  const phase = useTrip((s) => s.phase);
  const live = phase === "shopping" || phase === "checkout" || phase === "waiting_login";
  return (
    <nav className="fixed inset-x-0 bottom-0 z-30 border-t border-border bg-card/95 pb-[env(safe-area-inset-bottom)] backdrop-blur md:hidden" aria-label="Main">
      <ul className="grid grid-cols-5">
        {MOBILE_NAV.map((n) => {
          const active = n.match(path);
          const Icon = n.icon;
          return (
            <li key={n.href}>
              <Link
                href={n.href}
                aria-current={active ? "page" : undefined}
                className={cn("relative flex h-16 flex-col items-center justify-center gap-1 text-[11.5px] font-medium", active ? "text-leaf" : "text-muted-foreground")}
              >
                {active && <motion.span layoutId="tab-dot" className="absolute top-1.5 h-1 w-6 rounded-full bg-leaf" />}
                <motion.span animate={{ scale: active ? 1.12 : 1 }} transition={{ type: "spring", stiffness: 500, damping: 25 }}>
                  <Icon className="size-[22px]" strokeWidth={active ? 2.3 : 1.9} aria-hidden />
                </motion.span>
                {n.label}
                {n.label === "Robot" && live && <span className="live-dot absolute top-3 right-[calc(50%-18px)] size-2 rounded-full bg-tomato" />}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
