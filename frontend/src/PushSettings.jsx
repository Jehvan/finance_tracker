import { useEffect, useState } from "react";
import { disablePush, enablePush, pushState, sendTestPush } from "./push.js";

const STATE_TEXT = {
  checking: "Checking this device…",
  on: "On for this device. You'll get one notification on days a bill is due soon or overdue.",
  off: "Get a notification on days a bill is due soon or overdue.",
  denied: "Notifications are blocked for this app. Allow them in your phone's settings, then come back.",
  "needs-install": "On iPhone, add this app to your Home Screen first (Share, then Add to Home Screen) and open it from there.",
  unsupported: "This browser can't show notifications from the app.",
};

// "Reminders on this device" card at the bottom of the Bills tab.
export default function PushSettings({ authFetch }) {
  const [state, setState] = useState("checking");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    pushState().then((s) => alive && setState(s)).catch(() => alive && setState("unsupported"));
    return () => {
      alive = false;
    };
  }, []);

  async function run(action) {
    setBusy(true);
    setError(null);
    setMessage(null);
    const result = await action();
    setBusy(false);
    setState(await pushState().catch(() => "unsupported"));
    return result;
  }

  const turnOn = () =>
    run(() => enablePush(authFetch)).then((r) => (r.ok ? setMessage("Reminders are on.") : setError(r.error)));
  const turnOff = () =>
    run(() => disablePush(authFetch)).then((r) => (r.ok ? setMessage("Reminders are off for this device.") : setError(r.error)));
  const test = () =>
    run(() => sendTestPush(authFetch)).then((r) => {
      if (!r.ok) setError(r.error);
      else if (r.data.sent > 0) setMessage("Test sent. It should appear in a few seconds.");
      else setError("No device received it. Try turning reminders off and on again.");
    });

  return (
    <section className="push-card" aria-labelledby="push-card-title">
      <h3 className="section-title" id="push-card-title">Reminders on this device</h3>
      <p className="push-card-text">{STATE_TEXT[state]}</p>
      {(state === "off" || state === "on") && (
        <div className="push-card-actions">
          {state === "off" ? (
            <button className="bill-pay" onClick={turnOn} disabled={busy}>
              {busy ? "Turning on…" : "Turn on reminders"}
            </button>
          ) : (
            <>
              <button className="bill-secondary" onClick={test} disabled={busy}>Send a test</button>
              <button className="bill-secondary" onClick={turnOff} disabled={busy}>Turn off</button>
            </>
          )}
        </div>
      )}
      <div aria-live="polite">
        {message && <p className="push-card-note">{message}</p>}
        {error && <p className="push-card-error" role="alert">{error}</p>}
      </div>
    </section>
  );
}
