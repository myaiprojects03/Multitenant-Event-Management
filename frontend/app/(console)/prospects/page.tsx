"use client";

import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { RecList } from "@/components/RecList";
import { ErrorNotice, Loading, PageHeader, Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { useRequiredSession } from "@/lib/session";
import type { RecommendationList } from "@/lib/types";

export default function ProspectsPage() {
  const session = useRequiredSession();
  const [nonce, setNonce] = useState(0);
  const recs = useApi<RecommendationList>(`/recommendations?type=prospects&limit=15&phrase=false&n=${nonce}`);

  return (
    <>
      <PageHeader
        title="Prospects"
        description={
          <>
            Attendees who opted in to exhibitor matching and fit what {session.exhibitor_name} offers. Profiles stay
            anonymous: a name and email are shared only if the attendee accepts your meeting request.
          </>
        }
        actions={
          <button className="btn" onClick={() => setNonce((n) => n + 1)} disabled={recs.loading}>
            {recs.loading ? <Spinner /> : <RefreshCw size={14} />}
            Refresh
          </button>
        }
      />
      <section className="panel">
        {recs.error && (
          <div className="panel-body">
            <ErrorNotice message={recs.error.message} />
          </div>
        )}
        {recs.loading && !recs.data && <Loading rows={5} />}
        {recs.data?.message && (
          <div className="panel-body">
            <div className="notice">{recs.data.message}</div>
          </div>
        )}
        {recs.data && !recs.data.message && (
          <RecList
            items={recs.data.items}
            primary={{
              label: "Request meeting",
              done: "Requested",
              run: (r) => api("/meetings", { body: { profile_id: r.target_id } }),
            }}
            extra={(r) =>
              Array.isArray(r.data.interests) && (
                <div className="row-wrap" style={{ marginTop: 8 }}>
                  {(r.data.interests as string[]).map((i) => (
                    <span key={i} className="chip">
                      {i}
                    </span>
                  ))}
                </div>
              )
            }
            onRemoved={(id) => recs.setData({ ...recs.data!, items: recs.data!.items.filter((i) => i.target_id !== id) })}
          />
        )}
      </section>
    </>
  );
}
