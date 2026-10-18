"use client";
// Store staff view (gateway only): stock levels, low/out alerts with one-click restock, +/- adjustments, movement
// history, the merchant dashboard (out-of-stocks, mispricings, near-expiry) and the store's ceiling cameras.
// Endpoints: GET /api/stores, GET /api/stores/{id}/stock(/alerts|/history), POST .../stock/restock|adjust,
// GET /api/merchant/{id}/dashboard, GET /api/cameras?store_id= (+ snapshot.jpg, faces blurred by the gateway).
import { Minus, PackagePlus, Plus, RefreshCw } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { ago, GatewayNeeded, StorePicker, useLiveGateway, usePoll, useStores } from "@/components/LiveOnly";
import { Money, Panel, SectionTitle } from "@/components/picky";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cameraUrl, errorText, gw } from "@/lib/gateway/client";
import { cn } from "@/lib/utils";

const ACTOR = "staff:web";

export default function StaffPage() {
  const status = useLiveGateway();
  const live = status === "live";
  const { stores, err: storesErr } = useStores(live);
  const [picked, setPicked] = useState<string | null>(null);
  const storeId = picked ?? stores[0]?.store_id ?? "";
  const on = live && !!storeId;

  const stock = usePoll(on ? () => gw.stock(storeId).then((r) => r.data) : null, 5000, [storeId]);
  const alerts = usePoll(on ? () => gw.stockAlerts(storeId).then((r) => r.data) : null, 5000, [storeId]);
  const history = usePoll(on ? () => gw.stockHistory(storeId, 25).then((r) => r.data) : null, 5000, [storeId]);
  const dash = usePoll(on ? () => gw.merchant(storeId).then((r) => r.data) : null, 10_000, [storeId]);
  const cams = usePoll(on ? () => gw.cameras({ store_id: storeId }).then((r) => r.data) : null, 30_000, [storeId]);
  const [busy, setBusy] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "low" | "out">("all");

  const refreshAll = () => {
    stock.refresh();
    alerts.refresh();
    history.refresh();
    dash.refresh();
  };

  async function restock(sku: string, name: string, qty?: number | null) {
    setBusy(sku);
    try {
      const { data } = await gw.restock(storeId, { sku, qty: qty ?? null, actor: ACTOR, note: "restock from the staff view" });
      toast.success(`Restocked ${name}: now ${data.level.qty} on the shelf`);
      refreshAll();
    } catch (e) {
      toast.error(`Restock failed: ${errorText(e)}`);
    } finally {
      setBusy(null);
    }
  }

  async function adjust(sku: string, name: string, delta: number) {
    setBusy(sku);
    try {
      const { data } = await gw.adjust(storeId, { sku, delta, reason: "manual", actor: ACTOR, note: "manual count from the staff view" });
      toast.success(`${name}: ${data.level.qty} on the shelf`);
      refreshAll();
    } catch (e) {
      toast.error(`Adjustment failed: ${errorText(e)}`);
    } finally {
      setBusy(null);
    }
  }

  if (!live) {
    return (
      <div className="flex flex-col gap-6">
        <Header />
        <GatewayNeeded status={status} what="stock levels and alerts" />
      </div>
    );
  }

  const rows = (stock.data ?? []).filter((r) => filter === "all" || r.status === filter);
  const ceiling = (cams.data ?? []).filter((c) => c.kind === "ceiling" || c.camera_id.includes("ceiling") || c.camera_id.startsWith(storeId));

  return (
    <div className="flex flex-col gap-8">
      <Header />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <StorePicker stores={stores} value={storeId} onChange={setPicked} />
        <Button variant="outline" size="sm" onClick={refreshAll}>
          <RefreshCw /> Refresh
        </Button>
      </div>
      {storesErr && <p className="text-[14px] text-tomato">Stores: {storesErr}</p>}

      <div className="grid gap-4 sm:grid-cols-4">
        <Kpi label="Out of stock" value={(stock.data ?? []).filter((r) => r.status === "out").length} tone="text-tomato" />
        <Kpi label="Running low" value={(stock.data ?? []).filter((r) => r.status === "low").length} tone="text-amber" />
        <Kpi label="Mispricings" value={dash.data?.mispricings.length ?? 0} />
        <Kpi label="Shelf scans" value={dash.data?.scans_total ?? 0} />
      </div>

      <div className="grid gap-8 lg:grid-cols-12">
        <section className="lg:col-span-5">
          <SectionTitle>Alerts</SectionTitle>
          <Panel className="divide-y divide-border">
            {alerts.err && <p className="p-4 text-[14px] text-tomato">{alerts.err}</p>}
            {alerts.data && alerts.data.length === 0 && <p className="p-4 text-[14px] text-muted-foreground">No low or out-of-stock items.</p>}
            {(alerts.data ?? []).map((a) => (
              <div key={a.alert_id} className="flex items-center gap-3 p-4" data-testid="stock-alert">
                <Badge variant={a.status === "out" ? "destructive" : "secondary"}>{a.status === "out" ? "Out" : "Low"}</Badge>
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium">{a.name}</div>
                  <div className="text-[12.5px] text-muted-foreground">
                    {a.aisle_label ?? a.slot_ids.join(", ")} · {a.qty}/{a.capacity} · {ago(a.raised_at)}
                  </div>
                </div>
                <Button size="sm" disabled={busy === a.sku} onClick={() => restock(a.sku, a.name, a.suggested_restock_qty)}>
                  <PackagePlus /> Restock{a.suggested_restock_qty ? ` ${a.suggested_restock_qty}` : ""}
                </Button>
              </div>
            ))}
          </Panel>

          <SectionTitle className="mt-8">Merchant dashboard</SectionTitle>
          <Panel className="divide-y divide-border text-[14px]">
            {dash.err && <p className="p-4 text-tomato">{dash.err}</p>}
            {(dash.data?.mispricings ?? []).map((m) => (
              <div key={`mp-${m.sku}`} className="flex items-center justify-between gap-3 p-4">
                <div>
                  <div className="font-medium">{m.name}</div>
                  <div className="text-[12.5px] text-muted-foreground">Shelf tag differs from the system price · {m.slot_id}</div>
                </div>
                <div className="text-right tabular">
                  <div>
                    tag <Money cents={m.shelf_tag_cents} />
                  </div>
                  <div className="text-muted-foreground">
                    system <Money cents={m.system_price_cents} />
                  </div>
                </div>
              </div>
            ))}
            {(dash.data?.near_expiry ?? []).slice(0, 5).map((n, i) => (
              <div key={`ne-${n.sku}-${i}`} className="flex items-center justify-between gap-3 p-4">
                <div className="font-medium">{productName(stock.data, n.sku)}</div>
                <Badge variant="secondary">Near expiry</Badge>
              </div>
            ))}
            {dash.data && !dash.data.mispricings.length && !dash.data.near_expiry.length && <p className="p-4 text-muted-foreground">No mispricings or near-expiry lots.</p>}
          </Panel>
        </section>

        <section className="lg:col-span-7">
          <SectionTitle
            action={
              <div className="flex gap-1">
                {(["all", "low", "out"] as const).map((f) => (
                  <button
                    key={f}
                    onClick={() => setFilter(f)}
                    className={cn("cursor-pointer rounded-full px-3 py-1 text-[13px]", filter === f ? "bg-foreground text-background" : "hover:bg-muted")}
                  >
                    {f === "all" ? "All" : f === "low" ? "Low" : "Out"}
                  </button>
                ))}
              </div>
            }
          >
            Stock levels
          </SectionTitle>
          <Panel className="max-h-[560px] overflow-y-auto">
            {stock.err && <p className="p-4 text-[14px] text-tomato">{stock.err}</p>}
            <table className="w-full text-[14px]">
              <thead className="sticky top-0 bg-card text-left text-[12.5px] text-muted-foreground">
                <tr>
                  <th className="p-3 font-medium">Item</th>
                  <th className="p-3 font-medium">Shelf</th>
                  <th className="p-3 text-right font-medium">Price</th>
                  <th className="p-3 text-right font-medium">Count</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {rows.map((r) => (
                  <tr key={r.sku} data-testid="stock-row">
                    <td className="p-3">
                      <div className="font-medium">{r.name}</div>
                      <div className="text-[12px] text-muted-foreground">{r.aisle_label ?? r.category}</div>
                    </td>
                    <td className="p-3">
                      <div className="h-2 w-24 overflow-hidden rounded-full bg-muted">
                        <div
                          className={cn("h-full", r.status === "out" ? "bg-tomato" : r.status === "low" ? "bg-amber" : "bg-lime")}
                          style={{ width: `${Math.round(Math.min(1, r.fill_ratio) * 100)}%` }}
                        />
                      </div>
                    </td>
                    <td className="p-3 text-right">
                      <Money cents={r.price_cents ?? 0} />
                    </td>
                    <td className="p-3">
                      <div className="flex items-center justify-end gap-1">
                        <Button size="icon" variant="ghost" className="size-7" aria-label={`One less ${r.name}`} disabled={busy === r.sku || r.qty <= 0} onClick={() => adjust(r.sku, r.name, -1)}>
                          <Minus />
                        </Button>
                        <span className="w-12 text-center tabular">
                          {r.qty}/{r.capacity}
                        </span>
                        <Button size="icon" variant="ghost" className="size-7" aria-label={`One more ${r.name}`} disabled={busy === r.sku} onClick={() => adjust(r.sku, r.name, 1)}>
                          <Plus />
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>

          <SectionTitle className="mt-8">Movement history</SectionTitle>
          <Panel className="divide-y divide-border text-[14px]">
            {history.data && history.data.length === 0 && <p className="p-4 text-muted-foreground">No stock movements yet.</p>}
            {(history.data ?? []).map((m) => (
              <div key={m.movement_id} className="flex items-center gap-3 px-4 py-2.5" data-testid="stock-movement">
                <span className={cn("w-10 text-right font-semibold tabular", m.delta < 0 ? "text-tomato" : "text-lime-700")}>
                  {m.delta > 0 ? `+${m.delta}` : m.delta}
                </span>
                <span className="min-w-0 flex-1 truncate">
                  {productName(stock.data, m.sku)} <span className="text-muted-foreground">· {m.reason} by {m.actor}</span>
                </span>
                <span className="text-[12.5px] text-muted-foreground tabular">
                  {m.qty_before}→{m.qty_after} · {ago(m.ts)}
                </span>
              </div>
            ))}
          </Panel>
        </section>
      </div>

      {ceiling.length > 0 && (
        <section>
          <SectionTitle>Store cameras (faces blurred)</SectionTitle>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {ceiling.slice(0, 6).map((c) => (
              <Panel key={c.camera_id} className="overflow-hidden">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={cameraUrl(c.camera_id, "snapshot", { w: 480 })} alt={c.name ?? c.camera_id} className="aspect-video w-full bg-muted object-cover" />
                <div className="flex items-center justify-between p-3 text-[13px]">
                  <span className="font-medium">{c.name ?? c.camera_id}</span>
                  <span className="text-muted-foreground">{c.source ?? ""}</span>
                </div>
              </Panel>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function productName(rows: { sku: string; name: string }[] | null, sku: string) {
  return rows?.find((r) => r.sku === sku)?.name ?? sku;
}

function Header() {
  return (
    <section className="animate-rise">
      <h1 className="text-[36px] leading-[1.05] font-bold md:text-[46px]">Store staff</h1>
      <p className="mt-2 max-w-[62ch] text-[16px] text-muted-foreground">
        Live stock from the robots&apos; shelf scans and picks. Restocks and counts saved here re-sync the simulated shelves.
      </p>
    </section>
  );
}

function Kpi({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <Panel className="p-4">
      <div className="text-[13px] text-muted-foreground">{label}</div>
      <div className={cn("mt-1 text-[28px] font-bold tabular", tone)}>{value}</div>
    </Panel>
  );
}
