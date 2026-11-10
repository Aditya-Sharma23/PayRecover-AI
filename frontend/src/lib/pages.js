// The dashboard's pages, one per sidebar entry, chosen by the address's #hash.

export const PAGES = {
  dashboard: "Dashboard",
  check: "Check payments",
  disputes: "Disputes",
  timeline: "Timeline",
  recoveries: "Money recovered",
  more: "More tools",
};

// Older links point at a section inside a page (for example #scan or #settings); open its page.
// Ask PayRecover and Alerts live on the dashboard, so judges see them on the first screen.
const SECTION_PAGES = {
  payments: "check", scan: "check", report: "more", settings: "more", history: "more",
  assistant: "dashboard", alerts: "dashboard",
};

// The section to scroll to for a hash that names a section rather than a page, or null.
export function sectionFor(hash) {
  const key = String(hash || "").replace(/^#/, "");
  return key in SECTION_PAGES ? key : null;
}

export function pageFor(hash) {
  const key = String(hash || "").replace(/^#/, "");
  if (key in PAGES) return key;
  return SECTION_PAGES[key] || "dashboard";
}
