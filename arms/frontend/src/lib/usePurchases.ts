"use client";
import { useMemo } from "react";
import { maya, productBySku, tripsFrom, type Purchase } from "./data";
import { useTrip } from "./trip";

/** Fixture + seeded history, plus anything checked out in this session. */
export function usePurchases() {
  const extra = useTrip((s) => s.extraPurchases);
  return useMemo(() => {
    const purchases: Purchase[] = [...maya.purchases, ...extra];
    return { purchases, trips: tripsFrom(purchases) };
  }, [extra]);
}

/** Cents saved vs. list price (deals, markdowns). */
export function savedCents(purchases: Purchase[]) {
  return purchases.reduce((s, p) => {
    const base = productBySku[p.sku]?.price_cents ?? p.unit_price_cents;
    return s + Math.max(0, base - p.unit_price_cents) * p.qty;
  }, 0);
}
