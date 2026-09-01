"use client";

/**
 * The AI analyst.
 *
 * Two things are deliberately always visible: which tools were called to get
 * the answer, and what the answer is allowed to claim. An answer with no
 * citations is presented as a weakness, not smoothed over.
 */

import { useEffect, useRef, useState } from "react";

import { api, type AnalystResponse } from "@/lib/api";
import { Callout, Collapsible, ErrorNote, Panel, Spinner } from "@/components/ui";

interface AiStatus {
  llm_enabled: boolean;
  model: string | null;
  mode: string;
  mode_explanation: string;
  tools: { name: string; description: string }[];
  documents: {
    document_id: string;
    title: string;
    status: string;
    message: string | null;
    source_url: string | null;
    quotable: boolean;
  }[];
  suggested_questions: string[];
  guardrails: string[];
}

interface Exchange {
  question: string;
  response: AnalystResponse | null;
  error: string | null;
}

interface Props {
  municipalityId: string;
  date: string | null;
  selected: { lat: number; lon: number } | null;
}

export default function AnalystPanel({ municipalityId, date, selected }: Props) {
  const [status, setStatus] = useState<AiStatus | null>(null);
  const [question, setQuestion] = useState("");
  const [history, setHistory] = useState<Exchange[]>([]);
  const [pending, setPending] = useState(false);
  const scroller = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    api
      .aiStatus(municipalityId)
      .then(setStatus)
      .catch(() => setStatus(null));
  }, [municipalityId]);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [history, pending]);

  const ask = async (text: string) => {
    const trimmed = text.trim();
    if (!trimmed || pending) return;
    setQuestion("");
    setPending(true);
    const context: Record<string, unknown> = {};
    if (date) context.date = date;
    if (selected) {
      context.lat = selected.lat;
      context.lon = selected.lon;
    }
    try {
      const response = await api.ask(municipalityId, trimmed, context);
      setHistory((prev) => [...prev, { question: trimmed, response, error: null }]);
    } catch (error) {
      setHistory((prev) => [
        ...prev,
        { question: trimmed, response: null, error: (error as Error).message },
      ]);
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        {status && (
          <Panel
            title="Analyst"
            subtitle={status.mode_explanation}
            right={
              <span
                className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
                  status.llm_enabled
                    ? "bg-emerald-500/15 text-emerald-300"
                    : "bg-amber-500/15 text-amber-300"
                }`}
              >
                {status.llm_enabled ? status.model : "deterministic"}
              </span>
            }
          >
            {!status.llm_enabled && (
              <Callout tone="warn">
                No language model is configured, so the analyst is running in
                deterministic mode: it matches your question to a tool, runs it, and
                reports the result verbatim. Every number is still real.
              </Callout>
            )}

            {status.documents.some((d) => !d.quotable) && (
              <div className="mt-2">
                <Callout tone="gap">
                  {status.documents
                    .filter((d) => !d.quotable)
                    .map((d) => (
                      <div key={d.document_id} className="mb-1 last:mb-0">
                        <span className="text-purple-100">{d.title}</span> is not
                        loaded, so the analyst cannot quote it. {d.message}
                      </div>
                    ))}
                </Callout>
              </div>
            )}

            <div className="mt-3 space-y-1">
              {status.suggested_questions.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  onClick={() => ask(suggestion)}
                  className="block w-full rounded border border-white/10 bg-white/[0.02] px-2.5 py-1.5 text-left text-[11px] leading-snug text-zinc-300 hover:border-duck/40 hover:text-white"
                >
                  {suggestion}
                </button>
              ))}
            </div>

            <Collapsible title="What the analyst may and may not do" count={status.guardrails.length}>
              <ul className="space-y-1">
                {status.guardrails.map((rule, index) => (
                  <li key={index} className="text-[10px] leading-snug text-zinc-500">
                    · {rule}
                  </li>
                ))}
              </ul>
            </Collapsible>
          </Panel>
        )}

        <div className="space-y-3 px-4 py-3">
          {history.map((exchange, index) => (
            <div key={index} className="space-y-2">
              <div className="rounded bg-duck/10 px-2.5 py-1.5 text-[12px] text-duck">
                {exchange.question}
              </div>
              {exchange.error ? (
                <ErrorNote error={exchange.error} />
              ) : (
                exchange.response && <Answer response={exchange.response} />
              )}
            </div>
          ))}
          {pending && <Spinner label="Querying the operating picture" />}
        </div>
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          ask(question);
        }}
        className="border-t border-white/10 p-3"
      >
        {selected && (
          <div className="mb-1.5 text-[10px] text-zinc-500">
            Context: the selected cell at {selected.lat.toFixed(4)},{" "}
            {selected.lon.toFixed(4)}
            {date ? ` on ${date}` : ""}
          </div>
        )}
        <div className="flex gap-2">
          <input
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="Ask about this municipality"
            className="min-h-11 flex-1 rounded border border-white/10 bg-black/40 px-2.5 py-2 text-[16px] text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-duck/60 lg:min-h-0 lg:py-1.5 lg:text-[12px]"
          />
          <button
            type="submit"
            disabled={pending || !question.trim()}
            className="rounded bg-duck px-3 py-1.5 text-[12px] font-medium text-black disabled:opacity-40"
          >
            Ask
          </button>
        </div>
      </form>
    </div>
  );
}

function Answer({ response }: { response: AnalystResponse }) {
  return (
    <div className="rounded border border-white/10 bg-white/[0.02] px-2.5 py-2">
      <div className="whitespace-pre-wrap text-[12px] leading-relaxed text-zinc-200">
        {response.answer}
      </div>

      {response.citations.length === 0 && (
        <div className="mt-2">
          <Callout tone="warn">
            This answer cites no dataset or document. Treat it as unverified.
          </Callout>
        </div>
      )}

      {response.citations.length > 0 && (
        <Collapsible title="Citations" count={response.citations.length} defaultOpen>
          <div className="space-y-1.5">
            {response.citations.map((citation, index) => (
              <div key={index} className="text-[10px] leading-snug text-zinc-500">
                <span className="text-zinc-300">
                  {citation.title || citation.source_id || citation.document_id}
                </span>
                {citation.page ? ` · p.${citation.page}` : ""}
                {citation.observed_at ? ` · observed ${citation.observed_at.slice(0, 10)}` : ""}
                {citation.attribution ? ` · ${citation.attribution}` : ""}
              </div>
            ))}
          </div>
        </Collapsible>
      )}

      {response.tool_calls.length > 0 && (
        <Collapsible title="Tools called" count={response.tool_calls.length}>
          <div className="space-y-1">
            {response.tool_calls.map((call, index) => (
              <div key={index} className="font-mono text-[10px] text-zinc-500">
                {call.tool}
                {call.error && <span className="text-red-300"> — {call.error}</span>}
              </div>
            ))}
          </div>
        </Collapsible>
      )}

      {response.notes.map((note, index) => (
        <div key={index} className="mt-1.5 text-[10px] leading-snug text-amber-300/70">
          {note}
        </div>
      ))}
    </div>
  );
}
