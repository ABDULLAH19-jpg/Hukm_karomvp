import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Hukm Karo",
  description: "Order by voice. Hukm Karo is the phone ordering assistant for your restaurant.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}