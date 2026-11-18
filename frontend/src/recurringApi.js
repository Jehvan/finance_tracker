import { API_BASE, errorMessageFrom } from "./shared.jsx";

// Thin wrappers around /api/recurring. Each returns { ok, data } or { ok: false, error }.

async function call(authFetch, path, options, fallbackError) {
  try {
    const res = await authFetch(`${API_BASE}/recurring${path}`, options);
    if (!res.ok) return { ok: false, status: res.status, error: await errorMessageFrom(res, fallbackError) };
    return { ok: true, data: res.status === 204 ? null : await res.json() };
  } catch {
    return { ok: false, error: "Can't reach the server. Nothing was changed." };
  }
}

const json = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const listBills = (authFetch) => call(authFetch, "", undefined, "Couldn't load your bills.");
export const billSummary = (authFetch) => call(authFetch, "/summary", undefined, "Couldn't load bill totals.");
export const upcomingBills = (authFetch, days) =>
  call(authFetch, `/upcoming?days=${encodeURIComponent(days)}`, undefined, "Couldn't load upcoming bills.");
export const createBill = (authFetch, body) => call(authFetch, "", json("POST", body), "Couldn't save that bill.");
export const updateBill = (authFetch, id, body) =>
  call(authFetch, `/${encodeURIComponent(id)}`, json("PATCH", body), "Couldn't save that bill.");
export const deleteBill = (authFetch, id) =>
  call(authFetch, `/${encodeURIComponent(id)}`, { method: "DELETE" }, "Couldn't delete that bill.");

// Sends the due date being paid, so a double tap or a retry can't log the
// payment twice — the server answers 409 if that date was already paid.
export const markBillPaid = (authFetch, item) =>
  call(
    authFetch,
    `/${encodeURIComponent(item.id)}/mark-paid`,
    json("POST", { due_date: item.next_due_date }),
    "Couldn't mark that bill paid."
  );

// Paying a bill that isn't due yet needs a confirm. Right after a payment the
// next due date is a full cycle away, so this also stops an accidental double
// tap from paying the *next* period (the server can't tell that apart from a
// deliberate early payment, so the guard lives here).
export function confirmPayment(item, formatDate) {
  if (item.status === "overdue" || item.status === "due_soon") return true;
  return window.confirm(`${item.name} isn't due until ${formatDate(item.next_due_date)}. Mark it paid now?`);
}

export const CYCLE_LABEL = {
  weekly: "Weekly",
  monthly: "Monthly",
  quarterly: "Every 3 months",
  yearly: "Yearly",
};

export const CATEGORY_LABEL = {
  subscription: "Subscription",
  utility: "Utility",
  rent: "Rent",
  phone: "Phone",
  other: "Other",
};

export function dueLabel(item) {
  if (!item.is_active) return "Paused";
  const d = item.days_until_due;
  if (d < -1) return `${-d} days overdue`;
  if (d === -1) return "1 day overdue";
  if (d === 0) return "Due today";
  if (d === 1) return "Due tomorrow";
  return `In ${d} days`;
}
