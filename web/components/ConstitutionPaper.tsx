"use client";

import { useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import "react-pdf/dist/Page/TextLayer.css";

pdfjs.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";

const SRC = "/constitution.pdf";

export default function ConstitutionPaper() {
  const host = useRef<HTMLDivElement>(null);
  const [ready, setReady] = useState(false);
  const [width, setWidth] = useState(720);
  const [pages, setPages] = useState(0);

  useEffect(() => {
    setReady(true);
  }, []);

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const measure = () => {
      const next = Math.min(760, Math.floor(el.clientWidth));
      if (next > 0) setWidth(next);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return (
    <div ref={host} className="mx-auto w-full max-w-[760px]">
      {!ready ? (
        <p className="py-24 text-center text-[12px] text-zinc-600">
          Setting the paper…
        </p>
      ) : (
      <Document
        file={SRC}
        loading={
          <p className="py-24 text-center text-[12px] text-zinc-600">
            Setting the paper…
          </p>
        }
        error={
          <p className="py-24 text-center text-[12px] text-zinc-500">
            The Constitution could not be rendered.{" "}
            <a href={SRC} className="text-duck underline-offset-4 hover:underline">
              Open the PDF
            </a>
            .
          </p>
        }
        onLoadSuccess={({ numPages }) => setPages(numPages)}
      >
        {Array.from({ length: pages }, (_, index) => (
          <div key={index + 1} className="constitution-sheet">
            <Page
              pageNumber={index + 1}
              width={width}
              renderAnnotationLayer={false}
              renderTextLayer
              className="constitution-page"
            />
          </div>
        ))}
      </Document>
      )}
    </div>
  );
}
