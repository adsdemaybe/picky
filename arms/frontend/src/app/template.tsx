"use client";
// Re-mounts on every navigation, so each page gets the same short enter transition.
import { motion } from "motion/react";

export default function Template({ children }: { children: React.ReactNode }) {
  return (
    <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ type: "spring", stiffness: 260, damping: 30 }}>
      {children}
    </motion.div>
  );
}
