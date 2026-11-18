// Helpers shared by every tab. The frontend only ever talks to our Flask API
// (with the user's login token); it never receives Supabase credentials.

// In production (Render), set VITE_API_BASE to your backend's Render URL,
// e.g. https://finance-tracker-api.onrender.com/api
// Locally, it falls back to whatever host you loaded the page from,
// so it works whether you open it via localhost or your LAN IP.
export const API_BASE = (
  import.meta.env.VITE_API_BASE || `http://${window.location.hostname}:5000/api`
).replace(/\/+$/, "");

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

export function makeAuthFetch(token, onUnauthorized) {
  return async function authFetch(url, options = {}) {
    const res = await fetch(url, {
      ...options,
      headers: { ...options.headers, Authorization: `Bearer ${token}` },
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
