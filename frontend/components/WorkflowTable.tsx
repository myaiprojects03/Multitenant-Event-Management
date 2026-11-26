"use client";

import type { WorkflowRow } from "@/lib/types";
import { fmtRelative } from "@/lib/format";
import { Empty, ErrorNotice, Loading, StatusBadge } from "./ui";
import { KIND_LABEL, chainLabel } from "./Workflow";
import type { Query } from "@/lib/hooks";

export function WorkflowTable({ q, onOpen }: { q: Query<WorkflowRow[]>; onOpen: (id: string) => void }) {
  if (q.loading && !q.data) return <Loading />;
  if (q.error)
    return (
      <div className="panel-body">
        <ErrorNotice message={q.error.message} />
      </div>
    );
  if (!q.data?.length) return <Empty title="No workflows yet">Start one above.</Empty>;
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>Workflow</th>
            <th>Status</th>
            <th>Approval chain</th>
            <th>Started</th>
            <th>Updated</th>
          </tr>
        </thead>
        <tbody>
          {q.data.map((w) => (
            <tr key={w.id} className="clickable" onClick={() => onOpen(w.id)}>
              <td>
                <div style={{ fontWeight: 600 }}>{KIND_LABEL[w.kind] ?? w.kind}</div>
                {w.error && <div className="small" style={{ color: "var(--bad)" }}>{w.error}</div>}
              </td>
              <td><StatusBadge status={w.status} /></td>
              <td className="muted">{chainLabel(w.approval_chain)}</td>
              <td className="faint nowrap">{fmtRelative(w.created_at)}</td>
              <td className="faint nowrap">{fmtRelative(w.updated_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
