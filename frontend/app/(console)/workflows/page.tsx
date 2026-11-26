"use client";

import { useState } from "react";
import { Badge, Empty, ErrorNotice, Loading, PageHeader, Panel, Spinner, Tabs } from "@/components/ui";
import { WorkflowDrawer } from "@/components/Workflow";
import { WorkflowTable } from "@/components/WorkflowTable";
import { api } from "@/lib/api";
import { fmtDay, fmtRelative, fmtTime } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { ProgramSession, SentMessage, WorkflowRow } from "@/lib/types";

type Sim = "none" | "flaky" | "permanent";
const SIM: Record<Sim, object | undefined> = {
  none: undefined,
  flaky: { fail_times: 1 },
  permanent: { permanent: true },
};

function SessionTable({
  when,
  onStart,
  busy,
}: {
  when: "upcoming" | "past";
  onStart: (s: ProgramSession) => void;
  busy: string | null;
}) {
  const q = useApi<ProgramSession[]>(`/sessions?when=${when}`);
  if (q.loading && !q.data) return <Loading rows={5} />;
  if (q.error) return <div className="panel-body"><ErrorNotice message={q.error.message} /></div>;
  const rows = [...(q.data ?? [])];
  if (when === "upcoming") rows.sort((a, b) => b.bookmarks / (b.room_capacity || 1) - a.bookmarks / (a.room_capacity || 1));
  else rows.reverse();
  if (!rows.length) return <Empty title={when === "upcoming" ? "No upcoming sessions" : "No finished sessions yet"} />;
  return (
    <div className="table-wrap" style={{ maxHeight: 420, overflowY: "auto" }}>
      <table className="table">
        <thead>
          <tr>
            <th>Session</th>
            <th>When</th>
            <th>Room</th>
            <th className="r">{when === "upcoming" ? "Bookmarks / seats" : "Attended"}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 40).map((s) => {
            const over = when === "upcoming" && s.room_capacity != null && s.bookmarks > s.room_capacity;
            return (
              <tr key={s.id}>
                <td style={{ maxWidth: 380 }}>
                  <div style={{ fontWeight: 600 }}>{s.title}</div>
                  <div className="faint small">{s.kind}</div>
                </td>
                <td className="nowrap muted">
                  {fmtDay(s.starts_at)} · {fmtTime(s.starts_at)}
                </td>
                <td className="nowrap">{s.room ?? "—"}</td>
                <td className="r nowrap num">
                  {when === "upcoming" ? (
                    <>
                      {over && <Badge tone="warn">Over capacity</Badge>} {s.bookmarks} / {s.room_capacity ?? "—"}
                    </>
                  ) : (
                    s.attended
                  )}
                </td>
                <td className="r">
                  {(when === "past" || over) && (
                    <button className="btn btn-sm" disabled={busy !== null} onClick={() => onStart(s)}>
                      {busy === s.id && <Spinner size={12} />}
                      {when === "past" ? "Draft recap" : "Find a bigger room"}
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Outbox() {
  const q = useApi<SentMessage[]>("/workflows/outbox/sent?limit=100", { pollMs: 5000 });
  const [open, setOpen] = useState<string | null>(null);
  if (q.loading && !q.data) return <Loading />;
  if (q.error) return <div className="panel-body"><ErrorNotice message={q.error.message} /></div>;
  if (!q.data?.length) return <Empty title="Nothing sent yet">Approved emails land here. Nothing leaves the machine.</Empty>;
  return (
    <div className="list">
      {q.data.map((m) => (
        <div key={m.id} className="item clickable" style={{ cursor: "pointer" }} onClick={() => setOpen(open === m.id ? null : m.id)}>
          <div className="row">
            <span style={{ fontWeight: 600 }}>{m.subject}</span>
            <span className="spacer" />
            <span className="faint small nowrap">{fmtRelative(m.sent_at)}</span>
          </div>
          <div className="muted small">To {m.to}</div>
          {open === m.id && <div style={{ whiteSpace: "pre-wrap", marginTop: 8 }}>{m.body}</div>}
        </div>
      ))}
    </div>
  );
}

export default function WorkflowsPage() {
  const list = useApi<WorkflowRow[]>("/workflows?limit=30", { pollMs: 4000 });
  const [tab, setTab] = useState<"upcoming" | "past">("upcoming");
  const [lower, setLower] = useState<"workflows" | "outbox">("workflows");
  const [sim, setSim] = useState<Sim>("none");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  async function start(s: ProgramSession) {
    setBusy(s.id);
    setError(null);
    try {
      const r = await api<{ workflow_id: string }>("/workflows/trigger", {
        body: {
          kind: tab === "upcoming" ? "schedule_change" : "session_summary",
          params: { session_id: s.id, ...(SIM[sim] ? { simulate: SIM[sim] } : {}) },
        },
      });
      await list.reload();
      setOpen(r.workflow_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <PageHeader
        title="Workflows"
        description="The Operations Agent proposes room changes for overbooked sessions and drafts recaps from real attendance. You approve before the schedule changes or anything is published."
      />
      <div className="stack">
        <section className="panel">
          <div className="panel-head" style={{ borderBottom: 0, paddingBottom: 0 }}>
            <Tabs
              value={tab}
              onChange={setTab}
              items={[
                { value: "upcoming", label: "Upcoming: capacity" },
                { value: "past", label: "Finished: recaps" },
              ]}
            />
            <div className="actions" style={{ marginBottom: 18 }}>
              <label className="faint small" htmlFor="sim">
                Delivery
              </label>
              <select id="sim" className="select" style={{ width: "auto", minHeight: 30, padding: "2px 8px" }} value={sim} onChange={(e) => setSim(e.target.value as Sim)}>
                <option value="none">Normal</option>
                <option value="flaky">Simulate a transient failure</option>
                <option value="permanent">Simulate a permanent failure</option>
              </select>
            </div>
          </div>
          {error && (
            <div className="panel-body" style={{ paddingTop: 0 }}>
              <ErrorNotice message={error} />
            </div>
          )}
          <div style={{ borderTop: "1px solid var(--border)" }}>
            <SessionTable key={tab} when={tab} onStart={start} busy={busy} />
          </div>
        </section>

        <section className="panel">
          <div className="panel-head" style={{ borderBottom: 0, paddingBottom: 0 }}>
            <Tabs
              value={lower}
              onChange={setLower}
              items={[
                { value: "workflows", label: "Recent workflows" },
                { value: "outbox", label: "Sent messages" },
              ]}
            />
          </div>
          <div style={{ borderTop: "1px solid var(--border)" }}>
            {lower === "workflows" ? <WorkflowTable q={list} onOpen={setOpen} /> : <Outbox />}
          </div>
        </section>
      </div>
      {open && <WorkflowDrawer id={open} onClose={() => setOpen(null)} onChanged={list.reload} />}
    </>
  );
}
