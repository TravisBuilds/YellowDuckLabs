import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Fire Watch",
  description:
    "Municipal wildfire operating picture: what we preserve, what threatens it, what already defends it, and where the gaps are.",
};

export default function FireWatchLayout({ children }: { children: React.ReactNode }) {
  return children;
}
