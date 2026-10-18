import type { Metadata } from "next";
import { TmoHeader } from "./bits";

export const metadata: Metadata = {
  title: "Picky × T-Mobile · Cart Whisperer",
  description: "Phone shopping with checkout suggestions that actually help, including the ones that say skip or wait.",
};

// The T-Mobile store is its own site: own header and nav, no grocery tabs (Shell.tsx hides them under /tmobile).
export default function TmobileLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="-mx-4 -mt-6 md:-mx-8 md:-mt-8">
      <TmoHeader />
      <div className="mx-auto max-w-[1240px] px-4 pt-6 md:px-8 md:pt-8">{children}</div>
    </div>
  );
}
