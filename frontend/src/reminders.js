// Bill reminders.
//
// The server decides each bill's status (overdue / due_soon / ok) in
// recurring.py, so the app, and later push notifications, agree on what is
// "due". This module turns the /recurring/upcoming response into reminders
// and hands them to every registered channel.
//
// Phase 1 (now): in-app only. App.jsx reads the reminders through
// useReminders() for the Spending-tab banner and the badge on the Bills tab.
//
// Phase 2 (not built yet): web push. Plan:
//   1. Backend: a push_subscriptions table (user_id, endpoint, keys), VAPID keys
//      in Render env vars, and POST/DELETE /api/push/subscription routes.
//   2. Backend: a daily job that, per user, selects active bills where
//      rc.due_status(...) is "due_soon" or "overdue" and sends one web push.
//      It must be triggered from outside (Render's free plan has no workers):
//      for example a Render Cron Job or the existing uptime pinger calling a
//      secret-protected endpoint.
//   3. Frontend: switch vite-plugin-pwa to `injectManifest` with a custom
//      service worker that handles `push` and `notificationclick` (open the
//      Bills tab), then register a channel here:
//
//        registerReminderChannel({
//          name: "push",
//          notify: (reminders) => { ...ask permission once, subscribe via
//            registration.pushManager and POST the subscription... },
//        });
//
// Nothing else in the app needs to change for phase 2.

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
const channels = [];

export function registerReminderChannel(channel) {
  if (!channels.some((c) => c.name === channel.name)) channels.push(channel);
}

function dispatch(reminders) {
  for (const channel of channels) {
    try {
      channel.notify(reminders);
    } catch {
      // a failing channel must never break the app
    }
  }
}

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
    dispatch(next);
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
