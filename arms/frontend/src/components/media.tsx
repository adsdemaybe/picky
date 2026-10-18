"use client";
// Lottie + image slots with built-in fallbacks (see lib/media.ts for the slot list and what to drop in).
import { DotLottieReact } from "@lottiefiles/dotlottie-react";
import { ImageIcon } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { MEDIA, type MediaKey } from "@/lib/media";
import { cn } from "@/lib/utils";

const exists = new Map<string, Promise<boolean>>();
function checkAsset(path: string) {
  if (!exists.has(path))
    exists.set(
      path,
      fetch(path, { method: "HEAD" })
        .then((r) => r.ok)
        .catch(() => false),
    );
  return exists.get(path)!;
}

function useAsset(path: string) {
  const [ok, setOk] = useState<boolean | null>(null);
  useEffect(() => {
    let alive = true;
    checkAsset(path).then((v) => alive && setOk(v));
    return () => {
      alive = false;
    };
  }, [path]);
  return ok;
}

const DEV = process.env.NODE_ENV === "development";

function SlotHint({ slot }: { slot: MediaKey }) {
  if (!DEV) return null;
  return (
    <span className="pointer-events-none absolute right-2 bottom-2 z-10 flex items-center gap-1 rounded-full bg-black/60 px-2 py-0.5 text-[11px] text-white">
      <ImageIcon className="size-3" aria-hidden /> {MEDIA[slot].path}
    </span>
  );
}

/** Plays a Lottie from /public if it exists; otherwise renders the fallback (our own animated SVG). */
export function LottieSlot({
  slot,
  fallback,
  className,
  loop = true,
  hint = true,
}: {
  slot: MediaKey;
  fallback: ReactNode;
  className?: string;
  loop?: boolean;
  /** show the dev-only "drop a file here" hint (turn off for tiny slots) */
  hint?: boolean;
}) {
  const ok = useAsset(MEDIA[slot].path);
  return (
    <div className={cn("relative", className)}>
      {ok ? <DotLottieReact src={MEDIA[slot].path} autoplay loop={loop} className="h-full w-full" /> : fallback}
      {ok === false && hint && <SlotHint slot={slot} />}
    </div>
  );
}

/** Shows an image from /public if it exists; otherwise the fallback art. */
export function ImageSlot({
  slot,
  alt,
  fallback,
  className,
  hint = true,
}: {
  slot: MediaKey;
  alt: string;
  fallback: ReactNode;
  className?: string;
  hint?: boolean;
}) {
  const ok = useAsset(MEDIA[slot].path);
  return (
    <div className={cn("relative overflow-hidden", className)}>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      {ok ? <img src={MEDIA[slot].path} alt={alt} className="h-full w-full object-cover" /> : fallback}
      {ok === false && hint && <SlotHint slot={slot} />}
    </div>
  );
}
