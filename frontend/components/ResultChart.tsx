"use client";

import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fmtCell, fmtDay, humanize } from "@/lib/format";
import type { Chart } from "@/lib/types";

// one accent, then neutral steps: series are told apart by tone, not by a rainbow
const SERIES = ["#0f5e57", "#7a8f8b", "#b9c4c1"];
const AXIS = { fontSize: 12, fill: "#86867f" };

function label(v: unknown): string {
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}/.test(v)) return fmtDay(v);
  const s = String(v ?? "");
  return s.length > 22 ? s.slice(0, 21) + "…" : s;
}

export function ResultChart({ chart, rows }: { chart: Chart; rows: Record<string, unknown>[] }) {
  if (chart.type === "kpi" && rows[0]) {
    return (
      <div style={{ padding: "8px 0" }}>
        {chart.y.map((k) => (
          <div key={k}>
            <div className="kpi-label">{humanize(k)}</div>
            <div className="kpi-value" style={{ fontSize: 32 }}>
              {fmtCell(rows[0]![k])}
            </div>
          </div>
        ))}
      </div>
    );
  }
  if ((chart.type !== "bar" && chart.type !== "line") || !chart.x || !rows.length) return null;
  const data = rows.slice(0, 40).map((r) => ({ ...r, __x: label(r[chart.x!]) }));
  const horizontal = chart.type === "bar" && data.length > 8;
  const tooltip = (
    <Tooltip
      cursor={{ fill: "rgba(15,94,87,0.06)" }}
      contentStyle={{ border: "1px solid #e3e3df", borderRadius: 6, fontSize: 12.5, boxShadow: "none" }}
      formatter={(v, n) => [fmtCell(v), humanize(String(n))]}
    />
  );
  const height = horizontal ? Math.max(220, data.length * 26 + 40) : 260;
  return (
    <ResponsiveContainer width="100%" height={height}>
      {chart.type === "line" ? (
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="#ecece8" vertical={false} />
          <XAxis dataKey="__x" tick={AXIS} tickLine={false} axisLine={{ stroke: "#e3e3df" }} />
          <YAxis tick={AXIS} tickLine={false} axisLine={false} width={48} />
          {tooltip}
          {chart.y.map((k, i) => (
            <Line key={k} dataKey={k} stroke={SERIES[i % SERIES.length]} strokeWidth={2} dot={false} isAnimationActive={false} />
          ))}
        </LineChart>
      ) : horizontal ? (
        <BarChart data={data} layout="vertical" margin={{ top: 0, right: 16, bottom: 0, left: 8 }}>
          <CartesianGrid stroke="#ecece8" horizontal={false} />
          <XAxis type="number" tick={AXIS} tickLine={false} axisLine={false} />
          <YAxis type="category" dataKey="__x" tick={AXIS} tickLine={false} axisLine={false} width={170} />
          {tooltip}
          {chart.y.map((k, i) => (
            <Bar key={k} dataKey={k} fill={SERIES[i % SERIES.length]} radius={[0, 3, 3, 0]} barSize={14} isAnimationActive={false} />
          ))}
        </BarChart>
      ) : (
        <BarChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="#ecece8" vertical={false} />
          <XAxis dataKey="__x" tick={AXIS} tickLine={false} axisLine={{ stroke: "#e3e3df" }} />
          <YAxis tick={AXIS} tickLine={false} axisLine={false} width={48} />
          {tooltip}
          {chart.y.map((k, i) => (
            <Bar key={k} dataKey={k} fill={SERIES[i % SERIES.length]} radius={[3, 3, 0, 0]} maxBarSize={44} isAnimationActive={false} />
          ))}
        </BarChart>
      )}
    </ResponsiveContainer>
  );
}
