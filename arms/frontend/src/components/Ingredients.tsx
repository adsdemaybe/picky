import type { Product } from "@/lib/data";
import { cn } from "@/lib/utils";

/** Ingredients, dietary tags and allergens, with where the list comes from (a real matched product). */
export function Ingredients({ p, compact = false }: { p: Product; compact?: boolean }) {
  const list = p.ingredients ?? [];
  const src = p.ingredients_source;
  const allergens = p.allergens ?? [];
  if (!list.length && !p.dietary.length && !allergens.length) return null;
  return (
    <div className={cn("border-t border-border", compact ? "pt-3" : "p-5")}>
      <h3 className="mb-2 text-[16px] font-semibold">Ingredients</h3>
      {(p.dietary.length > 0 || allergens.length > 0) && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {p.dietary.map((d) => (
            <span key={d} className="rounded-full bg-leaf/15 px-2.5 py-0.5 text-[12.5px] font-medium text-leaf">
              {d.replace(/_/g, " ")}
            </span>
          ))}
          {allergens.map((a) => (
            <span key={a} className="rounded-full bg-tomato/15 px-2.5 py-0.5 text-[12.5px] font-medium text-tomato">
              contains {a.replace(/_/g, " ")}
            </span>
          ))}
        </div>
      )}
      {list.length > 0 ? (
        <p className="text-[14.5px] leading-relaxed">{list.join(", ")}</p>
      ) : (
        <p className="text-[14px] text-muted-foreground">No ingredient list on file yet.</p>
      )}
      {src && (
        <p className="mt-2 text-[12.5px] text-muted-foreground">
          {src.url ? (
            <>
              From{" "}
              <a href={src.url} target="_blank" rel="noreferrer" className="underline underline-offset-2">
                {src.matched ?? "a matched product"}
              </a>{" "}
              on {src.name} (similar product; our brands are fictional).
            </>
          ) : (
            <>Fresh, single ingredient.</>
          )}
        </p>
      )}
    </div>
  );
}
