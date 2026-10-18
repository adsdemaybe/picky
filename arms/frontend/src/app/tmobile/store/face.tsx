"use client";
// Magenta's face on the robot's screen: blinks when idle, looks up while listening, glances around while thinking,
// talks with its mouth while speaking, and looks ahead while walking.
import { motion } from "motion/react";

export type FaceMode = "idle" | "listening" | "thinking" | "talking" | "walking";

export function Face({ mode, size = 180 }: { mode: FaceMode; size?: number }) {
  const look = mode === "listening" ? -6 : mode === "walking" ? -3 : 0;
  return (
    <div className="relative" style={{ width: size, height: size * 0.72 }}>
      {mode === "listening" && (
        <motion.span
          className="absolute inset-0 rounded-[28%] ring-4 ring-magenta"
          initial={{ opacity: 0.7, scale: 1 }}
          animate={{ opacity: 0, scale: 1.12 }}
          transition={{ duration: 1.2, repeat: Infinity }}
          aria-hidden
        />
      )}
      <svg viewBox="0 0 200 144" className="relative h-full w-full drop-shadow-lg" aria-hidden>
        <rect x={4} y={4} width={192} height={136} rx={38} fill="#17141a" stroke="var(--color-magenta)" strokeWidth={5} />
        {/* eyes */}
        {[66, 134].map((cx, i) => (
          <motion.g
            key={cx}
            animate={
              mode === "thinking"
                ? { x: [0, 10, 10, -10, -10, 0], y: [-4, -4, -4, -4, -4, -4] }
                : { x: 0, y: look }
            }
            transition={mode === "thinking" ? { duration: 2.4, repeat: Infinity, ease: "easeInOut" } : { type: "spring", stiffness: 200, damping: 18 }}
          >
            {/* blink by squashing the eye around its own center (scaleY, not the y attribute) */}
            <motion.rect
              x={cx - 15}
              y={44}
              width={30}
              height={36}
              rx={13}
              fill="var(--color-magenta)"
              style={{ transformBox: "fill-box", transformOrigin: "center" }}
              initial={false}
              animate={{ scaleY: mode === "listening" ? 1.12 : [1, 1, 0.08, 1, 1] }}
              transition={mode === "listening" ? { duration: 0.2 } : { duration: 4.2, times: [0, 0.9, 0.93, 0.96, 1], repeat: Infinity, delay: i * 0.03 }}
            />
            <rect x={cx - 7} y={50} width={8} height={8} rx={4} fill="white" opacity={0.85} />
          </motion.g>
        ))}
        {/* mouth */}
        {mode === "talking" ? (
          <motion.ellipse cx={100} cy={108} rx={18} fill="var(--color-magenta)" animate={{ ry: [3, 10, 5, 12, 4, 9, 3] }} transition={{ duration: 1.1, repeat: Infinity }} />
        ) : mode === "thinking" ? (
          [86, 100, 114].map((x, i) => (
            <motion.circle key={x} cx={x} cy={110} r={4} fill="var(--color-magenta)" animate={{ opacity: [0.25, 1, 0.25] }} transition={{ duration: 0.9, repeat: Infinity, delay: i * 0.2 }} />
          ))
        ) : (
          <path d={mode === "listening" ? "M88 106 Q100 112 112 106" : "M80 102 Q100 120 120 102"} stroke="var(--color-magenta)" strokeWidth={6} strokeLinecap="round" fill="none" />
        )}
      </svg>
    </div>
  );
}
