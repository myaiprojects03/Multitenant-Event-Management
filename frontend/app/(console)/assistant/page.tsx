"use client";

import { ArrowUp } from "lucide-react";
import { Fragment, useEffect, useState, type ReactNode } from "react";
import { Badge, ErrorNotice, PageHeader, Panel, Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtMs, humanize } from "@/lib/format";
import { useRequiredSession } from "@/lib/session";
import type { AssistantAnswer, Role } from "@/lib/types";

const SUGGESTED: Record<Role, string[]> = {
  attendee: [
    "Which room is the FHIR workshop in?",
    "What are the best sessions on radiology AI?",
    "Bookmark the FHIR workshop for me",
    "Which exhibitors work on remote patient monitoring?",
  ],
  exhibitor: [
    "Where is our booth?",
    "Which sessions cover medical imaging?",
    "Which exhibitors offer clinical decision support?",
  ],
  organizer: [
    "Which room is the FHIR workshop in?",
    "Summarize the sessions on interoperability",
    "Who is speaking about value-based care?",
  ],
};

const ROUTES: Record<string, [string, "neutral" | "ok" | "warn" | "bad" | "accent"]> = {
  exact_lookup: ["Direct lookup", "neutral"],
  general: ["Grounded answer", "accent"],
  action: ["Tool action", "ok"],
  restricted: ["Refused: restricted", "bad"],
  out_of_scope: ["Refused: out of scope", "warn"],
};

interface Turn {
  id: number;
  question: string;
  answer?: AssistantAnswer;
  error?: string;
}

function withCitations(text: string, onCite: (n: number) => void, refs: Set<number>): ReactNode[] {
  return text.split(/(\[\d+\])/g).map((part, i) => {
    const m = part.match(/^\[(\d+)\]$/);
    if (m && refs.has(Number(m[1]))) {
      const n = Number(m[1]);
      return (
        <button key={i} className="cite" onClick={() => onCite(n)} title={`Source ${n}`}>
          {n}
        </button>
      );
    }
    return <Fragment key={i}>{part}</Fragment>;
  });
}

function AnswerBlock({ turn }: { turn: Turn }) {
  const [active, setActive] = useState<number | null>(null);
  const a = turn.answer;
  if (turn.error) return <ErrorNotice message={turn.error} />;
  if (!a)
    return (
      <div className="row faint">
        <Spinner /> Searching the event content…
      </div>
    );
  const refs = new Set(a.citations.map((c) => c.ref));
  const [label, tone] = ROUTES[a.route] ?? [humanize(a.route), "neutral" as const];
  return (
    <>
      <div className="turn-a">{withCitations(a.answer, setActive, refs)}</div>
      {a.citations.length > 0 && (
        <div className="sources">
          {a.citations.map((c) => (
            <div className="source" key={c.ref} data-active={active === c.ref}>
              <span className="cite" style={{ cursor: "default" }}>
                {c.ref}
              </span>
              <div style={{ minWidth: 0 }}>
                <div>
                  <strong style={{ fontWeight: 600 }}>{c.title}</strong>{" "}
                  <span className="faint">· {humanize(c.entity_type)}</span>
                </div>
                <div className="muted" style={{ marginTop: 2 }}>
                  {c.snippet}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
      <div className="turn-meta">
        <Badge tone={tone}>{label}</Badge>
        {a.action && <Badge>{humanize(a.action)}</Badge>}
        {a.cached && <Badge>Cached</Badge>}
        <span className="faint small">{fmtMs(a.latency_ms)}</span>
      </div>
    </>
  );
}

export default function AssistantPage() {
  const session = useRequiredSession();
  const [q, setQ] = useState("");
  const key = `eventai.assistant.${session.user_id}.${session.event_id}`;
  const [turns, setTurns] = useState<Turn[]>([]);
  const busy = turns.some((t) => !t.answer && !t.error);

  // keep the transcript while moving between pages (this tab only; finished turns only)
  useEffect(() => {
    try {
      const saved = sessionStorage.getItem(key);
      if (saved) setTurns(JSON.parse(saved) as Turn[]);
    } catch {
      /* storage unavailable */
    }
  }, [key]);
  useEffect(() => {
    try {
      sessionStorage.setItem(key, JSON.stringify(turns.filter((t) => t.answer || t.error).slice(0, 20)));
    } catch {
      /* storage unavailable */
    }
  }, [key, turns]);

  async function ask(question: string) {
    const text = question.trim();
    if (!text || busy) return;
    const id = Date.now();
    setTurns((ts) => [{ id, question: text }, ...ts]);
    setQ("");
    try {
      const answer = await api<AssistantAnswer>("/assistant/query", { body: { question: text } });
      setTurns((ts) => ts.map((t) => (t.id === id ? { ...t, answer } : t)));
    } catch (e) {
      setTurns((ts) => ts.map((t) => (t.id === id ? { ...t, error: e instanceof Error ? e.message : String(e) } : t)));
    }
  }

  return (
    <>
      <PageHeader
        title="Event assistant"
        description={
          <>
            Answers come from {session.event_name} content you are allowed to see, with numbered sources. Room
            and time questions are answered straight from the schedule.
          </>
        }
      />
      <div className="stack">
        <Panel>
          <form
            className="ask"
            onSubmit={(e) => {
              e.preventDefault();
              void ask(q);
            }}
          >
            <input
              className="input"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Ask about sessions, speakers, exhibitors or logistics"
              maxLength={500}
              autoFocus
            />
            <button className="btn btn-primary" disabled={!q.trim() || busy}>
              {busy ? <Spinner /> : <ArrowUp size={16} />}
              Ask
            </button>
          </form>
          <div className="row-wrap" style={{ marginTop: 12 }}>
            <span className="faint small" style={{ alignSelf: "center" }}>
              Try
            </span>
            {SUGGESTED[session.role].map((s) => (
              <button key={s} className="chip" onClick={() => void ask(s)} disabled={busy}>
                {s}
              </button>
            ))}
          </div>
        </Panel>

        {turns.length > 0 && (
          <section className="panel">
            {turns.map((t) => (
              <div className="turn" key={t.id}>
                <div className="turn-q">{t.question}</div>
                <AnswerBlock turn={t} />
              </div>
            ))}
          </section>
        )}
      </div>
    </>
  );
}
