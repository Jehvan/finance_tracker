import { useState } from "react";
import { formatDate } from "./shared.jsx";
import { confirmPayment, dueLabel, markBillPaid } from "./recurringApi.js";
import { BANNER_DAYS } from "./reminders.js";
import { BillAmount, DateStub } from "./Subscriptions.jsx";

// "X payments due in the next 7 days", shown at the top of the Spending tab,
// with a one-tap "Mark paid" for the most urgent few.
const MAX_ROWS = 3;
export default function DueBanner({ authFetch, items, onPaid, onOpenBills }) {
  const [busyId, setBusyId] = useState(null);
  const [message, setMessage] = useState(null);
  const [error, setError] = useState(null);

  if (items.length === 0 && !message) return null;

  async function handleMarkPaid(item) {
    if (!confirmPayment(item, formatDate)) return;
    setBusyId(item.id);
    setError(null);
    setMessage(null);
    const result = await markBillPaid(authFetch, item);
    setBusyId(null);
    if (result.ok) {
      setMessage(`${item.name} logged in Spending. Next due ${formatDate(result.data.item.next_due_date)}.`);
    } else {
      setError(result.error);
    }
    onPaid(); // refresh reminders and spending either way (a 409 means it changed elsewhere)
  }

  const overdue = items.some((i) => i.status === "overdue");
  const n = items.length;
  const shown = items.slice(0, MAX_ROWS); // keep the Spending form within reach

  return (
    <section className={`due-banner ${overdue ? "has-overdue" : ""}`} aria-label="Upcoming bills">
      {n > 0 && (
        <>
          <div className="due-banner-head">
            <span className="due-banner-title">
              {n === 1 ? "1 payment" : `${n} payments`} due in the next {BANNER_DAYS} days
            </span>
            <button className="due-banner-link" onClick={onOpenBills}>
              {n > MAX_ROWS ? `See all ${n}` : "See all bills"}
            </button>
          </div>
          <ul className="due-banner-list">
            {shown.map((item) => (
              <li key={item.id} className="due-banner-row">
                <DateStub item={item} />
                <div className="due-banner-body">
                  <span className="bill-name">{item.name}</span>
                  <span className="due-banner-meta">
                    <BillAmount amount={item.amount} currency={item.currency} />
                    <span className={`bill-due status-${item.status}`}>{dueLabel(item)}</span>
                  </span>
                </div>
                <button className="bill-pay" onClick={() => handleMarkPaid(item)} disabled={busyId === item.id}>
                  Mark paid
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
      <div aria-live="polite">
        {message && <div className="bill-notice">{message}</div>}
        {error && <div className="form-error" role="alert">{error}</div>}
      </div>
    </section>
  );
}
