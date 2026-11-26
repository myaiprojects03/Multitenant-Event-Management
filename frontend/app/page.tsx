"use client";

import { ChevronRight, Lock, ShieldCheck, UserCheck } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { Badge, ErrorNotice, Loading, Spinner } from "@/components/ui";
import { initials } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { HOME } from "@/lib/nav";
import { useSession } from "@/lib/session";
import type { DemoPersona } from "@/lib/types";

const ROLE_ORDER = { attendee: 0, exhibitor: 1, organizer: 2 } as const;

export default function SignIn() {
  const router = useRouter();
  const { session, ready, signIn } = useSession();
  const personas = useApi<DemoPersona[]>("/auth/demo-users");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (ready && session) router.replace(HOME[session.role]);
  }, [ready, session, router]);

  const groups = useMemo(() => {
    const by = new Map<string, DemoPersona[]>();
    for (const p of personas.data ?? []) {
      const k = p.tenant_name;
      by.set(k, [...(by.get(k) ?? []), p]);
    }
    for (const list of by.values()) list.sort((a, b) => ROLE_ORDER[a.role] - ROLE_ORDER[b.role]);
    return [...by.entries()].sort(([a], [b]) => (a.startsWith("Northwind") ? -1 : b.startsWith("Northwind") ? 1 : a.localeCompare(b)));
  }, [personas.data]);

  async function choose(p: DemoPersona) {
    setBusy(`${p.email}|${p.event_id}`);
    setError(null);
    try {
      const s = await signIn(p.email, p.event_id);
      router.push(HOME[s.role]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(null);
    }
  }

  return (
    <div className="signin">
      <aside className="signin-aside">
        <div>
          <div className="brand-name">EventAI</div>
          <div className="faint small">Multi-tenant event platform</div>
        </div>
        <h1>One platform for every event, with AI that stays inside its lane.</h1>
        <p>
          Sign in as any of the demo users below. Each one gets a token scoped to a single tenant, event and
          role; the database enforces that scope on every query.
        </p>
        <ul className="signin-points">
          <li>
            <Lock size={16} className="faint" style={{ marginTop: 3 }} />
            <div>
              <strong>Isolation in the database</strong>
              Row-level security on every table. The assistant and BI copilot can only read what the signed-in
              user could read anyway.
            </div>
          </li>
          <li>
            <UserCheck size={16} className="faint" style={{ marginTop: 3 }} />
            <div>
              <strong>Consent before contact</strong>
              Exhibitors see anonymized prospects. Contact details appear only after an accepted meeting or a
              badge scan.
            </div>
          </li>
          <li>
            <ShieldCheck size={16} className="faint" style={{ marginTop: 3 }} />
            <div>
              <strong>Nothing sends without approval</strong>
              Agents draft follow-ups, recaps and room changes. A person approves before anything leaves the
              platform.
            </div>
          </li>
        </ul>
        <div className="spacer" />
        <p className="faint small" style={{ marginTop: 32 }}>
          Synthetic data only. Email and platform calls are simulated.
        </p>
      </aside>

      <main className="signin-main">
        <h2 style={{ fontSize: 17 }}>Choose a demo user</h2>
        <p className="muted" style={{ marginTop: 4, marginBottom: 20 }}>
          The suggested walkthrough is Sara (attendee), then Maya at MedScan (exhibitor), then Ali (organizer).
        </p>
        <ErrorNotice message={error ?? personas.error?.message} />
        {personas.loading && (
          <div className="panel">
            <Loading rows={5} />
          </div>
        )}
        <div className="stack" style={{ marginTop: error || personas.error ? 16 : 0 }}>
          {groups.map(([tenant, list]) => (
            <section className="panel" key={tenant}>
              <div className="panel-head">
                <div>
                  <h2>{tenant}</h2>
                  <p>Tenant</p>
                </div>
              </div>
              <div className="divided">
                {list.map((p) => (
                  <button key={`${p.email}|${p.event_id}`} className="persona" onClick={() => choose(p)} disabled={busy !== null}>
                    <div className="row" style={{ gap: 12, minWidth: 0 }}>
                      <span className="avatar">{initials(p.full_name)}</span>
                      <div style={{ minWidth: 0 }}>
                        <div className="row">
                          <span className="item-title">{p.full_name}</span>
                          <Badge tone={p.role === "organizer" ? "accent" : "neutral"}>
                            {p.role === "exhibitor" ? `Exhibitor · ${p.exhibitor_name}` : p.role[0]!.toUpperCase() + p.role.slice(1)}
                          </Badge>
                        </div>
                        <div className="item-sub">
                          {p.event_name}
                          {p.persona_label?.includes("(") && (
                            <span className="faint"> · {p.persona_label.slice(p.persona_label.indexOf("(") + 1, -1)}</span>
                          )}
                        </div>
                      </div>
                    </div>
                    {busy === `${p.email}|${p.event_id}` ? <Spinner /> : <ChevronRight size={16} className="faint" />}
                  </button>
                ))}
              </div>
            </section>
          ))}
        </div>
      </main>
    </div>
  );
}
