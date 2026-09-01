import type { Metadata } from "next";
import Link from "next/link";

import ConstitutionPaperSlot from "@/components/ConstitutionPaperSlot";

const CONSTITUTION_HREF = "/constitution.pdf";
const CONSTITUTION_FILENAME =
  "Yellow_Duck_Labs_Founding_Constitution_v0.3.pdf";

export const metadata: Metadata = {
  title: "Yellow Duck Labs",
  description:
    "We protect the world so the next generation can still grow up with yellow ducks. Founding Constitution v0.3.",
};

const PROJECTS = [
  {
    n: "01",
    name: "Fire Watch",
    field: "Wildfire",
    href: "/firewatch",
  },
  {
    n: "02",
    name: "Coming soon",
    field: "Spill response",
    href: null,
  },
  {
    n: "03",
    name: "Coming soon",
    field: "Maritime security",
    href: null,
  },
  {
    n: "04",
    name: "Coming soon",
    field: "Border protection",
    href: null,
  },
];

export default function HomePage() {
  return (
    <main className="min-h-dvh bg-ink text-zinc-200 lg:flex lg:h-dvh lg:flex-row lg:overflow-hidden">
      <aside className="flex flex-col justify-between border-b border-white/[0.06] px-5 pb-8 pt-[max(1.5rem,env(safe-area-inset-top))] sm:px-6 lg:h-dvh lg:w-[22rem] lg:shrink-0 lg:border-b-0 lg:border-r lg:px-8 lg:py-10">
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.28em] text-duck">
            Yellow Duck Labs
          </p>
          <h1 className="mt-6 text-[26px] font-medium leading-snug tracking-tight text-zinc-100 sm:text-[28px] lg:mt-8 lg:text-[22px]">
            We protect the world so the next generation can still grow up with
            yellow ducks.
          </h1>
          <p className="mt-4 text-[14px] leading-relaxed text-zinc-500 lg:mt-5 lg:text-[12px]">
            Founding Constitution · Draft v0.3 · August 2026
          </p>
          <p className="mt-5 text-[15px] leading-relaxed text-zinc-400 lg:mt-6 lg:text-[13px] lg:text-zinc-500">
            The Constitution is the only public document for now. Domain
            playbooks sit beneath it. Fire Watch is the first.
          </p>
        </div>

        <div className="mt-10 lg:mt-0">
          <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-zinc-600">
            Projects
          </p>
          <ul className="mt-3 divide-y divide-white/[0.05]">
            {PROJECTS.map((project) => {
              const inner = (
                <>
                  <span className="font-mono text-[11px] text-zinc-600">
                    {project.n}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-[16px] text-zinc-200 lg:text-[13px]">
                      {project.name}
                    </span>
                    <span className="block text-[13px] text-zinc-500 lg:text-[11px] lg:text-zinc-600">
                      {project.field}
                    </span>
                  </span>
                  {project.href ? (
                    <span className="text-[11px] uppercase tracking-[0.14em] text-duck">
                      Live
                    </span>
                  ) : null}
                </>
              );

              return (
                <li key={project.n}>
                  {project.href ? (
                    <Link
                      href={project.href}
                      className="flex items-center gap-3 py-3.5 transition-colors hover:bg-white/[0.02] lg:py-3"
                    >
                      {inner}
                    </Link>
                  ) : (
                    <div className="flex items-center gap-3 py-3.5 opacity-50 lg:py-3">
                      {inner}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>

          <Link
            href="/firewatch"
            className="mt-6 flex min-h-12 w-full items-center justify-between rounded-full bg-duck px-5 py-3 text-[15px] font-medium text-ink transition-transform hover:scale-[1.01] lg:min-h-0 lg:py-2.5 lg:text-[13px]"
          >
            Enter Fire Watch
            <span aria-hidden>→</span>
          </Link>
        </div>
      </aside>

      <section className="flex min-w-0 flex-col bg-[#05070b] pb-[max(1.5rem,env(safe-area-inset-bottom))] lg:min-h-0 lg:flex-1">
        <div className="flex shrink-0 items-center justify-between px-5 py-4 lg:px-6 lg:py-3">
          <p className="text-[13px] text-zinc-400 lg:text-[11px] lg:text-zinc-500">
            The Preservation Constitution
          </p>
          <a
            href={CONSTITUTION_HREF}
            download={CONSTITUTION_FILENAME}
            className="text-[13px] text-zinc-300 underline-offset-4 hover:text-duck hover:underline lg:text-[11px] lg:text-zinc-400"
          >
            Download
          </a>
        </div>
        <div className="px-3 pb-10 pt-2 sm:px-4 lg:min-h-0 lg:flex-1 lg:overflow-y-auto lg:px-10 lg:py-12 lg:pb-12 lg:pt-12">
          <ConstitutionPaperSlot />
        </div>
      </section>
    </main>
  );
}
