// Preset questions for the robot screen, built from what is ON THE SHELVES of this robot's store right now
// (the gateway planogram: slots with qty > 0) + our product data (ingredients, dietary tags, prices). Every question
// is one the robot can answer for a product the shopper can actually find here.
import { productBySku, type Product } from "@/lib/data";
import type { S } from "@/lib/gateway/client";

export type Preset = { kind: "where" | "ingredients" | "diet" | "cheaper"; text: string };

/** "Mild Chunky Salsa 16 oz" -> "mild chunky salsa" (how a person would say it). */
export function spoken(name: string): string {
  return name
    .replace(/\(.*?\)/g, " ")
    .replace(/\b\d+(\.\d+)?\s*(oz|fl oz|lb|lbs|ct|g|kg|ml|l|gal|pk|pack|count|sticks?)\b/gi, " ")
    .replace(/\b(half gallon|1 gal|glass)\b/gi, " ")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

function seeded(seed: number) {
  let s = seed >>> 0 || 1;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32);
}

/** In-stock products of this store (unique SKUs), with the store's shelf price. */
export function stocked(planogram: S["Planogram"] | null): { p: Product; price: number }[] {
  if (!planogram) return [];
  const seen = new Map<string, { p: Product; price: number }>();
  for (const s of planogram.slots ?? []) {
    const p = productBySku[s.sku];
    if (!p || !s.qty || s.qty <= 0 || seen.has(p.sku)) continue;
    seen.set(p.sku, { p, price: s.price_cents ?? p.price_cents });
  }
  return [...seen.values()];
}

/** A balanced, shuffled set of answerable questions for this store (``round`` picks a different set). */
export function presetsFor(planogram: S["Planogram"] | null, round = 0, n = 8): Preset[] {
  const items = stocked(planogram);
  if (!items.length) return [];
  const rnd = seeded(round * 7919 + items.length);
  const shuffled = [...items].sort(() => rnd() - 0.5);
  const out: Preset[] = [];
  const used = new Set<string>();
  const push = (pr: Preset, sku: string) => {
    if (used.has(sku + pr.kind)) return;
    used.add(sku + pr.kind);
    out.push(pr);
  };
  // cheaper: only when a cheaper item of the same subcategory is in stock HERE
  const bySub = new Map<string, { p: Product; price: number }[]>();
  for (const it of items) {
    if (!it.p.subcategory) continue;
    bySub.set(it.p.subcategory, [...(bySub.get(it.p.subcategory) ?? []), it]);
  }
  const cheaper = shuffled.filter((it) => (bySub.get(it.p.subcategory ?? "") ?? []).some((o) => o.price < it.price));
  const withIngredients = shuffled.filter((it) => (it.p.ingredients ?? []).length > 1);
  const withDiet = shuffled.filter((it) => it.p.dietary.some((d) => ["vegan", "gluten_free", "dairy_free"].includes(d)));
  const quota: [Preset["kind"], { p: Product }[], number][] = [
    ["where", shuffled, 3],
    ["ingredients", withIngredients, 2],
    ["cheaper", cheaper, 2],
    ["diet", withDiet, 1],
  ];
  for (const [kind, pool, k] of quota) {
    for (const { p } of pool.slice(0, k)) {
      const name = spoken(p.name);
      if (kind === "where") push({ kind, text: `Where can I find the ${name}?` }, p.sku);
      if (kind === "ingredients") push({ kind, text: `What's in the ${name}?` }, p.sku);
      if (kind === "cheaper") push({ kind, text: `Is there a cheaper option than the ${name}?` }, p.sku);
      if (kind === "diet") {
        const d = p.dietary.find((x) => ["vegan", "gluten_free", "dairy_free"].includes(x))!;
        push({ kind, text: `Is the ${name} ${d.replace("_", "-")}?` }, p.sku);
      }
    }
  }
  return out.slice(0, n);
}
