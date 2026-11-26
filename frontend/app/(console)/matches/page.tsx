"use client";

import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { RecList, type RecAction } from "@/components/RecList";
import { ErrorNotice, Loading, PageHeader, Panel, Spinner, Tabs } from "@/components/ui";
import { api } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import type { Me, RecommendationList, RecType } from "@/lib/types";

const TABS: { value: RecType; label: string }[] = [
  { value: "peers", label: "People" },
  { value: "sessions", label: "Sessions" },
  { value: "speakers", label: "Speakers" },
  { value: "exhibitors", label: "Exhibitors" },
];

const ACTIONS: Partial<Record<RecType, RecAction>> = {
  peers: {
    label: "Connect",
    done: "Request sent",
    run: (r) => api("/connections", { body: { user_id: r.data.user_id } }),
  },
  sessions: {
    label: "Save",
    done: "Bookmarked",
    run: (r) => api("/recommendations/feedback", { body: { target_type: "session", target_id: r.target_id, action: "save" } }),
  },
  speakers: {
    label: "Save",
    done: "Saved",
    run: (r) => api("/recommendations/feedback", { body: { target_type: "speaker", target_id: r.target_id, action: "save" } }),
  },
  exhibitors: {
    label: "Request meeting",
    done: "Requested",
    run: (r) => api("/meetings", { body: { exhibitor_id: r.target_id } }),
  },
};

function Consent() {
  const me = useApi<Me>("/me");
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function set(key: "networking_opt_in" | "exhibitor_matching_opt_in", value: boolean) {
    if (!me.data) return;
    setSaving(key);
    setError(null);
    try {
      await api("/me/consent", { method: "PUT", body: { [key]: value } });
      me.setData({ ...me.data, [key]: value });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(null);
    }
  }

  return (
    <Panel title="Your matching preferences" description="Two separate choices. Changes apply immediately.">
      {me.loading && !me.data ? (
        <div className="skeleton" style={{ width: "50%" }} />
      ) : (
        <div className="grid-2">
          <label className="switch">
            <input
              type="checkbox"
              checked={!!me.data?.networking_opt_in}
              disabled={saving !== null}
              onChange={(e) => set("networking_opt_in", e.target.checked)}
            />
            <span>
              <strong style={{ fontWeight: 600 }}>Networking</strong>
              <span className="hint" style={{ display: "block" }}>
                Other attendees can find you and send connection requests.
              </span>
            </span>
          </label>
          <label className="switch">
            <input
              type="checkbox"
              checked={!!me.data?.exhibitor_matching_opt_in}
              disabled={saving !== null}
              onChange={(e) => set("exhibitor_matching_opt_in", e.target.checked)}
            />
            <span>
              <strong style={{ fontWeight: 600 }}>Exhibitor matching</strong>
              <span className="hint" style={{ display: "block" }}>
                Exhibitors see an anonymized profile. Your name and email are shared only if you accept a meeting.
              </span>
            </span>
          </label>
        </div>
      )}
      <div style={{ marginTop: error ? 12 : 0 }}>
        <ErrorNotice message={error} />
      </div>
    </Panel>
  );
}

export default function MatchesPage() {
  const [tab, setTab] = useState<RecType>("peers");
  const [phrase, setPhrase] = useState(false);
  const [nonce, setNonce] = useState(0);
  const recs = useApi<RecommendationList>(`/recommendations?type=${tab}&limit=10&phrase=${phrase}&n=${nonce}`);

  return (
    <>
      <PageHeader
        title="Recommendations"
        description="Ranked from your interests, goals and bookmarks. Every match states the facts behind it."
      />
      <div className="stack">
        <Consent />
        <section className="panel">
          <div className="panel-head" style={{ paddingBottom: 0, borderBottom: 0 }}>
            <Tabs value={tab} onChange={setTab} items={TABS} />
            <div className="actions" style={{ marginBottom: 18 }}>
              <label className="check small muted" title="Uses Gemini to reword the reasons; the facts stay the same">
                <input type="checkbox" checked={phrase} onChange={(e) => setPhrase(e.target.checked)} />
                Reword reasons with AI
              </label>
              <button className="btn btn-sm" onClick={() => setNonce((n) => n + 1)} disabled={recs.loading}>
                {recs.loading ? <Spinner /> : <RefreshCw size={14} />}
                Refresh
              </button>
            </div>
          </div>
          <div style={{ borderTop: "1px solid var(--border)" }}>
            {recs.error && (
              <div className="panel-body">
                <ErrorNotice message={recs.error.message} />
              </div>
            )}
            {recs.loading && !recs.data && <Loading rows={4} />}
            {recs.data?.message && (
              <div className="panel-body">
                <div className="notice">{recs.data.message}</div>
              </div>
            )}
            {recs.data && !recs.data.message && (
              <RecList
                items={recs.data.items}
                primary={ACTIONS[tab]}
                onRemoved={(id) => recs.setData({ ...recs.data!, items: recs.data!.items.filter((i) => i.target_id !== id) })}
              />
            )}
          </div>
        </section>
      </div>
    </>
  );
}
