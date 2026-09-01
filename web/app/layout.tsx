import type { Metadata, Viewport } from "next";
import "maplibre-gl/dist/maplibre-gl.css";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "Yellow Duck Labs",
    template: "%s — Yellow Duck Labs",
  },
  description:
    "A silent sentry: safeguarding the continuity of communities, cultures, ecosystems, and the ways of life entrusted to our care.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#070a0f",
  viewportFit: "cover",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full">
      <body className="min-h-full font-sans antialiased">{children}</body>
    </html>
  );
}
