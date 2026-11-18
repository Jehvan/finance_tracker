// Bill-reminder push notifications on this device (phase 2).
//
// The server decides what to send and when: a daily GitHub Actions run calls
// /api/push/run-daily, which sends each subscribed device one digest of bills
// that are overdue or inside their reminder window. This module only turns
// notifications on or off for the current browser.

import { API_BASE } from "./shared.jsx";

export function isStandalone() {
  return window.matchMedia?.("(display-mode: standalone)").matches || window.navigator.standalone === true;
}

function isIOS() {
  return /iPhone|iPad|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

// "unsupported" | "needs-install" (iPhone: push only works from the Home Screen app)
// | "denied" | "off" | "on"
export async function pushState() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    return isIOS() && !isStandalone() ? "needs-install" : "unsupported";
  }
  if (Notification.permission === "denied") return "denied";
  let reg;
  try {
    reg = await swReady();
  } catch {
    return "unsupported";
  }
  const sub = await reg.pushManager.getSubscription();
  return sub && Notification.permission === "granted" ? "on" : "off";
}

// serviceWorker.ready never settles if the worker failed to register.
function swReady(ms = 5000) {
  return Promise.race([
    navigator.serviceWorker.ready,
    new Promise((_, reject) => setTimeout(() => reject(new Error("service worker not ready")), ms)),
  ]);
}

function keyToBytes(base64url) {
  const pad = "=".repeat((4 - (base64url.length % 4)) % 4);
  const raw = atob((base64url + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

async function jsonOrError(res, fallback) {
  if (res.ok) return { ok: true, data: res.status === 204 ? null : await res.json() };
  try {
    return { ok: false, error: (await res.json()).error || fallback };
  } catch {
    return { ok: false, error: fallback };
  }
}

// Must be called from a tap (browsers only show the permission prompt then).
export async function enablePush(authFetch) {
  try {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      return { ok: false, error: "Notifications weren't allowed. You can change this in your phone's settings." };
    }
    const config = await jsonOrError(await authFetch(`${API_BASE}/push/config`), "Couldn't reach the server.");
    if (!config.ok) return config;
    if (!config.data.enabled) return { ok: false, error: "Reminders aren't set up on the server yet." };

    const reg = await swReady();
    let sub = await reg.pushManager.getSubscription();
    const serverKey = keyToBytes(config.data.public_key);
    if (sub) {
      // Re-subscribe if the server's key changed since this device subscribed.
      const current = new Uint8Array(sub.options.applicationServerKey || []);
      if (current.length && current.join() !== serverKey.join()) {
        await sub.unsubscribe();
        sub = null;
      }
    }
    sub = sub || (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: serverKey }));

    const saved = await authFetch(`${API_BASE}/push/subscription`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sub.toJSON()),
    });
    return jsonOrError(saved, "Couldn't turn on reminders.");
  } catch {
    return { ok: false, error: "Couldn't turn on reminders on this device." };
  }
}

export async function disablePush(authFetch) {
  try {
    const reg = await swReady();
    const sub = await reg.pushManager.getSubscription();
    if (!sub) return { ok: true };
    const endpoint = sub.endpoint;
    await sub.unsubscribe();
    const res = await authFetch(`${API_BASE}/push/subscription`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint }),
    });
    return jsonOrError(res, "Turned off here, but couldn't tell the server.");
  } catch {
    return { ok: false, error: "Couldn't turn off reminders." };
  }
}

export async function sendTestPush(authFetch) {
  try {
    const res = await authFetch(`${API_BASE}/push/test`, { method: "POST" });
    return jsonOrError(res, "Couldn't send a test notification.");
  } catch {
    return { ok: false, error: "Can't reach the server." };
  }
}
