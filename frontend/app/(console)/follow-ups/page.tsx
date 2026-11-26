"use client";

import { Play } from "lucide-react";
import { useState } from "react";
import { ErrorNotice, PageHeader, Panel, Spinner } from "@/components/ui";
import { WorkflowDrawer } from "@/components/Workflow";
import { WorkflowTable } from "@/components/WorkflowTable";
import { api } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { useRequiredSession } from "@/lib/session";
import type { WorkflowRow } from "@/lib/types";

const TIERS = ["A", "B", "C", "D"] as const;

export default function FollowUpsPage() {
  const session = useRequiredSession();
  const list = useApi<WorkflowRow[]>("/workflows?limit=25", { pollMs: 4000 });
  const [tiers, setTiers] = useState<string[]>(["A", "B"]);
  const [max, setMax] = useState(5);
  const [flaky, setFlaky] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ workflow_id: string }>("/workflows/trigger", {
        body: {
          kind: "lead_follow_up",
          params: { tiers, max_messages: max, ...(flaky ? { simulate: { fail_times: 1 } } : {}) },
        },
      });
      await list.reload();
      setOpen(r.workflow_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Lead follow-ups"
        description={`The Lead Qualification Agent scores ${session.exhibitor_name}'s leads in code, then drafts a personal email for each selected lead. You review every message before anything is sent.`}
      />
      <div className="stack">
        <Panel title="Draft follow-ups" description="Leads already contacted are skipped. More than 20 recipients also needs an organizer.">
          <div className="row-wrap" style={{ gap: 24, alignItems: "flex-end" }}>
            <div className="field">
              <label>Lead tiers</label>
              <div className="row" style={{ gap: 14, minHeight: 34 }}>
                {TIERS.map((t) => (
                  <label key={t} className="check">
                    <input
                      type="checkbox"
                      checked={tiers.includes(t)}
                      onChange={(e) => setTiers(e.target.checked ? [...tiers, t].sort() : tiers.filter((x) => x !== t))}
                    />
                    Tier {t}
                  </label>
                ))}
              </div>
            </div>
            <div className="field" style={{ width: 140 }}>
              <label htmlFor="max">Max messages</label>
              <input id="max" className="input" type="number" min={1} max={50} value={max} onChange={(e) => setMax(Math.max(1, Math.min(50, Number(e.target.value) || 1)))} />
            </div>
            <label className="check small muted" style={{ minHeight: 34 }} title="Demo: the simulated email provider fails the first attempt, so you can watch the retry">
              <input type="checkbox" checked={flaky} onChange={(e) => setFlaky(e.target.checked)} />
              Simulate a flaky email provider
            </label>
            <div className="spacer" />
            <button className="btn btn-primary" onClick={start} disabled={busy || tiers.length === 0}>
              {busy ? <Spinner /> : <Play size={14} />}
              Run agent
            </button>
          </div>
          {error && (
            <div style={{ marginTop: 12 }}>
              <ErrorNotice message={error} />
            </div>
          )}
        </Panel>
        <Panel flush title="Recent workflows">
          <WorkflowTable q={list} onOpen={setOpen} />
        </Panel>
      </div>
      {open && <WorkflowDrawer id={open} onClose={() => setOpen(null)} onChanged={list.reload} />}
    </>
  );
}
