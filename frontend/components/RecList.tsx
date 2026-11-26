"use client";

import { ChevronDown, ChevronRight, X } from "lucide-react";
import { useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import type { Recommendation } from "@/lib/types";
import { Badge, Empty } from "./ui";

export interface RecAction {
  label: string;
  done: string;
  run: (r: Recommendation) => Promise<unknown>;
}

function Row({
  rec,
  primary,
  onDismiss,
  extra,
}: {
  rec: Recommendation;
  primary?: RecAction;
  onDismiss: () => void;
  extra?: (r: Recommendation) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<"idle" | "busy" | "done">("idle");
  const [error, setError] = useState<string | null>(null);

  async function act() {
    if (!primary) return;
    setState("busy");
    setError(null);
    try {
      await primary.run(rec);
      setState("done");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setState("idle");
    }
  }

  function toggle() {
    if (!open) void api("/recommendations/feedback", { body: { target_type: rec.target_type, target_id: rec.target_id, action: "click" } }).catch(() => {});
    setOpen(!open);
  }

  return (
    <div className="item">
      <div className="row" style={{ alignItems: "flex-start", gap: 16 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row">
            <span className="item-title">{rec.title}</span>
            {rec.category && <Badge>{rec.category}</Badge>}
            <span className="faint small num">score {Math.round(rec.score * 100)}</span>
          </div>
          <div className="item-sub">{rec.subtitle}</div>
          <p style={{ marginTop: 6 }}>{rec.reason}</p>
          {extra?.(rec)}
          {rec.facts.length > 1 && (
            <button className="btn btn-ghost btn-sm" style={{ marginTop: 6, marginLeft: -10 }} onClick={toggle}>
              {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              Why this match
            </button>
          )}
          {open && (
            <ul className="facts">
              {rec.facts.map((f) => (
                <li key={f}>{f}</li>
              ))}
            </ul>
          )}
          {error && (
            <p className="small" style={{ color: "var(--bad)", marginTop: 6 }}>
              {error}
            </p>
          )}
        </div>
        <div className="row" style={{ flex: "none" }}>
          {primary &&
            (state === "done" ? (
              <Badge tone="ok">{primary.done}</Badge>
            ) : (
              <button className="btn btn-sm" onClick={act} disabled={state === "busy"}>
                {primary.label}
              </button>
            ))}
          <button className="btn btn-ghost btn-sm" onClick={onDismiss} title="Not relevant: hide it and show fewer like it">
            <X size={14} />
            Dismiss
          </button>
        </div>
      </div>
    </div>
  );
}

export function RecList({
  items,
  primary,
  extra,
  onRemoved,
}: {
  items: Recommendation[];
  primary?: RecAction;
  extra?: (r: Recommendation) => ReactNode;
  onRemoved: (id: string) => void;
}) {
  if (items.length === 0) return <Empty title="No recommendations right now">Dismissed items stay hidden.</Empty>;
  return (
    <div className="list">
      {items.map((r) => (
        <Row
          key={r.target_id}
          rec={r}
          primary={primary}
          extra={extra}
          onDismiss={() => {
            onRemoved(r.target_id);
            void api("/recommendations/feedback", {
              body: { target_type: r.target_type, target_id: r.target_id, action: "dismiss" },
            }).catch(() => {});
          }}
        />
      ))}
    </div>
  );
}
