"use client";

import { useState } from "react";
import { Badge, Empty, ErrorNotice, Loading, PageHeader, Panel } from "@/components/ui";
import { KIND_LABEL, WorkflowDrawer, chainLabel } from "@/components/Workflow";
import { fmtRelative } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { useRequiredSession } from "@/lib/session";
import type { QueueRow } from "@/lib/types";

function recipients(q: QueueRow): string {
  const d = q.draft as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  if (Array.isArray(d.messages)) return `${d.messages.length} email${d.messages.length === 1 ? "" : "s"}`;
  if (d.change) return `${d.change.bookmarks} attendees + room move`;
  if (d.document) return "Attendee-visible document";
  return "—";
}

function subject(q: QueueRow): string {
  const d = q.draft as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  if (d.change) return `${d.change.session_title}: ${d.change.from_room} → ${d.change.to_room}`;
  if (d.document) return d.document.title;
  if (Array.isArray(d.messages)) return `Follow-ups to ${d.messages.slice(0, 2).map((m: { to_name: string }) => m.to_name).join(", ")}${d.messages.length > 2 ? ` and ${d.messages.length - 2} more` : ""}`;
  return "";
}

// flags of the draft as it is now (edits can add them), not of the agent's first version
function currentFlags(q: QueueRow): string[] {
  const msgs = (q.draft as { messages?: { flags?: string[] }[] }).messages;
  if (Array.isArray(msgs)) return [...new Set(msgs.flatMap((m) => m.flags ?? []))];
  return q.flags ?? [];
}

export default function ApprovalsPage() {
  const session = useRequiredSession();
  const queue = useApi<QueueRow[]>("/approvals", { pollMs: 5000 });
  const [open, setOpen] = useState<string | null>(null);

  return (
    <>
      <PageHeader
        title="Approvals"
        description={
          session.role === "exhibitor"
            ? `Drafts that ${session.exhibitor_name} owns. Nothing is sent until you approve; bulk or flagged drafts also need an organizer.`
            : "Schedule changes and recaps you own, plus exhibitor drafts escalated for being bulk or flagged."
        }
      />
      <Panel flush title="Waiting for you" description="Approved drafts are sent once, with retries; rejected drafts are never sent.">
        {queue.loading && !queue.data && <Loading />}
        {queue.error && (
          <div className="panel-body">
            <ErrorNotice message={queue.error.message} />
          </div>
        )}
        {queue.data && queue.data.length === 0 && <Empty title="Nothing waiting for approval">New drafts appear here as agents finish them.</Empty>}
        {queue.data && queue.data.length > 0 && (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Request</th>
                  <th>Reach</th>
                  <th>Step</th>
                  <th>Flags</th>
                  <th>Requested</th>
                  <th>Expires</th>
                </tr>
              </thead>
              <tbody>
                {queue.data.map((q) => (
                  <tr key={q.id} className="clickable" onClick={() => setOpen(q.workflow_id)}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{KIND_LABEL[q.kind] ?? q.kind}</div>
                      <div className="muted small" style={{ maxWidth: 420 }}>{subject(q)}</div>
                    </td>
                    <td className="nowrap">{recipients(q)}</td>
                    <td className="nowrap">
                      {q.escalation_level === 0 ? (
                        <>
                          Owner approval
                          <div className="faint small">{chainLabel(q.approval_chain)}</div>
                        </>
                      ) : (
                        <>
                          <Badge tone="warn">Escalated</Badge>
                          <div className="faint small">Owner already approved</div>
                        </>
                      )}
                    </td>
                    <td>
                      <div className="row-wrap">
                        {currentFlags(q).length === 0 ? <span className="faint">None</span> : currentFlags(q).map((f) => <Badge key={f} tone="warn">{f}</Badge>)}
                      </div>
                    </td>
                    <td className="faint nowrap">{fmtRelative(q.created_at)}</td>
                    <td className="faint nowrap">{fmtRelative(q.expires_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
      {open && <WorkflowDrawer id={open} onClose={() => setOpen(null)} onChanged={queue.reload} />}
    </>
  );
}
