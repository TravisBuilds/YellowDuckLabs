import type { Metadata } from "next";
import "maplibre-gl/dist/maplibre-gl.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Fire Watch — Yellow Duck Labs",
  description:
    "Municipal wildfire operating picture: what we preserve, what threatens it, what already defends it, and where the gaps are.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="h-full overflow-hidden font-sans antialiased">{children}</body>
    </html>
  );
}
