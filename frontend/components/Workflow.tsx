"use client";

import { AlertTriangle, ArrowRight, RotateCcw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { fmtDateTime, fmtRelative, fmtTime, humanize } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { useRequiredSession } from "@/lib/session";
import type { ActionRow, ApprovalRow, FollowUpMessage, WorkflowDetail } from "@/lib/types";
import { Badge, Drawer, ErrorNotice, Loading, Spinner, StatusBadge, Tabs } from "./ui";

export const KIND_LABEL: Record<string, string> = {
  lead_follow_up: "Lead follow-up emails",
  session_summary: "Session recap",
  schedule_change: "Room change",
};

const CHANNEL: Record<string, string> = { email: "Email", platform_api: "Platform API", publish: "Publish" };
const LIVE = new Set(["pending", "running", "approved", "executing"]);

export function chainLabel(chain: string[] | null | undefined) {
  if (!chain?.length) return "—";
  return chain.map(humanize).join(" → ");
}

// ---------------------------------------------------------------------------------------------
// draft review / editing

type Edits = Record<string, unknown> | null;

function FollowUpDraft({
  draft,
  editable,
  onEdits,
}: {
  draft: { messages: FollowUpMessage[] };
  editable: boolean;
  onEdits: (e: Edits) => void;
}) {
  const [state, setState] = useState(() =>
    Object.fromEntries(draft.messages.map((m) => [m.lead_id, { subject: m.subject, body: m.body, remove: false }])),
  );
  useEffect(() => {
    const msgs = draft.messages
      .map((m) => {
        const s = state[m.lead_id]!;
        if (s.remove) return { lead_id: m.lead_id, remove: true };
        const e: Record<string, unknown> = { lead_id: m.lead_id };
        if (s.subject !== m.subject) e.subject = s.subject;
        if (s.body !== m.body) e.body = s.body;
        return Object.keys(e).length > 1 ? e : null;
      })
      .filter(Boolean);
    onEdits(msgs.length ? { messages: msgs } : null);
  }, [state, draft.messages, onEdits]);

  const kept = draft.messages.filter((m) => !state[m.lead_id]!.remove).length;
  return (
    <div className="stack">
      <p className="muted">
        {draft.messages.length} message{draft.messages.length === 1 ? "" : "s"} drafted
        {editable && kept !== draft.messages.length && <> · {kept} will be sent</>}. Recipients come from your leads
        and cannot be changed here.
      </p>
      {draft.messages.map((m) => {
        const s = state[m.lead_id]!;
        const set = (patch: Partial<typeof s>) => setState((st) => ({ ...st, [m.lead_id]: { ...s, ...patch } }));
        return (
          <section key={m.lead_id} className="panel" style={{ opacity: s.remove ? 0.55 : 1 }}>
            <div className="panel-head">
              <div style={{ minWidth: 0 }}>
                <h3>
                  {m.to_name} <span className="faint" style={{ fontWeight: 400 }}>&lt;{m.to}&gt;</span>
                </h3>
                <p>
                  Tier {m.tier} · score {m.score} · {m.reasons.slice(0, 3).join("; ")}
                </p>
              </div>
              <div className="actions">
                {m.flags.map((f) => (
                  <Badge key={f} tone="warn" title="Flagged: needs organizer approval">
                    {f}
                  </Badge>
                ))}
                {editable && (
                  <label className="check small muted">
                    <input type="checkbox" checked={s.remove} onChange={(e) => set({ remove: e.target.checked })} />
                    Don&apos;t send
                  </label>
                )}
              </div>
            </div>
            <div className="panel-body" style={{ display: "grid", gap: 10 }}>
              {editable && !s.remove ? (
                <>
                  <div className="field">
                    <label>Subject</label>
                    <input className="input" value={s.subject} maxLength={120} onChange={(e) => set({ subject: e.target.value })} />
                  </div>
                  <div className="field">
                    <label>Body</label>
                    <textarea className="textarea" rows={7} value={s.body} maxLength={2000} onChange={(e) => set({ body: e.target.value })} />
                  </div>
                </>
              ) : (
                <>
                  <div style={{ fontWeight: 600 }}>{s.subject}</div>
                  <div style={{ whiteSpace: "pre-wrap" }} className="muted">
                    {s.body}
                  </div>
                </>
              )}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function TextBlockDraft({
  block,
  value,
  fields,
  editable,
  onEdits,
}: {
  block: "announcement" | "document";
  value: Record<string, string>;
  fields: { key: string; label: string; max: number; rows?: number }[];
  editable: boolean;
  onEdits: (e: Edits) => void;
}) {
  const [state, setState] = useState(value);
  useEffect(() => {
    const changed = Object.fromEntries(fields.filter((f) => state[f.key] !== value[f.key]).map((f) => [f.key, state[f.key]]));
    onEdits(Object.keys(changed).length ? { [block]: changed } : null);
  }, [state, value, fields, block, onEdits]);
  return (
    <div style={{ display: "grid", gap: 10 }}>
      {fields.map((f) =>
        editable ? (
          <div className="field" key={f.key}>
            <label>{f.label}</label>
            {f.rows ? (
              <textarea className="textarea" rows={f.rows} maxLength={f.max} value={state[f.key]} onChange={(e) => setState({ ...state, [f.key]: e.target.value })} />
            ) : (
              <input className="input" maxLength={f.max} value={state[f.key]} onChange={(e) => setState({ ...state, [f.key]: e.target.value })} />
            )}
          </div>
        ) : (
          <div key={f.key}>
            <div className="label">{f.label}</div>
            <div style={{ whiteSpace: "pre-wrap", marginTop: 2 }}>{state[f.key]}</div>
          </div>
        ),
      )}
    </div>
  );
}

const ANNOUNCE_FIELDS = [
  { key: "subject", label: "Announcement subject", max: 120 },
  { key: "body", label: "Announcement", max: 900, rows: 5 },
];
const DOC_FIELDS = [
  { key: "title", label: "Title", max: 140 },
  { key: "body", label: "Recap", max: 3000, rows: 12 },
];

export function DraftView({ approval, editable, onEdits }: { approval: ApprovalRow; editable: boolean; onEdits: (e: Edits) => void }) {
  const d = approval.draft as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  if (approval.action_type === "exhibitor_follow_up_email")
    return <FollowUpDraft draft={d as { messages: FollowUpMessage[] }} editable={editable} onEdits={onEdits} />;
  if (approval.action_type === "schedule_change") {
    const c = d.change;
    return (
      <div className="stack">
        <section className="panel">
          <div className="panel-body">
            <div className="row" style={{ fontSize: 15, fontWeight: 600 }}>
              {c.from_room} <span className="faint small" style={{ fontWeight: 400 }}>({c.from_capacity} seats)</span>
              <ArrowRight size={15} className="faint" />
              {c.to_room} <span className="faint small" style={{ fontWeight: 400 }}>({c.to_capacity} seats)</span>
            </div>
            <dl className="kv" style={{ marginTop: 12 }}>
              <dt>Session</dt>
              <dd>{c.session_title}</dd>
              <dt>When</dt>
              <dd>{c.when}</dd>
              <dt>Demand</dt>
              <dd>
                {c.bookmarks} bookmarks for {c.from_capacity} seats
              </dd>
              <dt>Notified</dt>
              <dd>Everyone who bookmarked the session</dd>
            </dl>
          </div>
        </section>
        <TextBlockDraft block="announcement" value={d.announcement} fields={ANNOUNCE_FIELDS} editable={editable} onEdits={onEdits} />
      </div>
    );
  }
  if (approval.action_type === "session_summary_publish") {
    const f = d.facts ?? {};
    return (
      <div className="stack">
        <dl className="kv">
          {Object.entries(f)
            .filter(([, v]) => typeof v !== "object")
            .map(([k, v]) => (
              <div key={k} style={{ display: "contents" }}>
                <dt>{humanize(k)}</dt>
                <dd>{String(v)}</dd>
              </div>
            ))}
        </dl>
        <TextBlockDraft block="document" value={d.document} fields={DOC_FIELDS} editable={editable} onEdits={onEdits} />
      </div>
    );
  }
  return <pre className="code">{JSON.stringify(d, null, 2)}</pre>;
}

// ---------------------------------------------------------------------------------------------

function ActionsTable({ actions }: { actions: ActionRow[] }) {
  if (!actions.length) return <p className="faint">No outbound actions. They are created only after final approval.</p>;
  return (
    <div className="table-wrap panel">
      <table className="table">
        <thead>
          <tr>
            <th>Stage</th>
            <th>Channel</th>
            <th>Recipient</th>
            <th>Status</th>
            <th className="r">Attempts</th>
            <th>Detail</th>
          </tr>
        </thead>
        <tbody>
          {actions.slice(0, 60).map((a) => (
            <tr key={a.id}>
              <td className="num">{a.stage + 1}</td>
              <td className="nowrap">{CHANNEL[a.channel] ?? a.channel}</td>
              <td style={{ maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={a.recipient ?? undefined}>{a.recipient ?? a.subject ?? "—"}</td>
              <td><StatusBadge status={a.status} /></td>
              <td className="r num">{a.attempts}</td>
              <td className="small muted">
                {a.status === "sent"
                  ? `Sent ${fmtTime(a.sent_at)}`
                  : a.status === "retrying"
                    ? `Next try ${fmtRelative(a.next_attempt_at)}`
                    : (a.last_error ?? "")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {actions.length > 60 && <div className="panel-body faint small">Showing 60 of {actions.length}.</div>}
    </div>
  );
}

function Counts({ counts }: { counts: Record<string, number> }) {
  const order = ["sent", "queued", "retrying", "dead_letter", "cancelled"];
  const entries = order.filter((k) => counts[k]).map((k) => [k, counts[k]!] as const);
  if (!entries.length) return null;
  return (
    <div className="row-wrap">
      {entries.map(([k, n]) => (
        <span key={k} className="row" style={{ gap: 4 }}>
          <StatusBadge status={k} />
          <span className="num small">{n}</span>
        </span>
      ))}
    </div>
  );
}

export function WorkflowDrawer({ id, onClose, onChanged }: { id: string; onClose: () => void; onChanged?: () => void }) {
  const session = useRequiredSession();
  const [poll, setPoll] = useState(true);
  const wf = useApi<WorkflowDetail>(`/workflows/${id}`, { pollMs: poll ? 1500 : undefined });
  const [tab, setTab] = useState<"review" | "delivery" | "history">("review");
  const [edits, setEdits] = useState<Edits>(null);
  const [reason, setReason] = useState("");
  const [rejecting, setRejecting] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const w = wf.data;
  useEffect(() => {
    if (w) setPoll(LIVE.has(w.status));
  }, [w]);
  useEffect(() => {
    if (w && ["executing", "completed", "failed"].includes(w.status) && w.actions.length) setTab((t) => (t === "review" ? "delivery" : t));
  }, [w?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const pending = useMemo(() => w?.approvals.find((a) => a.status === "pending"), [w]);
  const latest = w?.approvals[w.approvals.length - 1];
  const mine =
    pending &&
    pending.required_role === session.role &&
    (session.role !== "exhibitor" || w?.owner_exhibitor_id === session.exhibitor_id);
  const editable = !!mine && pending.escalation_level === 0;
  const shown = pending ?? latest;

  async function decide(decision: "approve" | "reject") {
    setBusy(decision);
    setError(null);
    try {
      const d = await api<WorkflowDetail>(`/workflows/${id}/decision`, {
        body: { decision, reason: reason.trim() || null, edits: decision === "approve" ? edits : null },
      });
      wf.setData(d);
      setPoll(true);
      setRejecting(false);
      setEdits(null);
      onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function retry() {
    setBusy("retry");
    setError(null);
    try {
      const d = await api<WorkflowDetail>(`/workflows/${id}/retry`, { body: {} });
      wf.setData(d);
      setPoll(true);
      onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const out = (w?.output ?? {}) as Record<string, unknown>;
  const flags = (out.flags as string[] | undefined) ?? [];
  const dead = w?.action_counts.dead_letter ?? 0;

  const footer = w && (
    <>
      <ErrorNotice message={error} />
      <div className="spacer" />
      {mine && !rejecting && (
        <>
          <button className="btn btn-danger" onClick={() => setRejecting(true)} disabled={busy !== null}>
            Reject
          </button>
          <button className="btn btn-primary" onClick={() => decide("approve")} disabled={busy !== null}>
            {busy === "approve" && <Spinner />}
            {edits ? "Approve with edits" : "Approve"}
          </button>
        </>
      )}
      {mine && rejecting && (
        <>
          <input className="input" style={{ maxWidth: 360 }} placeholder="Reason (required)" value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
          <button className="btn" onClick={() => setRejecting(false)}>
            Cancel
          </button>
          <button className="btn btn-danger" onClick={() => decide("reject")} disabled={!reason.trim() || busy !== null}>
            {busy === "reject" && <Spinner />}
            Reject
          </button>
        </>
      )}
      {session.role === "organizer" && w.status === "failed" && dead > 0 && (
        <button className="btn" onClick={retry} disabled={busy !== null}>
          {busy === "retry" ? <Spinner /> : <RotateCcw size={14} />}
          Retry {dead} failed
        </button>
      )}
    </>
  );

  return (
    <Drawer
      title={w ? KIND_LABEL[w.kind] ?? humanize(w.kind) : "Workflow"}
      subtitle={w && <>Started {fmtDateTime(w.created_at)} · {w.id.slice(0, 8)}</>}
      onClose={onClose}
      footer={footer}
    >
      {!w && wf.loading && <Loading rows={6} />}
      <ErrorNotice message={wf.error?.message} />
      {w && (
        <>
          <dl className="kv" style={{ marginBottom: 18 }}>
            <dt>Status</dt>
            <dd className="row">
              <StatusBadge status={w.status} />
              {LIVE.has(w.status) && <Spinner size={12} />}
            </dd>
            <dt>Approval chain</dt>
            <dd>
              {chainLabel(w.approvals.length ? w.approvals.map((a) => a.required_role) : (out.approval_chain as string[]))}
              {pending && (
                <span className="faint">
                  {" "}
                  · waiting for {humanize(pending.required_role)}, expires {fmtRelative(pending.expires_at)}
                </span>
              )}
              {!pending && w.status === "awaiting_approval" && (
                // the next step belongs to another role, whose approval row this user cannot read
                <div className="small" style={{ color: "var(--warn)", marginTop: 2 }}>
                  {w.history.filter((h) => h.note?.includes("escalated")).slice(-1)[0]?.note ?? "Waiting for the next approver"}
                </div>
              )}
            </dd>
            {flags.length > 0 && (
              <>
                <dt>Flags</dt>
                <dd className="row-wrap">
                  {flags.map((f) => (
                    <Badge key={f} tone="warn">
                      {f}
                    </Badge>
                  ))}
                </dd>
              </>
            )}
            {out.used_llm !== undefined && (
              <>
                <dt>Drafted by</dt>
                <dd>{out.used_llm ? "Gemini (text only); scores, recipients and rooms decided in code" : "Template (LLM unavailable)"}</dd>
              </>
            )}
            {w.error && (
              <>
                <dt>Error</dt>
                <dd style={{ color: "var(--bad)" }}>{w.error}</dd>
              </>
            )}
            {w.actions.length > 0 && (
              <>
                <dt>Delivery</dt>
                <dd>
                  <Counts counts={w.action_counts} />
                </dd>
              </>
            )}
          </dl>

          {mine && pending.escalation_level > 0 && (
            <div className="notice notice-warn" style={{ marginBottom: 16 }}>
              <AlertTriangle size={15} />
              <span>
                Escalated: {w.history.filter((h) => h.note?.includes("escalated")).slice(-1)[0]?.note ?? "needs a second approval"}. Reviewers at
                this level can approve or reject but not edit.
              </span>
            </div>
          )}

          <Tabs
            value={tab}
            onChange={setTab}
            items={[
              { value: "review", label: mine ? "Review draft" : "Draft" },
              { value: "delivery", label: `Delivery${w.actions.length ? ` (${w.actions.length})` : ""}` },
              { value: "history", label: "History" },
            ]}
          />
          {tab === "review" &&
            (shown ? (
              <DraftView key={shown.id} approval={shown} editable={editable} onEdits={setEdits} />
            ) : (
              <p className="faint">{w.status === "completed" ? String(w.history.slice(-1)[0]?.note ?? "Nothing to do.") : "The agent is preparing a draft…"}</p>
            ))}
          {tab === "delivery" && <ActionsTable actions={w.actions} />}
          {tab === "history" && (
            <ol className="timeline">
              {w.history.map((h, i) => (
                <li key={i}>
                  <span className="faint num">{fmtDateTime(h.at)}</span>
                  <span>
                    {h.to && h.to !== h.from ? <StatusBadge status={h.to} /> : <Badge>Note</Badge>}{" "}
                    <span className="muted">
                      {h.actor_role ? humanize(h.actor_role) : ""}
                      {h.note ? ` · ${h.note}` : ""}
                    </span>
                  </span>
                </li>
              ))}
            </ol>
          )}
        </>
      )}
    </Drawer>
  );
}
