import type { Metadata, Viewport } from "next";
import { Bricolage_Grotesque, Plus_Jakarta_Sans } from "next/font/google";
import { AskSheet } from "@/components/AskPanel";
import { MotionProvider } from "@/components/motion";
import { BottomTabs, Main, TopBar } from "@/components/Shell";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import "./globals.css";

const display = Bricolage_Grotesque({ variable: "--font-bricolage", subsets: ["latin"], weight: ["500", "600", "700", "800"] });
const sans = Plus_Jakarta_Sans({ variable: "--font-jakarta", subsets: ["latin"], weight: ["400", "500", "600", "700"] });

export const metadata: Metadata = {
  title: "Picky",
  description: "Find the best price, keep your spending in check, and send a robot to shop for you.",
};

export const viewport: Viewport = { themeColor: "#f3f5f2" };

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${display.variable} ${sans.variable} h-full antialiased`}>
      <body className="min-h-full">
        <MotionProvider>
          <TooltipProvider delayDuration={200}>
            <TopBar />
            <Main>{children}</Main>
            <BottomTabs />
            <AskSheet />
            <Toaster />
          </TooltipProvider>
        </MotionProvider>
      </body>
    </html>
  );
}
