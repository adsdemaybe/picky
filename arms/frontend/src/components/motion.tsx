"use client";
// Motion helpers (motion.dev, the successor to framer-motion). Everything respects the user's reduced-motion setting.
import { MotionConfig, animate, motion, useInView, useMotionValue, useTransform, type Variants } from "motion/react";
import { useEffect, useRef, type ReactNode } from "react";
import { fmt } from "@/lib/money";

export const spring = { type: "spring", stiffness: 420, damping: 32 } as const;
export const soft = { type: "spring", stiffness: 220, damping: 26 } as const;

export function MotionProvider({ children }: { children: ReactNode }) {
  return (
    <MotionConfig reducedMotion="user" transition={soft}>
      {children}
    </MotionConfig>
  );
}

/** Fade + rise when scrolled into view (once). */
export function Reveal({ children, delay = 0, className, y = 16 }: { children: ReactNode; delay?: number; className?: string; y?: number }) {
  return (
    <motion.div
      className={className}
      initial={{ opacity: 0, y }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "-40px" }}
      transition={{ ...soft, delay }}
    >
      {children}
    </motion.div>
  );
}

export const listVariants: Variants = {
  hidden: {},
  show: { transition: { staggerChildren: 0.06, delayChildren: 0.05 } },
};
export const itemVariants: Variants = {
  hidden: { opacity: 0, y: 14, scale: 0.98 },
  show: { opacity: 1, y: 0, scale: 1, transition: soft },
};

/** Money that counts up to its value the first time it's on screen, and glides when it changes. */
export function CountUp({ cents, className }: { cents: number; className?: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true });
  const mv = useMotionValue(0);
  const text = useTransform(mv, (v) => fmt(Math.round(v)));
  useEffect(() => {
    if (!inView) return;
    const c = animate(mv, cents, { duration: 0.9, ease: [0.2, 0.8, 0.2, 1] });
    return () => c.stop();
  }, [cents, inView, mv]);
  return (
    <motion.span ref={ref} className={`tabular ${className ?? ""}`} aria-label={fmt(cents)}>
      {text}
    </motion.span>
  );
}

/** Burst of product-colored confetti, used once when a trip is paid. */
export function Confetti({ colors = ["#0f7a4a", "#d9f26b", "#2f5bea", "#f2a516", "#d93a30", "#7c4dcc"], count = 36 }: { colors?: string[]; count?: number }) {
  const pieces = Array.from({ length: count }, (_, i) => {
    const a = (i / count) * Math.PI * 2;
    const r = 90 + ((i * 37) % 80);
    return { i, x: Math.cos(a) * r, y: Math.sin(a) * r - 40, rot: (i * 47) % 360, c: colors[i % colors.length], w: 6 + (i % 3) * 3 };
  });
  return (
    <div className="pointer-events-none absolute inset-0 grid place-items-center overflow-visible" aria-hidden>
      {pieces.map((p) => (
        <motion.span
          key={p.i}
          className="absolute rounded-[2px]"
          style={{ width: p.w, height: p.w * 1.6, background: p.c }}
          initial={{ x: 0, y: 0, opacity: 1, rotate: 0, scale: 0.4 }}
          animate={{ x: p.x, y: [0, p.y, p.y + 120], opacity: [1, 1, 0], rotate: p.rot + 360, scale: 1 }}
          transition={{ duration: 1.6, ease: "easeOut", times: [0, 0.45, 1] }}
        />
      ))}
    </div>
  );
}
