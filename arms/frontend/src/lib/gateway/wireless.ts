"use client";
// Wireless retail client: plans, trade-in, financing, promos, household optimizer and cart quotes for store_tmobile
// (gateway /api/wireless/*, contracts/wireless.py). Every number is computed by the backend's wireless arm; the web
// app only displays it. Errors: 404 unknown id, 409 invalid combination, 422 validation (GatewayError.status).
import { req, qs } from "./client";
import type {
  CompatibleAccessory,
  CostBreakdown,
  FinancingQuote,
  FinancingRequest,
  HouseholdOptimization,
  HouseholdRequest,
  PlanQuote,
  PromoEligibility,
  QuoteRequest,
  TradeInQuote,
  TradeInRequest,
  WirelessAccount,
  WirelessCatalogInfo,
} from "./wireless-types";

const enc = encodeURIComponent;
const post = <T>(path: string, body: unknown) => req<T>(path, { method: "POST", body }).then((r) => r.data);
const get = <T>(path: string) => req<T>(path).then((r) => r.data);

export const wirelessApi = {
  info: () => get<WirelessCatalogInfo>("/api/wireless"),
  plans: (q: { lines: number; autopay?: boolean; user_id?: string }) => get<PlanQuote[]>(`/api/wireless/plans${qs({ lines: q.lines, autopay: q.autopay === undefined ? undefined : String(q.autopay), user_id: q.user_id })}`),
  planQuote: (planId: string, q: { lines: number; autopay?: boolean; user_id?: string }) =>
    get<PlanQuote>(`/api/wireless/plans/${enc(planId)}/quote${qs({ lines: q.lines, autopay: q.autopay === undefined ? undefined : String(q.autopay), user_id: q.user_id })}`),
  compatible: (deviceSku: string, storeId = "store_tmobile") => get<CompatibleAccessory[]>(`/api/wireless/compatible/${enc(deviceSku)}${qs({ store_id: storeId })}`),
  tradeIn: (b: TradeInRequest) => post<TradeInQuote>("/api/wireless/trade-in", b),
  financing: (b: FinancingRequest) => post<FinancingQuote>("/api/wireless/financing", b),
  promotions: (b: QuoteRequest) => post<PromoEligibility[]>("/api/wireless/promotions", b),
  accounts: () => get<WirelessAccount[]>("/api/wireless/accounts"),
  account: (userId: string) => get<WirelessAccount>(`/api/wireless/accounts/${enc(userId)}`),
  household: (b: HouseholdRequest) => post<HouseholdOptimization>("/api/wireless/household", b),
  quote: (b: QuoteRequest) => post<CostBreakdown>("/api/wireless/quote", b),
};
