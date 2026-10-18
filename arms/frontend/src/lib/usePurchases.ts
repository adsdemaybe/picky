"use client";
import { useEffect, useMemo, useState } from "react";
import { maya, productBySku, registerProduct, tripsFrom, type Purchase } from "./data";
import { placeholderProduct } from "./gateway/adapt";
import { gatewayHref, gw, type PurchaseRow } from "./gateway/client";
import { useGateway } from "./gateway/useGateway";
import { useTrip } from "./trip";

/** Fixture + seeded history, plus anything checked out in this session. */
export function usePurchases() {
  const extra = useTrip((s) => s.extraPurchases);
  return useMemo(() => {
    const purchases: Purchase[] = [...maya.purchases, ...extra];
    return { purchases, trips: tripsFrom(purchases) };
  }, [extra]);
}

/** NEXT_PUBLIC_PURCHASES_MOCK=1: always the demo history, even with a live gateway. */
const FORCE_MOCK = process.env.NEXT_PUBLIC_PURCHASES_MOCK === "1";

/** GET /api/users/{id}/purchases row → the app's Purchase. Checkouts through Picky keep their gateway trip id (same
 *  shape as purchasesFromReceipt, so a session receipt and its stored rows are one trip); seed rows are demo history. */
function fromRow(r: PurchaseRow): Purchase {
  const checkout = r.source !== "seed" && !!r.trip_id;
  if (!productBySku[r.sku]) registerProduct(placeholderProduct({ sku: r.sku, name: r.name, category: r.category, price_cents: r.unit_price_cents }));
  return {
    purchase_id: checkout ? `pur_gw_${r.purchase_id}` : r.purchase_id,
    user_id: r.user_id ?? maya.profile.user_id,
    sku: r.sku,
    qty: r.qty,
    unit_price_cents: r.unit_price_cents,
    store_id: r.store_id,
    trip_id: checkout ? `trip_maya_1017_${r.trip_id}` : (r.trip_id ?? `trip_${r.ts.slice(0, 10)}_${r.store_id}`),
    ts: r.ts,
    rating: r.rating ?? null,
    synthetic: r.source === "seed",
    photo_urls: (r.photo_urls ?? []).filter(Boolean).map((u) => gatewayHref(u)),
  };
}

export type PurchaseHistory = ReturnType<typeof usePurchases> & { source: "gateway" | "demo"; loading: boolean };

/** Purchase history for Spending: the gateway's purchase-history database (GET /api/users/maya/purchases) when it
 *  answers with rows, plus this session's receipts it doesn't have yet; otherwise the demo history above. */
export function usePurchaseHistory(): PurchaseHistory {
  const local = usePurchases();
  const extra = useTrip((s) => s.extraPurchases);
  const receipt = useTrip((s) => s.receipt?.receipt_id ?? null);
  const status = useGateway((s) => s.status);
  const live = status === "live" && !FORCE_MOCK;
  const [rows, setRows] = useState<PurchaseRow[] | "failed" | null>(null);

  useEffect(() => {
    useGateway.getState().startPolling();
  }, []);

  useEffect(() => {
    if (!live) return;
    let alive = true;
    gw.purchases(maya.profile.user_id, 500)
      .then(({ data }) => {
        const list = Array.isArray(data) ? data : (data.purchases ?? data.items ?? []);
        if (alive) setRows(list.length ? list : "failed");
      })
      // endpoint not deployed yet (404) or the gateway hiccuped: the demo history keeps the page working
      .catch(() => alive && setRows("failed"));
    return () => {
      alive = false;
    };
  }, [live, receipt]);

  return useMemo(() => {
    if (!live || rows === "failed") return { ...local, source: "demo" as const, loading: false };
    if (rows === null) return { ...local, source: "demo" as const, loading: true };
    const stored = rows.map(fromRow);
    const have = new Set(stored.map((p) => p.trip_id));
    const purchases = [...stored, ...extra.filter((p) => !have.has(p.trip_id))];
    return { purchases, trips: tripsFrom(purchases), source: "gateway" as const, loading: false };
  }, [live, rows, local, extra]);
}

/** Cents saved vs. list price (deals, markdowns). */
export function savedCents(purchases: Purchase[]) {
  return purchases.reduce((s, p) => {
    const base = productBySku[p.sku]?.price_cents ?? p.unit_price_cents;
    return s + Math.max(0, base - p.unit_price_cents) * p.qty;
  }, 0);
}
