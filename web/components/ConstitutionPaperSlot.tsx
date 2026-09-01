"use client";

import dynamic from "next/dynamic";

const ConstitutionPaper = dynamic(() => import("./ConstitutionPaper"), {
  ssr: false,
  loading: () => (
    <p className="py-24 text-center text-[12px] text-zinc-600">
      Setting the paper…
    </p>
  ),
});

export default function ConstitutionPaperSlot() {
  return <ConstitutionPaper />;
}
