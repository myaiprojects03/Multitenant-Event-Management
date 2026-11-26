"use client";

import { ArrowUp, ChevronRight, Sparkles } from "lucide-react";
import { useState } from "react";
import { ResultChart } from "@/components/ResultChart";
import { Badge, Empty, ErrorNotice, Loading, PageHeader, Panel, Spinner, StatusBadge } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtCell, fmtMs, humanize } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { AnomalyReport, NlSqlResponse } from "@/lib/types";

const EXAMPLES = [
  "Which exhibitors had the most booth visits yesterday?",
  "How many check-ins per day?",
  "Which sessions are bookmarked beyond room capacity?",
  "How many attendees opted in to exhibitor matching?",
  "Leads per exhibitor by day",
];

function Result({ r }: { r: NlSqlResponse }) {
  const ok = r.status === "ok";
  return (
    <div className="stack">
      <Panel
        title={r.question}
        actions={
          <>
            <StatusBadge status={r.status} />
            {r.repaired && <Badge title="The first query failed and was repaired once">Repaired</Badge>}
            {r.cached && <Badge>Cached</Badge>}
            <span className="faint small">{fmtMs(r.latency_ms)}</span>
          </>
        }
      >
        {!ok && <ErrorNotice message={r.error ?? r.summary} />}
        {ok && (
          <>
            <p style={{ fontSize: 14.5 }}>{r.summary}</p>
            {r.key_points.length > 0 && (
              <ul className="facts" style={{ marginTop: 10 }}>
                {r.key_points.map((k) => (
                  <li key={k}>{k}</li>
                ))}
              </ul>
            )}
            {r.chart && r.chart.type !== "table" && (
              <div style={{ marginTop: 18 }}>
                <ResultChart chart={r.chart} rows={r.rows} />
              </div>
            )}
          </>
        )}
        {r.sql && (
          <details style={{ marginTop: 16 }}>
            <summary>
              <ChevronRight size={14} /> SQL{r.explanation ? ` · ${r.explanation}` : ""}
            </summary>
            <pre className="code" style={{ marginTop: 8 }}>
              {r.sql}
            </pre>
            <p className="hint" style={{ marginTop: 6 }}>
              Checked by the SQL guard (approved views only, SELECT only) and run as a read-only role with a 5 second timeout.
            </p>
          </details>
        )}
      </Panel>
      {ok && r.columns.length > 0 && (
        <Panel flush title={`${r.row_count} row${r.row_count === 1 ? "" : "s"}`} description={r.truncated ? "Truncated to the first rows" : undefined}>
          <div className="table-wrap" style={{ maxHeight: 420, overflowY: "auto" }}>
            <table className="table">
              <thead>
                <tr>
                  {r.columns.map((c) => (
                    <th key={c}>{humanize(c)}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {r.rows.map((row, i) => (
                  <tr key={i}>
                    {r.columns.map((c) => (
                      <td key={c} className={typeof row[c] === "number" ? "r num" : ""}>
                        {fmtCell(row[c])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
    </div>
  );
}

function Anomalies() {
  const [explain, setExplain] = useState(false);
  const q = useApi<AnomalyReport>(`/analytics/anomalies?explain=${explain}`);
  return (
    <Panel
      flush
      title="Flags"
      description="Computed with rules and statistics; the model only explains them."
      actions={
        !explain && (
          <button className="btn btn-sm" onClick={() => setExplain(true)} disabled={q.loading}>
            <Sparkles size={14} />
            Explain
          </button>
        )
      }
    >
      {q.loading && !q.data && <Loading rows={4} />}
      {q.error && (
        <div className="panel-body">
          <ErrorNotice message={q.error.message} />
        </div>
      )}
      {q.data && q.data.flags.length === 0 && <Empty title="No anomalies detected" />}
      {q.data && q.data.flags.length > 0 && (
        <div className="list">
          {q.data.flags.map((f, i) => (
            <div className="item" key={i}>
              <div className="row">
                <Badge tone={f.severity === "high" ? "bad" : "warn"}>{humanize(f.severity)}</Badge>
                <span className="item-title">{f.title}</span>
              </div>
              <p className="muted small" style={{ marginTop: 4 }}>
                {f.detail}
              </p>
            </div>
          ))}
        </div>
      )}
      {explain && (
        <div className="panel-body" style={{ borderTop: "1px solid var(--border)" }}>
          {q.loading ? (
            <div className="row faint">
              <Spinner /> Writing an explanation…
            </div>
          ) : (
            q.data?.explanation && <p style={{ whiteSpace: "pre-wrap" }}>{q.data.explanation}</p>
          )}
        </div>
      )}
    </Panel>
  );
}

export default function AnalyticsPage() {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<NlSqlResponse | null>(null);

  async function ask(question: string) {
    const text = question.trim();
    if (!text) return;
    setQ(text);
    setBusy(true);
    setError(null);
    try {
      setResult(await api<NlSqlResponse>("/analytics/nl-sql", { body: { question: text } }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title="BI copilot"
        description="Ask about registrations, attendance, booth traffic or leads in plain English. You get the SQL, the rows, a chart and a short summary."
      />
      <div className="split" style={{ gridTemplateColumns: "1fr 360px" }}>
        <div className="stack">
          <Panel>
            <form
              className="ask"
              onSubmit={(e) => {
                e.preventDefault();
                void ask(q);
              }}
            >
              <input className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. Which sessions lost the most attendees before the end?" maxLength={500} />
              <button className="btn btn-primary" disabled={busy || !q.trim()}>
                {busy ? <Spinner /> : <ArrowUp size={16} />}
                Ask
              </button>
            </form>
            <div className="row-wrap" style={{ marginTop: 12 }}>
              {EXAMPLES.map((e) => (
                <button key={e} className="chip" onClick={() => void ask(e)} disabled={busy}>
                  {e}
                </button>
              ))}
            </div>
          </Panel>
          <ErrorNotice message={error} />
          {busy && (
            <div className="panel">
              <div className="panel-body row faint">
                <Spinner /> Planning the query, checking it against the guard and running it read-only…
              </div>
              {!result && <Loading rows={3} />}
            </div>
          )}
          {result && <div style={{ opacity: busy ? 0.5 : 1 }}><Result r={result} /></div>}
        </div>
        <Anomalies />
      </div>
    </>
  );
}
