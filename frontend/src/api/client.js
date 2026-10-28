// Empty means same origin: the Vite dev server proxies /api, and in production
// FastAPI serves the built dashboard itself. Set VITE_API_BASE_URL only when the
// API lives on another origin.
export const API_BASE = (import.meta.env?.VITE_API_BASE_URL || "").replace(/\/$/, "");
const TOKEN_KEY = "testpilot-api-token";

export function getToken() {
  try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}

export function setToken(token) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch { /* storage unavailable; token is simply not remembered */ }
}

// FastAPI returns `detail` as a string or as a list of validation errors.
export function formatDetail(detail, fallback) {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail.map((item) => {
      const field = Array.isArray(item.loc) ? item.loc.filter((part) => part !== "body").join(".") : "";
      const message = String(item.msg || "invalid value").replace(/^Value error, /, "");
      return field ? `${field}: ${message}` : message;
    }).join("; ");
  }
  return fallback;
}

async function request(path, { method = "GET", body } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new Error("Could not reach the TestPilot API. Is the backend running?");
  }
  return response;
}

export async function apiJson(path, options, fallback = "Request could not be completed.") {
  const response = await request(path, options);
  let data = null;
  try { data = await response.json(); } catch { /* non-JSON body */ }
  if (!response.ok) throw new Error(formatDetail(data?.detail, `${fallback} (HTTP ${response.status})`));
  if (data === null) throw new Error("The API returned a response that was not JSON.");
  return data;
}

export async function apiBlobUrl(path) {
  const response = await request(path);
  if (!response.ok) throw new Error(`Artifact unavailable (HTTP ${response.status})`);
  return URL.createObjectURL(await response.blob());
}

// Open an API-rendered HTML page (such as a report) with the token attached,
// which a plain link cannot do when authentication is enabled.
export async function openHtmlPage(path) {
  const tab = window.open("about:blank", "_blank");
  try {
    const response = await request(path);
    if (!response.ok) throw new Error(`Page unavailable (HTTP ${response.status})`);
    const url = URL.createObjectURL(new Blob([await response.text()], { type: "text/html" }));
    if (tab) tab.location.href = url;
    else window.location.href = url;
  } catch (error) {
    if (tab) tab.close();
    throw error;
  }
}
