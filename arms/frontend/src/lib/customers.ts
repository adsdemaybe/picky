// Who can check in with Magenta, and where each person's data lives (all synthetic, from data/wireless.yaml and
// data/fixtures/tmobile_directions.yaml). One agent, five skills: Cart Whisperer (basket), Matchmaker (preset answers),
// Next Best Moment (retention signals -> what Magenta raises first), Timekeeper (phones), Household Mix (lines).
import { accountOf, DATA, household, match, moment, upgrade, type Household, type Preset } from "./directions";

export type Customer = {
  id: string;
  name: string;
  first: string;
  blurb: string;
  account_id?: string;
  household_id?: string;
  upgrade_ids: string[];
  retention_id?: string;
  preset_id?: string;
  /** not a T-Mobile customer yet (other carrier / new) */
  prospect?: boolean;
};

export const CUSTOMERS: Customer[] = [
  { id: "maya", name: "Maya", first: "Maya", blurb: "Pixel 7 · 2 lines · Value Unlimited", account_id: "acct_maya", upgrade_ids: ["maya"], retention_id: "acct_maya" },
  { id: "garcia", name: "Elena Garcia", first: "Elena", blurb: "Family of 4 · Premium Unlimited", account_id: "acct_garcia", household_id: "garcia", upgrade_ids: ["teo", "marco"], retention_id: "acct_garcia" },
  { id: "jordan", name: "Jordan", first: "Jordan", blurb: "iPhone 13, 4 years old · photographer", account_id: "acct_jordan", upgrade_ids: ["jordan"], retention_id: "acct_jordan", preset_id: "jordan" },
  { id: "dev", name: "Dev", first: "Dev", blurb: "140 GB a month · Value Unlimited", account_id: "acct_dev", household_id: "dev", upgrade_ids: [], retention_id: "acct_dev", preset_id: "dev" },
  { id: "ruth", name: "Ruth", first: "Ruth", blurb: "68 · iPhone 15 · light use", account_id: "acct_ruth", household_id: "ruth", upgrade_ids: ["ruth"], retention_id: "acct_ruth", preset_id: "ruth" },
  { id: "priya", name: "Priya", first: "Priya", blurb: "Student · other carrier · $60 budget", account_id: "acct_priya", upgrade_ids: [], preset_id: "priya", prospect: true },
  { id: "alex", name: "Alex", first: "Alex", blurb: "Switching 2 lines from another carrier", account_id: "acct_alex", household_id: "alex", upgrade_ids: [], preset_id: "alex", prospect: true },
];

export const customerOf = (id: string | null | undefined) => CUSTOMERS.find((c) => c.id === id) ?? null;

/** The household to optimize: a listed one, else every line on their account. */
export function householdFor(c: Customer): Household | null {
  if (c.household_id) return DATA.households.find((h) => h.id === c.household_id) ?? null;
  if (c.account_id && accountOf(c.account_id)?.plan_id) return { id: c.id, name: c.name, account_ids: [c.account_id], streaming: [], note: "" };
  return null;
}
export const householdResult = (c: Customer) => {
  const h = householdFor(c);
  return h ? household(h) : null;
};
export const upgradesFor = (c: Customer) => c.upgrade_ids.map((id) => upgrade(DATA.upgrade_cases.find((u) => u.id === id)!));
export const momentFor = (c: Customer) => {
  const r = DATA.retention_accounts.find((x) => x.account_id === c.retention_id);
  return r ? moment(r) : null;
};
export const presetFor = (c: Customer): Preset | null => DATA.matchmaker_presets.find((p) => p.id === c.preset_id) ?? null;
export const matchFor = (c: Customer) => {
  const p = presetFor(c);
  return p ? match(p) : null;
};
