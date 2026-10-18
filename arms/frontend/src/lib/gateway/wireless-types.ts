// /api/wireless/* shapes = contracts/wireless.py, generated into api-types.ts by `make gen-types` (never hand-write
// them). Responses are the generated schemas as-is; request bodies keep their required keys and make the fields with
// server-side defaults optional. Money is integer cents; every value is synthetic demo pricing.
import type { S } from "./client";

type Req<T, K extends keyof T> = Pick<T, K> & Partial<Omit<T, K>>;

export type PlanQuote = S["PlanQuote"];
export type FinancingQuote = S["FinancingQuote"];
export type TradeInCondition = S["TradeInCondition"];
export type TradeInQuote = S["TradeInQuote"];
export type CompatibleAccessory = S["CompatibleAccessory"];
export type Promotion = S["Promotion"];
export type PromoEligibility = S["PromoEligibility"];
export type HouseholdOption = S["HouseholdOption"];
export type HouseholdOptimization = S["HouseholdOptimization"];
export type WirelessAccount = S["WirelessAccount"];
export type QuoteLine = S["QuoteLine"];
export type CostBreakdown = S["CostBreakdown"];
export type WirelessCatalogInfo = S["WirelessCatalogInfo"];
export type ServicePlan = S["ServicePlan"];

export type QuoteItem = Req<S["QuoteItem-Input"], "sku">;
export type QuoteRequest = Omit<Partial<S["QuoteRequest-Input"]>, "items"> & { items?: QuoteItem[] };
export type TradeInRequest = Req<S["TradeInRequest"], "model_id" | "condition">;
export type FinancingRequest = Req<S["FinancingRequest"], "sku">;
export type HouseholdRequest = Partial<S["HouseholdRequest"]>;
