// The dashboard's pages, one per sidebar entry, chosen by the address's #hash.

export const PAGES = {
  dashboard: "Dashboard",
  check: "Check payments",
  assistant: "Ask PayRecover",
  disputes: "Disputes",
  timeline: "Timeline",
  alerts: "Alerts",
  recoveries: "Money recovered",
  more: "More tools",
};

// Older links point at a section inside a page (for example #scan or #settings); open its page.
const SECTION_PAGES = { payments: "check", scan: "check", report: "more", settings: "more", history: "more" };

export function pageFor(hash) {
  const key = String(hash || "").replace(/^#/, "");
  if (key in PAGES) return key;
  return SECTION_PAGES[key] || "dashboard";
}
