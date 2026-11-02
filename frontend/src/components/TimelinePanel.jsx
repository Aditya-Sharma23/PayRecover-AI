import { useEffect, useMemo, useState } from "react";
import { BryntumScheduler } from "@bryntum/scheduler-react";
import "@bryntum/scheduler/fontawesome/css/fontawesome.css";
import "@bryntum/scheduler/fontawesome/css/solid.css";
import "@bryntum/scheduler/scheduler.css";
import "@bryntum/scheduler/stockholm-dark.css";
import { apiJson } from "../api/client.js";
import { TIMELINE_COLORS, escapeHtml, timelineEvents } from "../lib/assist.js";

const REFRESH_MS = 60000;

// Bryntum Scheduler: one row per store plus PayPal disputes; scans, recoveries and dispute deadlines as blocks.
export default function TimelinePanel({ refreshKey }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    const load = () => apiJson("/api/timeline", {}, "The timeline could not be loaded.")
      .then((value) => { setData(value); setError(null); }).catch((reason) => setError(reason.message));
    load();
    const timer = setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [refreshKey]);

  const events = useMemo(() => (data ? timelineEvents(data.events) : []), [data]);
  const config = useMemo(() => ({
    readOnly: true,
    viewPreset: "dayAndWeek",
    rowHeight: 46,
    barMargin: 6,
    columns: [{ text: "Store", field: "name", width: 170 }],
    features: {
      eventTooltip: {
        template: ({ eventRecord }) => `<strong>${escapeHtml(eventRecord.name)}</strong><br>${escapeHtml(eventRecord.detail)}`,
      },
    },
  }), []);

  return <section className="panel" id="timeline" aria-labelledby="timeline-heading">
    <div className="section-heading">
      <div><p className="eyebrow">PayRecover · Bryntum Scheduler</p><h2 id="timeline-heading">Timeline</h2></div>
      <div className="timeline-legend" aria-label="Legend">
        {Object.entries(TIMELINE_COLORS).map(([tone, [, label]]) => <span key={tone}><i className={`dot ${tone}`} />{label}</span>)}
      </div>
    </div>
    <p className="hint">Every scan, recovery and PayPal dispute over the last three days and the next ten. A dispute runs from the day it opened to PayPal&apos;s response deadline, and turns red when fewer than three days are left.</p>
    {error && <p className="notice notice-error" role="alert">{error}</p>}
    {data?.note && <p className="notice" role="status">{data.note}</p>}
    {data && <div className="timeline-view">
      <BryntumScheduler {...config} startDate={new Date(data.start)} endDate={new Date(data.end)}
        resources={data.resources} events={events} />
    </div>}
  </section>;
}
