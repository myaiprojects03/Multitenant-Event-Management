"use client";

import { ScanLine } from "lucide-react";
import { useState } from "react";
import { Badge, Empty, ErrorNotice, Loading, PageHeader, Panel, Spinner, StatusBadge } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtRelative } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { useRequiredSession } from "@/lib/session";
import type { ConnectionRow, MeetingRow } from "@/lib/types";

function Respond({ path, onDone }: { path: string; onDone: () => void }) {
  const [busy, setBusy] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  async function go(accept: boolean) {
    setBusy(accept);
    setError(null);
    try {
      await api(path, { body: { accept } });
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }
  return (
    <div className="row" style={{ justifyContent: "flex-end" }}>
      {error && <span className="small" style={{ color: "var(--bad)" }}>{error}</span>}
      <button className="btn btn-sm" onClick={() => go(false)} disabled={busy !== null}>
        Decline
      </button>
      <button className="btn btn-sm btn-primary" onClick={() => go(true)} disabled={busy !== null}>
        {busy === true && <Spinner size={12} />}
        Accept
      </button>
    </div>
  );
}

function Meetings({ role }: { role: "attendee" | "exhibitor" }) {
  const meetings = useApi<MeetingRow[]>("/meetings");
  const receiver = role === "attendee" ? "exhibitor" : "attendee";
  return (
    <Panel
      flush
      title="Meeting requests"
      description={
        role === "exhibitor"
          ? "Prospects stay anonymous until they accept. Accepting creates the lead."
          : "Accepting an exhibitor's request shares your name and email with them."
      }
    >
      {meetings.loading && !meetings.data && <Loading />}
      {meetings.error && <div className="panel-body"><ErrorNotice message={meetings.error.message} /></div>}
      {meetings.data && meetings.data.length === 0 && <Empty title="No meeting requests yet" />}
      {meetings.data && meetings.data.length > 0 && (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{role === "exhibitor" ? "Attendee" : "Exhibitor"}</th>
                <th>Requested by</th>
                <th>Message</th>
                <th>Status</th>
                <th>When</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {meetings.data.map((m) => (
                <tr key={m.id}>
                  <td className="nowrap">
                    {role === "exhibitor" ? (
                      m.attendee_name === "Anonymous prospect" ? <span className="faint">Anonymous prospect</span> : m.attendee_name
                    ) : (
                      <>
                        {m.exhibitor_name}
                        {m.booth && <span className="faint"> · booth {m.booth}</span>}
                      </>
                    )}
                  </td>
                  <td>{m.initiated_by === role ? "You" : m.initiated_by === "exhibitor" ? "Exhibitor" : "Attendee"}</td>
                  <td className="muted" style={{ maxWidth: 320 }}>{m.message || "—"}</td>
                  <td><StatusBadge status={m.status} request /></td>
                  <td className="faint nowrap">{fmtRelative(m.created_at)}</td>
                  <td className="r">
                    {m.status === "pending" && m.initiated_by === receiver && (
                      <Respond path={`/meetings/${m.id}/respond`} onDone={meetings.reload} />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

function Connections() {
  const conns = useApi<ConnectionRow[]>("/connections");
  return (
    <Panel flush title="Connections" description="Email addresses appear only after both sides accept.">
      {conns.loading && !conns.data && <Loading />}
      {conns.error && <div className="panel-body"><ErrorNotice message={conns.error.message} /></div>}
      {conns.data && conns.data.length === 0 && <Empty title="No connections yet">Send requests from Recommendations.</Empty>}
      {conns.data && conns.data.length > 0 && (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Person</th>
                <th>Direction</th>
                <th>Contact</th>
                <th>Status</th>
                <th>When</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {conns.data.map((c) => (
                <tr key={c.id}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{c.display_name ?? "Attendee"}</div>
                    <div className="faint small">{c.job_title}</div>
                  </td>
                  <td>{c.direction === "incoming" ? "Received" : "Sent"}</td>
                  <td>{c.contact_email ?? <span className="faint">Hidden until accepted</span>}</td>
                  <td><StatusBadge status={c.status} request /></td>
                  <td className="faint nowrap">{fmtRelative(c.created_at)}</td>
                  <td className="r">
                    {c.status === "pending" && c.direction === "incoming" && (
                      <Respond path={`/connections/${c.id}/respond`} onDone={conns.reload} />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

function BadgeScan() {
  const [id, setId] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  async function scan() {
    setBusy(true);
    setResult(null);
    try {
      const r = await api<{ lead_id: string }>("/leads/badge-scan", { body: { attendee_user_id: id.trim() } });
      setResult({ ok: true, text: `Lead captured (${r.lead_id.slice(0, 8)}). The attendee's contact details are now visible to your team.` });
      setId("");
    } catch (e) {
      setResult({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  }
  return (
    <Panel title="Badge scan" description="Scanning a badge at the booth is the attendee's in-person consent to share contact details.">
      <form
        className="ask"
        onSubmit={(e) => {
          e.preventDefault();
          void scan();
        }}
      >
        <input className="input mono" value={id} onChange={(e) => setId(e.target.value)} placeholder="Badge code (attendee user id)" />
        <button className="btn" disabled={busy || id.trim().length < 32}>
          {busy ? <Spinner /> : <ScanLine size={15} />}
          Scan
        </button>
      </form>
      {result && (
        <div className={`notice ${result.ok ? "notice-ok" : "notice-bad"}`} style={{ marginTop: 12 }}>
          {result.text}
        </div>
      )}
    </Panel>
  );
}

export default function NetworkPage() {
  const session = useRequiredSession();
  const role = session.role as "attendee" | "exhibitor";
  return (
    <>
      <PageHeader
        title={role === "exhibitor" ? "Meetings & scans" : "Connections & meetings"}
        description={
          role === "exhibitor" ? (
            <>
              Requests between <Badge>{session.exhibitor_name}</Badge> and attendees. Leads exist only after an
              accepted meeting or a badge scan.
            </>
          ) : (
            "Requests you sent and received."
          )
        }
      />
      <div className="stack">
        {role === "attendee" && <Connections />}
        <Meetings role={role} />
        {role === "exhibitor" && <BadgeScan />}
      </div>
    </>
  );
}
