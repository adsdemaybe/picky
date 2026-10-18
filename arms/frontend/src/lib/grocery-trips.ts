// Grocery demo scenarios. Prices, stock and shelf positions still come from the generated planogram.
import { productBySku, type Product } from "./data";

export type GroceryTripId = "tacos" | "basics" | "breakfast" | "produce" | "pantry";

export const GROCERY_TRIPS = [
  {
    id: "tacos", label: "Taco night", description: "Dinner for six, with a vegetarian option.",
    text: "tacos for 6, under $40, one vegetarian", budget: 4000, color: "var(--color-tomato)",
    matches: /taco|mexican|dinner|party/i,
    items: [["TORT-CORN", 1], ["BEEF-GROUND", 2], ["BEANS-BLACK", 2], ["CHEESE-MEX", 1], ["SALSA-MILD", 1], ["AVOCADO", 3], ["LIMES", 4], ["ONION-WHITE", 1], ["CILANTRO", 1], ["SOUR-CREAM", 1]],
    extras: [
      { sku: "SALSA-CHIPOTLE", reason: "You rated Classic Red Hot Sauce 5 stars. Try a smokier, hotter salsa.", interest: 0.82 },
      { sku: "GUAC-KIT", reason: "A guacamole kit marked down for its approaching best-by date. Use it for taco night.", interest: 0.64 },
      { sku: "CHIPS-TORTILLA", reason: "Tortilla chips are on sale at the endcap and go with your salsa.", interest: 0.51 },
    ],
  },
  {
    id: "basics", label: "Weekly restock", description: "Everyday staples across the whole store.",
    text: "my usual weekly basics, under $35", budget: 3500, color: "var(--color-sky)",
    matches: /weekly|usual|basic|restock/i,
    items: [["OAT-MILK", 1], ["EGGS", 1], ["BANANAS", 1], ["LIMES", 4], ["AVOCADO", 3], ["BEANS-BLACK", 2], ["BREAD-WHEAT", 1], ["COFFEE", 1]],
    extras: [
      { sku: "YOGURT-GREEK", reason: "Greek yogurt is marked down, and you buy it regularly.", interest: 0.58 },
      { sku: "BREAD-SOURDOUGH", reason: "A marked-down sourdough loaf near your wheat bread.", interest: 0.4 },
    ],
  },
  {
    id: "breakfast", label: "Breakfast run", description: "Fruit, oats and a stop at the dairy wall.",
    text: "breakfast for the week, under $25", budget: 2500, color: "var(--color-amber)",
    matches: /breakfast|brunch|oat|cereal|morning/i,
    items: [["BANANAS", 1], ["OATMEAL", 1], ["EGGS", 1], ["OAT-MILK", 1], ["BREAD-WHEAT", 1]],
    extras: [
      { sku: "YOGURT-GREEK", reason: "Marked-down Greek yogurt goes with your bananas and oats.", interest: 0.72 },
      { sku: "GRANOLA", reason: "Maple granola adds crunch to breakfast. Stop only if you want it.", interest: 0.54 },
    ],
  },
  {
    id: "produce", label: "Fresh produce", description: "A short loop around the produce tables.",
    text: "fresh fruit and vegetables, under $20", budget: 2000, color: "var(--color-leaf)",
    matches: /produce|fruit|vegetable|salad|fresh/i,
    items: [["LETTUCE-ROMAINE", 1], ["TOMATO-ROMA", 1], ["PEPPER-RED", 2], ["AVOCADO", 2], ["LIMES", 2], ["BANANAS", 1]],
    extras: [
      { sku: "APPLES", reason: "A bag of apples adds another fruit option from the produce section.", interest: 0.62 },
    ],
  },
  {
    id: "pantry", label: "Pantry refill", description: "Shelf-stable essentials, no chilled stops.",
    text: "refill my pantry with pasta, rice and beans, under $25", budget: 2500, color: "var(--color-grape)",
    matches: /pantry|pasta|rice|bean|cupboard/i,
    items: [["PASTA", 2], ["MARINARA", 1], ["RICE-WHITE", 1], ["BEANS-BLACK", 3], ["PEANUT-BUTTER", 1]],
    extras: [
      { sku: "TACO-SEASONING", reason: "Seasoning turns your rice and beans into another easy meal.", interest: 0.61 },
      { sku: "CHIPS-TORTILLA", reason: "Tortilla chips are discounted at the front endcap.", interest: 0.52 },
    ],
  },
] as const satisfies readonly {
  id: GroceryTripId; label: string; description: string; text: string; budget: number; color: string;
  matches: RegExp; items: readonly (readonly [string, number])[];
  extras: readonly { sku: string; reason: string; interest: number }[];
}[];

export function groceryTrip(text: string) {
  return GROCERY_TRIPS.find((trip) => trip.id !== "basics" && trip.matches.test(text)) ?? GROCERY_TRIPS[1];
}

export const FOOD_DEPARTMENTS = [
  { id: "produce", label: "Fruit & vegetables", categories: ["produce"] },
  { id: "pantry", label: "Pantry", categories: ["pantry"] },
  { id: "bakery", label: "Bakery", categories: ["bakery"] },
  { id: "chilled", label: "Dairy, meat & deli", categories: ["dairy", "meat", "deli"] },
  { id: "snacks", label: "Snacks & drinks", categories: ["snacks", "beverages"] },
  { id: "frozen", label: "Frozen", categories: ["frozen"] },
  { id: "other", label: "Other essentials", categories: [] },
];

export function departmentFor(product: Product) {
  return FOOD_DEPARTMENTS.find((d) => d.categories.includes(product.category)) ?? FOOD_DEPARTMENTS[FOOD_DEPARTMENTS.length - 1];
}

export function groupFoodLines<T extends { sku: string }>(lines: T[]) {
  return FOOD_DEPARTMENTS.map((department) => ({
    ...department,
    lines: lines.filter((line) => departmentFor(productBySku[line.sku]).id === department.id),
  })).filter((department) => department.lines.length > 0);
}
