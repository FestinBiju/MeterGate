import type { Metadata } from "next";
import { Inconsolata, Inter, Open_Sans } from "next/font/google";
import type { ReactNode } from "react";

import "./globals.css";

const bodyFont = Open_Sans({ subsets: ["latin"], variable: "--font-body" });
const displayFont = Inter({ subsets: ["latin"], variable: "--font-display" });
const monoFont = Inconsolata({ subsets: ["latin"], variable: "--font-mono" });

export const metadata: Metadata = {
  title: "MeterGate | Agent commerce infrastructure",
  description:
    "A Razorpay-native agent storefront for paid APIs and digital services.",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en">
      <body className={`${bodyFont.variable} ${displayFont.variable} ${monoFont.variable}`}>
        {children}
      </body>
    </html>
  );
}
