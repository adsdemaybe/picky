"use client";
// Hand-made animated illustrations (the fallbacks for the Lottie/image slots). Pure SVG + motion.
import { ArrowRight, Bot, MessageCircle, ScanSearch, ShoppingBasket, Smartphone } from "lucide-react";
import { motion } from "motion/react";
import { cn } from "@/lib/utils";

const ITEMS = ["#e8c15a", "#d93a30", "#2f6b45", "#f0a830", "#7c4dcc", "#2f5bea"];

/** Picky's robot: rolls in place, blinks, and keeps dropping groceries into its basket. */
export function RobotArt({ className, sleeping = false }: { className?: string; sleeping?: boolean }) {
  return (
    <svg viewBox="0 0 320 280" className={cn("h-full w-full", className)} role="img" aria-label={sleeping ? "Picky's robot, resting" : "Picky's robot shopping"}>
      {/* floor + shadow */}
      <ellipse cx="160" cy="258" rx="110" ry="12" fill="black" opacity="0.08" />
      {!sleeping &&
        [0, 1, 2].map((i) => (
          <motion.line
            key={i}
            x1={36}
            x2={70}
            y1={190 + i * 18}
            y2={190 + i * 18}
            stroke="var(--color-leaf)"
            strokeWidth="5"
            strokeLinecap="round"
            initial={{ opacity: 0, x: 10 }}
            animate={{ opacity: [0, 0.5, 0], x: [10, -14, -30] }}
            transition={{ duration: 1.2, repeat: Infinity, delay: i * 0.25, ease: "easeOut" }}
          />
        ))}

      <motion.g animate={sleeping ? { y: [0, 2, 0] } : { y: [0, -4, 0] }} transition={{ duration: sleeping ? 3 : 0.9, repeat: Infinity, ease: "easeInOut" }}>
        {/* antenna */}
        <line x1="160" y1="40" x2="160" y2="62" stroke="#111" strokeWidth="5" strokeLinecap="round" />
        <motion.circle
          cx="160"
          cy="34"
          r="9"
          fill="var(--color-lime)"
          stroke="#111"
          strokeWidth="4"
          animate={sleeping ? { opacity: [0.4, 0.8, 0.4] } : { scale: [1, 1.25, 1] }}
          transition={{ duration: 1.4, repeat: Infinity }}
          style={{ transformOrigin: "160px 34px" }}
        />
        {/* head */}
        <rect x="104" y="60" width="112" height="76" rx="26" fill="white" stroke="#111" strokeWidth="5" />
        <rect x="118" y="74" width="84" height="48" rx="16" fill="#111" />
        {/* eyes */}
        {sleeping ? (
          <>
            <path d="M136 99 q8 6 16 0" stroke="var(--color-lime)" strokeWidth="5" fill="none" strokeLinecap="round" />
            <path d="M168 99 q8 6 16 0" stroke="var(--color-lime)" strokeWidth="5" fill="none" strokeLinecap="round" />
            <motion.text x="214" y="58" fontSize="20" fontWeight="700" fill="#111" animate={{ opacity: [0, 1, 0], y: [0, -14, -24] }} transition={{ duration: 2.4, repeat: Infinity }}>
              z
            </motion.text>
          </>
        ) : (
          <motion.g animate={{ scaleY: [1, 1, 0.1, 1, 1] }} transition={{ duration: 3.2, repeat: Infinity, times: [0, 0.9, 0.93, 0.96, 1] }} style={{ transformOrigin: "160px 98px" }}>
            <circle cx="144" cy="98" r="8" fill="var(--color-lime)" />
            <circle cx="176" cy="98" r="8" fill="var(--color-lime)" />
          </motion.g>
        )}
        {/* body */}
        <rect x="96" y="140" width="128" height="84" rx="22" fill="var(--color-leaf)" stroke="#111" strokeWidth="5" />
        <circle cx="128" cy="168" r="7" fill="var(--color-lime)" />
        <rect x="146" y="162" width="52" height="12" rx="6" fill="white" opacity="0.85" />

        {/* arm reaching over the basket */}
        <motion.g
          animate={sleeping ? {} : { rotate: [0, -16, 0] }}
          transition={{ duration: 1.8, repeat: Infinity, ease: "easeInOut" }}
          style={{ transformOrigin: "222px 160px" }}
        >
          <path d="M222 160 C 252 150, 262 130, 258 112" stroke="#111" strokeWidth="9" fill="none" strokeLinecap="round" />
          <path d="M250 106 l8 6 l8 -6" stroke="#111" strokeWidth="5" fill="none" strokeLinecap="round" strokeLinejoin="round" />
        </motion.g>

        {/* basket in front */}
        <path d="M78 178 h96 l-10 44 h-76 z" fill="white" stroke="#111" strokeWidth="5" strokeLinejoin="round" />
        <path d="M96 178 l6 44 M126 178 v44 M156 178 l-6 44" stroke="#111" strokeWidth="3" opacity="0.25" />
        {/* groceries falling in */}
        {!sleeping &&
          ITEMS.map((c, i) => (
            <motion.rect
              key={i}
              width="20"
              height="26"
              rx="5"
              fill={c}
              stroke="#111"
              strokeWidth="3"
              initial={{ x: 96 + (i % 3) * 22, y: 90, opacity: 0 }}
              animate={{ y: [90, 158, 158], opacity: [0, 1, 0], rotate: [0, (i % 2 ? 1 : -1) * 18, 0] }}
              transition={{ duration: 2.4, repeat: Infinity, delay: i * 0.4, times: [0, 0.4, 1], ease: "easeIn" }}
            />
          ))}
      </motion.g>

      {/* wheels */}
      {[118, 202].map((cx) => (
        <motion.g key={cx} animate={sleeping ? {} : { rotate: 360 }} transition={{ duration: 1, repeat: Infinity, ease: "linear" }} style={{ transformOrigin: `${cx}px 238px` }}>
          <circle cx={cx} cy="238" r="18" fill="#111" />
          <circle cx={cx} cy="238" r="7" fill="var(--color-lime)" />
          <rect x={cx - 2} y="222" width="4" height="10" rx="2" fill="white" opacity="0.6" />
        </motion.g>
      ))}
    </svg>
  );
}

/** Banner art for store cards: a little shelf of product-colored packs that sway in. */
export function ShelfArt({ tint, colors, className }: { tint: string; colors: string[]; className?: string }) {
  return (
    <svg viewBox="0 0 400 120" preserveAspectRatio="xMidYMid slice" className={cn("h-full w-full", className)} aria-hidden>
      <rect width="400" height="120" fill={tint} />
      {[0, 1].map((row) => (
        <g key={row}>
          <rect x="0" y={62 + row * 50} width="400" height="6" fill="black" opacity="0.12" />
          {Array.from({ length: 12 }, (_, i) => {
            const c = colors[(i + row * 3) % colors.length];
            const h = 26 + ((i * 7 + row * 5) % 14);
            return (
              <motion.rect
                key={i}
                x={12 + i * 32}
                width="24"
                height={h}
                rx="5"
                fill={c}
                stroke="black"
                strokeOpacity="0.15"
                initial={{ y: 62 + row * 50 - h + 20, opacity: 0 }}
                animate={{ y: 62 + row * 50 - h, opacity: 1 }}
                transition={{ type: "spring", stiffness: 260, damping: 18, delay: 0.1 + i * 0.03 + row * 0.08 }}
              />
            );
          })}
        </g>
      ))}
    </svg>
  );
}

/** "How Picky works": three steps joined by a line that draws itself when scrolled into view. */
export function HowItWorks({ className }: { className?: string }) {
  const steps = [
    { icon: MessageCircle, title: "Tell Picky", body: "“Tacos for 6, under $40.” Text it, type it, or tap a list.", color: "bg-sky text-white" },
    { icon: ScanSearch, title: "It checks every price", body: "7 stores, 13 weeks of history, your budget and your rules.", color: "bg-lime text-lime-ink" },
    { icon: Bot, title: "The robot shops", body: "Shortest route, freshest pick, and it asks before any extra.", color: "bg-leaf text-white" },
  ];
  return (
    <div className={cn("relative", className)}>
      <svg className="pointer-events-none absolute top-7 left-[12%] hidden h-4 w-[76%] md:block" viewBox="0 0 100 4" preserveAspectRatio="none" aria-hidden>
        <motion.path
          d="M0 2 H100"
          stroke="var(--input)"
          strokeWidth="0.8"
          strokeDasharray="2 2"
          fill="none"
          initial={{ pathLength: 0 }}
          whileInView={{ pathLength: 1 }}
          viewport={{ once: true }}
          transition={{ duration: 1.2, ease: "easeInOut" }}
        />
      </svg>
      <ol className="relative grid gap-6 md:grid-cols-3">
        {steps.map((s, i) => (
          <motion.li
            key={s.title}
            className="flex flex-col items-start md:items-center md:text-center"
            initial={{ opacity: 0, y: 18 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ type: "spring", stiffness: 220, damping: 22, delay: 0.15 + i * 0.25 }}
          >
            <span className={cn("relative grid size-14 place-items-center rounded-2xl shadow-md", s.color)}>
              <s.icon className="size-7" aria-hidden />
              <span className="absolute -top-2 -right-2 grid size-6 place-items-center rounded-full bg-foreground text-[12px] font-bold text-background">{i + 1}</span>
            </span>
            <h3 className="mt-3 text-[18px] font-semibold">{s.title}</h3>
            <p className="mt-1 max-w-[30ch] text-[14px] text-muted-foreground">{s.body}</p>
            {i < 2 && <ArrowRight className="mt-3 size-5 text-input md:hidden" aria-hidden />}
          </motion.li>
        ))}
      </ol>
    </div>
  );
}

/** Square store thumbnail used until a real photo is dropped in: tinted tile, a few product packs, store icon. */
export function StoreThumb({ grocery }: { grocery: boolean }) {
  const packs = grocery ? ["#e8c15a", "#d93a30", "#2f6b45", "#9cc85a"] : ["#2b2f33", "#cfe3ea", "#7c4dcc", "#e9dfcf"];
  const Icon = grocery ? ShoppingBasket : Smartphone;
  return (
    <div className={cn("relative h-full w-full", grocery ? "bg-[#cfead9]" : "bg-[#e3d8f5]")}>
      <div className="absolute inset-x-2 bottom-2 flex items-end gap-1">
        {packs.map((c, i) => (
          <motion.span
            key={c}
            className="flex-1 rounded-[4px] ring-1 ring-black/10"
            style={{ background: c, height: 14 + ((i * 7) % 12) }}
            initial={{ y: 12, opacity: 0 }}
            animate={{ y: 0, opacity: 1 }}
            transition={{ type: "spring", stiffness: 300, damping: 18, delay: 0.05 * i }}
          />
        ))}
      </div>
      <span className={cn("absolute top-2 left-2 grid size-8 place-items-center rounded-lg text-white", grocery ? "bg-leaf" : "bg-grape")}>
        <Icon className="size-4.5" aria-hidden />
      </span>
    </div>
  );
}
