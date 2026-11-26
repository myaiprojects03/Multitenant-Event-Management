"use client";

import { AlertTriangle, Loader2, X } from "lucide-react";
import { useEffect, type ReactNode } from "react";

type Tone = "neutral" | "ok" | "warn" | "bad" | "accent";

export function Badge({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`badge${tone === "neutral" ? "" : ` badge-${tone}`}`} title={title}>
      {children}
    </span>
  );
}

const STATUS: Record<string, [string, Tone]> = {
  // workflows
  pending: ["Queued", "neutral"],
  running: ["Drafting", "neutral"],
  awaiting_approval: ["Awaiting approval", "warn"],
  approved: ["Approved", "accent"],
  executing: ["Sending", "accent"],
  completed: ["Completed", "ok"],
  failed: ["Failed", "bad"],
  rejected: ["Rejected", "neutral"],
  expired: ["Expired", "neutral"],
  // outbound actions
  queued: ["Queued", "neutral"],
  retrying: ["Retrying", "warn"],
  sent: ["Sent", "ok"],
  dead_letter: ["Dead letter", "bad"],
  cancelled: ["Cancelled", "neutral"],
  // connections / meetings
  accepted: ["Accepted", "ok"],
  declined: ["Declined", "neutral"],
  // analytics
  ok: ["Answered", "ok"],
  blocked: ["Blocked", "bad"],
  unanswerable: ["Unanswerable", "warn"],
  error: ["Error", "bad"],
};

export function StatusBadge({ status, request }: { status: string; request?: boolean }) {
  // connection / meeting requests: "pending" means waiting for the other person
  if (request && status === "pending") return <Badge tone="warn">Pending</Badge>;
  const [label, tone] = STATUS[status] ?? [status.replace(/_/g, " "), "neutral" as Tone];
  return <Badge tone={tone}>{label}</Badge>;
}

export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="actions">{actions}</div>}
    </div>
  );
}

export function Panel({
  title,
  description,
  actions,
  children,
  flush,
}: {
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
}) {
  return (
    <section className="panel">
      {(title || actions) && (
        <div className="panel-head">
          <div>
            {title && <h2>{title}</h2>}
            {description && <p>{description}</p>}
          </div>
          {actions && <div className="actions">{actions}</div>}
        </div>
      )}
      {flush ? children : <div className="panel-body">{children}</div>}
    </section>
  );
}

export function Spinner({ size = 14 }: { size?: number }) {
  return <Loader2 size={size} className="spin" aria-label="Loading" />;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      {children}
    </div>
  );
}

export function ErrorNotice({ message }: { message: string | null | undefined }) {
  if (!message) return null;
  return (
    <div className="notice notice-bad" role="alert">
      <AlertTriangle size={15} />
      <span>{message}</span>
    </div>
  );
}

export function Loading({ rows = 3 }: { rows?: number }) {
  return (
    <div className="panel-body" style={{ display: "grid", gap: 10 }}>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton" style={{ width: `${90 - i * 15}%` }} />
      ))}
    </div>
  );
}

export function Tabs<T extends string>({
  value,
  onChange,
  items,
}: {
  value: T;
  onChange: (v: T) => void;
  items: { value: T; label: ReactNode }[];
}) {
  return (
    <div className="tabs" role="tablist">
      {items.map((it) => (
        <button key={it.value} role="tab" aria-selected={value === it.value} onClick={() => onChange(it.value)}>
          {it.label}
        </button>
      ))}
    </div>
  );
}

export function Drawer({
  title,
  subtitle,
  onClose,
  children,
  footer,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true">
        <div className="drawer-head">
          <div style={{ minWidth: 0 }}>
            <h2>{title}</h2>
            {subtitle && <div className="faint small">{subtitle}</div>}
          </div>
          <button className="btn btn-ghost btn-sm" style={{ marginLeft: "auto" }} onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>
        <div className="drawer-body">{children}</div>
        {footer && <div className="drawer-foot">{footer}</div>}
      </aside>
    </>
  );
}

export function Meta({ children }: { children: ReactNode }) {
  return <span className="faint small">{children}</span>;
}
