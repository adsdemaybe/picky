import type { Metadata } from "next";
import { Bricolage_Grotesque, IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import { AskPanel } from "@/components/AskPanel";
import { MobileBar, Rail } from "@/components/Rail";
import "./globals.css";

const display = Bricolage_Grotesque({ variable: "--font-bricolage", subsets: ["latin"], weight: ["500", "600", "700"] });
const sans = IBM_Plex_Sans({ variable: "--font-plex-sans", subsets: ["latin"], weight: ["400", "500", "600"] });
const mono = IBM_Plex_Mono({ variable: "--font-plex-mono", subsets: ["latin"], weight: ["400", "500"] });

export const metadata: Metadata = {
  title: "Picky · shopping assistant",
  description: "Find the best price, track your spending, and send a robot to shop for you.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${display.variable} ${sans.variable} ${mono.variable} h-full antialiased`}>
      <body className="min-h-full">
        <div className="flex min-h-dvh">
          <Rail />
          <div className="min-w-0 flex-1">
            <MobileBar />
            <main className="mx-auto max-w-[1360px] px-4 py-7 md:px-9">{children}</main>
          </div>
        </div>
        <AskPanel />
      </body>
    </html>
  );
}
