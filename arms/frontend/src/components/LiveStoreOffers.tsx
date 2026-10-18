"use client";
// Find & compare, gateway mode: what the Picky stores actually have right now for the query (GET /api/products/search:
// live shelf stock, price and deals per store from the catalog arm). The 13-week history / retailer offers above stay
// synthetic demo data (no gateway endpoint for them yet).
import { Store } from "lucide-react";
import { useEffect, useState } from "react";
import { Money, Panel, SectionTitle } from "@/components/picky";
import { Badge } from "@/components/ui/badge";
import { errorText, gw, type S } from "@/lib/gateway/client";
import { useGateway } from "@/lib/gateway/useGateway";

const STORE_NAME: Record<string, string> = { store_ave: "The Ave", store_hill: "Capitol Hill", store_tmobile: "T-Mobile", online: "Online" };

export function LiveStoreOffers({ query }: { query: string }) {
  const live = useGateway((s) => s.status === "live");
  const [res, setRes] = useState<{ q: string; hits: S["SearchHit"][]; err: string | null } | null>(null);
  useEffect(() => {
    if (!live || !query) return;
    let on = true;
    gw.search(query, 6)
      .then(({ data }) => on && setRes({ q: query, hits: data, err: null }))
      .catch((e) => on && setRes({ q: query, hits: [], err: errorText(e) }));
    return () => {
      on = false;
    };
  }, [live, query]);
  if (!live || !query || !res || res.q !== query) return null;
  return (
    <section data-testid="live-store-offers">
      <SectionTitle>
        <span className="inline-flex items-center gap-2">
          <Store className="size-5" /> On the shelf at Picky stores now
        </span>
      </SectionTitle>
      <Panel className="divide-y divide-border text-[14px]">
        {res.err && <p className="p-4 text-tomato">{res.err}</p>}
        {!res.err && res.hits.length === 0 && <p className="p-4 text-muted-foreground">No Picky store carries “{query}”.</p>}
        {res.hits.map((h) => (
          <div key={h.product.sku} className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
            <div className="min-w-[12rem] flex-1">
              <div className="font-medium">{h.product.name}</div>
              <div className="text-[12.5px] text-muted-foreground">{h.product.brand}</div>
            </div>
            <div className="flex flex-wrap gap-2">
              {h.offers.map((o) => (
                <span key={o.store_id} className="inline-flex items-center gap-1.5 rounded-full bg-muted px-3 py-1 text-[13px]">
                  <span className="font-medium">{STORE_NAME[o.store_id] ?? o.store_id}</span>
                  <Money cents={o.deal?.price_cents ?? o.price_cents} />
                  {o.deal && <Badge variant="lime">deal</Badge>}
                  <span className={o.qty > 0 ? "text-muted-foreground" : "text-tomato"}>{o.qty > 0 ? `${o.qty} in stock` : "out"}</span>
                </span>
              ))}
            </div>
          </div>
        ))}
      </Panel>
    </section>
  );
}
