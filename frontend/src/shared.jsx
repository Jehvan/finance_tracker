// Helpers shared by every tab. The frontend only ever talks to our Flask API;
// it never receives Supabase credentials.

// The API is served from this site's own origin under /api: in production a
// Render rewrite proxies /api/* to the Flask service, and locally Vite's dev
// server does the same (vite.config.js). Same-origin is what lets the login
// cookie be HttpOnly + SameSite=Strict (Safari blocks third-party cookies).
//
// VITE_API_BASE may only override this with a same-site path (e.g. "/api").
// A full URL to another host (the old setup) would make the cookie
// third-party: login "works", the browser drops the cookie, and you get
// signed straight back out. So absolute URLs are ignored.
const configuredBase = import.meta.env.VITE_API_BASE || "";
export const API_BASE = (configuredBase.startsWith("/") ? configuredBase : "/api").replace(/\/+$/, "");

// Sent on every API call; the backend refuses cookie-authenticated writes
// without it (CSRF protection).
export const CSRF_HEADERS = { "X-Requested-With": "fetch" };

// The denar is pegged to the euro by the National Bank of the Republic of
// North Macedonia at a fixed rate (has held ~61.5 since 1997), so a static
// conversion is appropriate here rather than fetching a live FX rate.
// The backend uses the same rate (recurring.py) for bill totals.
export const EUR_TO_MKD_RATE = 61.5;

// All date/month logic uses the device's local time, not UTC — otherwise a
// transaction added in the evening (US timezones) or just after midnight
// (timezones ahead of UTC) can land on the wrong day/month.
export function localDateString(d = new Date()) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

// "2027-01-31" -> local Date (new Date("2027-01-31") would parse as UTC).
export function parseISODate(s) {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y, m - 1, d);
}

export function formatDate(s) {
  return parseISODate(s).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function Money({ value, decimals = 2 }) {
  const n = Number(value ?? 0);
  return (
    <span>{n.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}</span>
  );
}

// The session lives in an HttpOnly cookie the browser attaches by itself;
// page JavaScript never sees the token.
export function makeAuthFetch(onUnauthorized) {
  return async function authFetch(url, options = {}) {
    const res = await fetch(url, {
      ...options,
      credentials: "same-origin",
      headers: { ...options.headers, ...CSRF_HEADERS },
    });
    if (res.status === 401) {
      onUnauthorized();
      throw new Error("unauthorized");
    }
    return res;
  };
}

export async function errorMessageFrom(res, fallback) {
  try {
    const data = await res.json();
    return data.error || fallback;
  } catch {
    return fallback;
  }
}
