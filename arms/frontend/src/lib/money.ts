// Money is integer cents everywhere; format only at render (CLAUDE.md §7).
export function fmt(cents: number, opts: { sign?: boolean } = {}): string {
  const neg = cents < 0;
  const abs = Math.abs(Math.round(cents));
  const s = `$${Math.floor(abs / 100).toLocaleString("en-US")}.${String(abs % 100).padStart(2, "0")}`;
  if (neg) return `−${s}`;
  return opts.sign ? `+${s}` : s;
}

export function fmtShort(cents: number): string {
  return cents % 100 === 0 ? `$${cents / 100}` : fmt(cents);
}
