"use client";

import { LogOut } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useMemo, useState, type ReactNode } from "react";
import { initials } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { HOME, NAV } from "@/lib/nav";
import { useRequiredSession, useSession } from "@/lib/session";
import type { DemoPersona, QueueRow } from "@/lib/types";
import { Badge } from "./ui";

export function Shell({ children }: { children: ReactNode }) {
  const session = useRequiredSession();
  const { signIn, signOut } = useSession();
  const router = useRouter();
  const path = usePathname();
  const personas = useApi<DemoPersona[]>("/auth/demo-users");
  const canApprove = session.role !== "attendee";
  const queue = useApi<QueueRow[]>(canApprove ? "/approvals" : null, { pollMs: 15000 });
  const [switchError, setSwitchError] = useState<string | null>(null);

  const events = useMemo(() => {
    const m = new Map<string, string>();
    for (const p of personas.data ?? []) if (p.email === session.email) m.set(p.event_id, p.event_name);
    return [...m.entries()];
  }, [personas.data, session.email]);

  const byEvent = useMemo(() => {
    const list =
      personas.data ??
      ([{ email: session.email, event_id: session.event_id, full_name: session.full_name, role: session.role, event_name: session.event_name, exhibitor_name: session.exhibitor_name }] as DemoPersona[]);
    const m = new Map<string, DemoPersona[]>();
    for (const p of list) m.set(p.event_name, [...(m.get(p.event_name) ?? []), p]);
    return [...m.entries()].sort(([a], [b]) => (a === session.event_name ? -1 : b === session.event_name ? 1 : a.localeCompare(b)));
  }, [personas.data, session]);

  async function switchTo(email: string, eventId?: string) {
    setSwitchError(null);
    try {
      const s = await signIn(email, eventId);
      router.push(HOME[s.role]);
    } catch (e) {
      setSwitchError(e instanceof Error ? e.message : String(e));
    }
  }

  const roleLabel =
    session.role === "exhibitor" ? `Exhibitor · ${session.exhibitor_name}` : session.role[0]!.toUpperCase() + session.role.slice(1);

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-name">EventAI</div>
          <div className="brand-sub">{session.tenant_name}</div>
        </div>
        <nav className="nav" aria-label="Main">
          {NAV[session.role].map((g) => (
            <div key={g.label} style={{ display: "contents" }}>
              <div className="nav-label">{g.label}</div>
              {g.items.map((it) => {
                const Icon = it.icon;
                const n = it.badge === "approvals" ? queue.data?.length ?? 0 : 0;
                return (
                  <Link key={it.href} href={it.href} aria-current={path.startsWith(it.href) ? "page" : undefined}>
                    <Icon size={16} strokeWidth={1.75} />
                    {it.label}
                    {n > 0 && <span className="nav-count">{n}</span>}
                  </Link>
                );
              })}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="row" style={{ gap: 10 }}>
            <span className="avatar">{initials(session.full_name)}</span>
            <div style={{ minWidth: 0, lineHeight: 1.35 }}>
              <div style={{ color: "var(--text)", fontWeight: 600, fontSize: 13 }}>{session.full_name}</div>
              <div className="faint small" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                {session.email}
              </div>
            </div>
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="crumbs">
            <span>{session.tenant_name}</span>
            <span className="sep">/</span>
            {events.length > 1 ? (
              <select
                className="select"
                style={{ width: "auto", minHeight: 30, padding: "2px 8px", fontWeight: 600 }}
                value={session.event_id}
                onChange={(e) => switchTo(session.email, e.target.value)}
                aria-label="Event"
              >
                {events.map(([id, name]) => (
                  <option key={id} value={id}>
                    {name}
                  </option>
                ))}
              </select>
            ) : (
              <strong>{session.event_name}</strong>
            )}
            <Badge tone={session.role === "organizer" ? "accent" : "neutral"}>{roleLabel}</Badge>
          </div>
          <div className="topbar-right">
            {switchError && <span className="small" style={{ color: "var(--bad)" }}>{switchError}</span>}
            <label className="faint small" htmlFor="switch-user">
              View as
            </label>
            <select
              id="switch-user"
              className="select"
              style={{ width: 230, minHeight: 30, padding: "2px 8px" }}
              value={`${session.email}|${session.event_id}`}
              onChange={(e) => {
                const p = personas.data?.find((x) => `${x.email}|${x.event_id}` === e.target.value);
                if (p) void switchTo(p.email, p.event_id);
              }}
            >
              {byEvent.map(([eventName, list]) => (
                <optgroup key={eventName} label={eventName}>
                  {list.map((p) => (
                    <option key={`${p.email}|${p.event_id}`} value={`${p.email}|${p.event_id}`}>
                      {p.full_name} · {p.role === "exhibitor" ? p.exhibitor_name : p.role}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => {
                signOut();
                router.push("/");
              }}
              title="Sign out"
            >
              <LogOut size={15} />
            </button>
          </div>
        </header>
        <div className="content">{children}</div>
      </div>
    </div>
  );
}
