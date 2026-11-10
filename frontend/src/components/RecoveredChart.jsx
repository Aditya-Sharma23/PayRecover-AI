import { useEffect, useMemo, useRef, useState } from "react";
import { RECOVERY_LABELS, recoveredOverTime } from "../lib/payment.js";

// Money recovered over time: one series, so the title names it and there is no legend. A step line, because the
// total only changes when a recovery lands. Hovering shows a crosshair and the recovery under it.
const HEIGHT = 200;
const PAD = { top: 14, right: 16, bottom: 26, left: 56 };

function niceMax(value) {
  if (value <= 0) return 10;
  const step = 10 ** Math.floor(Math.log10(value));
  return Math.ceil((value * 1.1) / step) * step;
}

const day = (time) => new Date(time).toLocaleDateString(undefined, { day: "numeric", month: "short" });
const stamp = (time) => new Date(time).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

export default function RecoveredChart({ records }) {
  const points = useMemo(() => recoveredOverTime(records), [records]);
  const box = useRef(null);
  const [width, setWidth] = useState(800);
  const [hover, setHover] = useState(null);

  useEffect(() => {
    if (!box.current) return undefined;
    const watch = new ResizeObserver(([entry]) => setWidth(Math.max(320, Math.round(entry.contentRect.width))));
    watch.observe(box.current);
    return () => watch.disconnect();
  }, []);

  if (points.length === 0) return null;
  const currency = points[0].currency;
  const last = points[points.length - 1];
  // A single recovery still gets a visible span: start the axis a day earlier.
  const start = points.length > 1 ? points[0].time : points[0].time - 86400000;
  const end = last.time === start ? start + 86400000 : last.time;
  const top = niceMax(last.total);
  const plotW = width - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const x = (time) => PAD.left + ((time - start) / (end - start)) * plotW;
  const y = (value) => PAD.top + plotH - (value / top) * plotH;

  // Step path: flat until each recovery, then a jump to the new total.
  let line = `M${x(start)},${y(0)}`;
  for (const point of points) line += ` H${x(point.time)} V${y(point.total)}`;
  line += ` H${x(end)}`;
  const area = `${line} V${y(0)} H${x(start)} Z`;
  const ticks = [0, top / 2, top];

  function move(event) {
    const bounds = event.currentTarget.getBoundingClientRect();
    const at = start + ((event.clientX - bounds.left) * (width / bounds.width) - PAD.left) / plotW * (end - start);
    let nearest = points[0];
    for (const point of points) if (Math.abs(point.time - at) < Math.abs(nearest.time - at)) nearest = point;
    setHover(nearest);
  }

  const summary = `Money recovered rose to ${last.total.toFixed(2)} ${currency} across ${points.length} recoveries, `
    + `from ${day(points[0].time)} to ${day(last.time)}.`;
  const tipLeft = hover ? Math.min(Math.max(x(hover.time), PAD.left + 80), width - 90) : 0;

  return <figure className="recovered-chart" ref={box}>
    <figcaption>
      <span>Money recovered over time</span>
      <strong>{last.total.toFixed(2)} {currency}</strong>
    </figcaption>
    <div className="recovered-plot">
      <svg width={width} height={HEIGHT} role="img" aria-label={summary}>
        {ticks.map((tick) => <g key={tick}>
          <line className="chart-grid" x1={PAD.left} x2={width - PAD.right} y1={y(tick)} y2={y(tick)} />
          <text className="chart-tick" x={PAD.left - 8} y={y(tick)} dy="0.32em" textAnchor="end">{tick.toFixed(0)}</text>
        </g>)}
        <text className="chart-tick" x={PAD.left} y={HEIGHT - 6}>{day(start)}</text>
        <text className="chart-tick" x={width - PAD.right} y={HEIGHT - 6} textAnchor="end">{day(end)}</text>
        <path className="chart-area" d={area} />
        <path className="chart-line" d={line} />
        <circle className="chart-dot" cx={x(last.time)} cy={y(last.total)} r="4" />
        {hover && <g aria-hidden="true">
          <line className="chart-crosshair" x1={x(hover.time)} x2={x(hover.time)} y1={PAD.top} y2={PAD.top + plotH} />
          <circle className="chart-dot active" cx={x(hover.time)} cy={y(hover.total)} r="5" />
        </g>}
        {/* The hit area covers the whole plot, so the pointer never has to land on the thin line. */}
        <rect x={PAD.left} y={PAD.top} width={plotW} height={plotH} fill="transparent"
          onMouseMove={move} onMouseLeave={() => setHover(null)} />
      </svg>
      {hover && <div className="chart-tooltip" style={{ left: tipLeft, top: Math.max(y(hover.total) - 70, 0) }} role="status">
        <small>{stamp(hover.time)}</small>
        <span>+{hover.amount.toFixed(2)} {currency} · {RECOVERY_LABELS[hover.action] || hover.action}</span>
        <strong>Total {hover.total.toFixed(2)} {currency}</strong>
      </div>}
    </div>
  </figure>;
}
