// Shimmering placeholders shown while data loads, in the shape of what will appear, instead of "Loading…".

export function SkeletonText({ width = "60%", tall = false }) {
  return <span className={`skeleton ${tall ? "skeleton-tall" : "skeleton-text"}`} style={{ width }} aria-hidden="true" />;
}

// A table-shaped placeholder: a header row and a few body rows.
export function GridSkeleton({ rows = 4, label = "Loading" }) {
  return <div className="grid-skeleton" role="status" aria-label={label}>
    <div className="grid-skeleton-row head">{[18, 22, 30, 14, 16].map((width, index) => <SkeletonText key={index} width={`${width}%`} />)}</div>
    {Array.from({ length: rows }, (_, row) => <div className="grid-skeleton-row" key={row}>
      {[18, 22, 30, 14, 16].map((width, index) => <SkeletonText key={index} width={`${width - (row % 2) * 4}%`} />)}
    </div>)}
  </div>;
}

// A block placeholder for a panel whose content is still loading (the timeline, a chart).
export function PanelSkeleton({ height = 220, label = "Loading" }) {
  return <div className="skeleton skeleton-block" style={{ height }} role="status" aria-label={label} />;
}
