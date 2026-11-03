import { useEffect, useRef, useState } from "react";

// Renders its children only once it scrolls near the viewport, so a heavy panel is not fetched until it is wanted.
export default function OnVisible({ fallback, children }) {
  const holder = useRef(null);
  const [seen, setSeen] = useState(typeof IntersectionObserver === "undefined");

  useEffect(() => {
    if (seen || !holder.current) return undefined;
    const watcher = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) setSeen(true);
    }, { rootMargin: "400px" });
    watcher.observe(holder.current);
    return () => watcher.disconnect();
  }, [seen]);

  return seen ? children : <div ref={holder}>{fallback}</div>;
}
