"use client";

import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Badge, Empty, ErrorNotice, Loading, PageHeader, Panel, Spinner } from "@/components/ui";
import { fmtInt, fmtMs, fmtPct, fmtTime, humanize } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { TelemetrySummary, UsageRow } from "@/lib/types";

const WINDOWS = [
  { m: 60, label: "Last hour" },
  { m: 1440, label: "Last 24 hours" },
  { m: 10080, label: "Last 7 days" },
];

function Kpis({ s }: { s: TelemetrySummary }) {
  const items = [
    { label: "LLM calls", value: fmtInt(s.calls), sub: `${s.errors} errors` },
    { label: "p50 latency", value: fmtMs(s.p50_ms), sub: `p95 ${fmtMs(s.p95_ms)}` },
    { label: "Cached vs uncached p50", value: fmtMs(s.p50_cached_ms), sub: `uncached ${fmtMs(s.p50_uncached_ms)}` },
    { label: "Cache hit rate", value: fmtPct(s.cache_hit_rate), sub: "Responses served from Redis" },
    { label: "Tokens", value: fmtInt(s.prompt_tokens + s.completion_tokens), sub: `${fmtInt(s.avg_tokens_per_call)} avg per uncached call` },
    { label: "Est. cost", value: `$${s.cost_usd.toFixed(4)}`, sub: "Free tier: billed $0" },
  ];
  return (
    <section className="panel">
      <div className="kpis">
        {items.map((k) => (
          <div className="kpi" key={k.label}>
            <div className="kpi-label">{k.label}</div>
            <div className="kpi-value">{k.value}</div>
            <div className="kpi-sub">{k.sub}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

export default function TelemetryPage() {
  const [minutes, setMinutes] = useState(1440);
  const [nonce, setNonce] = useState(0);
  const sum = useApi<TelemetrySummary>(`/telemetry/summary?minutes=${minutes}&n=${nonce}`);
  const recent = useApi<UsageRow[]>(`/telemetry/recent?limit=40&n=${nonce}`, { pollMs: 10000 });
  const s = sum.data;

  return (
    <>
      <PageHeader
        title="Telemetry"
        description="Every LLM call is logged with route, model, latency, tokens and cache status. Prompts and answers are never stored."
        actions={
          <>
            <select className="select" style={{ width: "auto" }} value={minutes} onChange={(e) => setMinutes(Number(e.target.value))}>
              {WINDOWS.map((w) => (
                <option key={w.m} value={w.m}>
                  {w.label}
                </option>
              ))}
            </select>
            <button className="btn" onClick={() => setNonce((n) => n + 1)} disabled={sum.loading}>
              {sum.loading ? <Spinner /> : <RefreshCw size={14} />}
              Refresh
            </button>
          </>
        }
      />
      <ErrorNotice message={sum.error?.message} />
      {!s && sum.loading && (
        <div className="panel">
          <Loading rows={3} />
        </div>
      )}
      {s && (
        <div className="stack">
          <Kpis s={s} />
          <div className="grid-2">
            <Panel title="Latency by route" description="Median and 95th percentile per call">
              {s.by_route.length === 0 ? (
                <Empty title="No calls in this window" />
              ) : (
                <ResponsiveContainer width="100%" height={Math.max(200, s.by_route.length * 44 + 40)}>
                  <BarChart data={s.by_route.map((r) => ({ ...r, name: humanize(r.route_type) }))} layout="vertical" margin={{ left: 8, right: 16 }}>
                    <CartesianGrid stroke="#ecece8" horizontal={false} />
                    <XAxis type="number" tick={{ fontSize: 12, fill: "#86867f" }} tickLine={false} axisLine={false} tickFormatter={(v) => fmtMs(Number(v))} />
                    <YAxis type="category" dataKey="name" width={130} tick={{ fontSize: 12, fill: "#53534e" }} tickLine={false} axisLine={false} />
                    <Tooltip contentStyle={{ border: "1px solid #e3e3df", borderRadius: 6, fontSize: 12.5 }} formatter={(v) => fmtMs(Number(v))} />
                    <Legend iconType="square" iconSize={10} wrapperStyle={{ fontSize: 12 }} />
                    <Bar dataKey="p50_ms" name="p50" fill="#0f5e57" barSize={10} isAnimationActive={false} />
                    <Bar dataKey="p95_ms" name="p95" fill="#b9c4c1" barSize={10} isAnimationActive={false} />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </Panel>
            <Panel flush title="By route">
              <table className="table">
                <thead>
                  <tr>
                    <th>Route</th>
                    <th className="r">Calls</th>
                    <th className="r">Cache hits</th>
                    <th className="r">Errors</th>
                    <th className="r">Avg tokens</th>
                  </tr>
                </thead>
                <tbody>
                  {s.by_route.map((r) => (
                    <tr key={r.route_type}>
                      <td>{humanize(r.route_type)}</td>
                      <td className="r num">{r.calls}</td>
                      <td className="r num">{r.cache_hits}</td>
                      <td className="r num">{r.errors}</td>
                      <td className="r num">{fmtInt(r.avg_tokens)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {Object.keys(s.by_model).length > 0 && (
                <div className="panel-body" style={{ borderTop: "1px solid var(--border)" }}>
                  <div className="label" style={{ marginBottom: 6 }}>
                    Models
                  </div>
                  <div className="row-wrap">
                    {Object.entries(s.by_model).map(([m, n]) => (
                      <span key={m} className="chip mono">
                        {m} · {n}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </Panel>
          </div>
          <Panel flush title="Recent requests">
            {recent.data?.length === 0 && <Empty title="No requests yet" />}
            {recent.data && recent.data.length > 0 && (
              <div className="table-wrap" style={{ maxHeight: 460, overflowY: "auto" }}>
                <table className="table">
                  <thead>
                    <tr>
                      <th>Time</th>
                      <th>Route</th>
                      <th>Role</th>
                      <th>Model</th>
                      <th className="r">Latency</th>
                      <th className="r">Tokens in / out</th>
                      <th>Result</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recent.data.map((r, i) => (
                      <tr key={`${r.request_id}-${i}`}>
                        <td className="num faint nowrap">{fmtTime(r.created_at)}</td>
                        <td>{humanize(r.route_type)}</td>
                        <td className="muted">{r.user_role ? humanize(r.user_role) : "—"}</td>
                        <td className="mono small muted">{r.model ?? "—"}</td>
                        <td className="r num">{fmtMs(r.latency_ms)}</td>
                        <td className="r num">
                          {r.prompt_tokens} / {r.completion_tokens}
                        </td>
                        <td>
                          {r.status !== "ok" ? (
                            <Badge tone="bad" title={r.error ?? undefined}>
                              {humanize(r.status)}
                            </Badge>
                          ) : r.cache_hit ? (
                            <Badge>Cache hit</Badge>
                          ) : (
                            <Badge tone="ok">OK</Badge>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>
      )}
    </>
  );
}
