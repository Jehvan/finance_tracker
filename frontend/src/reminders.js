// Bill reminders.
//
// The server decides each bill's status (overdue / due_soon / ok) in
// recurring.py, so the app and push notifications agree on what is "due".
// This module turns the /recurring/upcoming response into in-app reminders.
//
// In-app (phase 1): App.jsx reads the reminders through useReminders() for
// the Spending-tab banner and the badge on the Bills tab.
//
// Push (phase 2): sent by the server, not from here. A daily GitHub Actions
// run (.github/workflows/bill-reminders.yml) calls /api/push/run-daily, which
// builds each user's digest with recurring.build_digest(). push.js turns
// notifications on/off for this device; sw.js shows them.

import { useCallback, useEffect, useRef, useState } from "react";
import { upcomingBills } from "./recurringApi.js";

export const BANNER_DAYS = 7;
// The longest reminder window a bill can have (MAX_REMINDER_DAYS on the server),
// so one request covers every bill's reminder window.
export const LOOKAHEAD_DAYS = 60;
const REFRESH_ON_RETURN_MS = 5 * 60 * 1000;

export function computeReminders(items) {
  const needsAttention = items.filter((i) => i.status === "overdue" || i.status === "due_soon");
  return {
    // Banner: everything due within the next week, overdue included.
    dueThisWeek: items.filter((i) => i.days_until_due <= BANNER_DAYS),
    // Badge: bills that are overdue or inside their own reminder window.
    needsAttention,
    overdueCount: needsAttention.filter((i) => i.status === "overdue").length,
  };
}

const EMPTY = computeReminders([]);

export function useReminders(authFetch) {
  const [reminders, setReminders] = useState(EMPTY);
  const lastLoaded = useRef(0);

  const refresh = useCallback(async () => {
    if (!authFetch) return;
    lastLoaded.current = Date.now();
    const result = await upcomingBills(authFetch, LOOKAHEAD_DAYS);
    if (!result.ok) return; // best effort: keep the last known reminders
    const next = computeReminders(result.data.items);
    setReminders(next);
  }, [authFetch]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // An installed PWA can stay open in the background for days. Refresh when
  // it comes back to the foreground, at most every few minutes.
  useEffect(() => {
    function onVisible() {
      if (document.visibilityState === "visible" && Date.now() - lastLoaded.current > REFRESH_ON_RETURN_MS) {
        refresh();
      }
    }
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [refresh]);

  return { reminders, refresh };
}
