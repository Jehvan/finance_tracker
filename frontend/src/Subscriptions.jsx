import { useCallback, useEffect, useRef, useState } from "react";
import { Money, formatDate, localDateString, parseISODate } from "./shared.jsx";
import PushSettings from "./PushSettings.jsx";
import {
  CATEGORY_LABEL,
  CYCLE_LABEL,
  billSummary,
  confirmPayment,
  createBill,
  deleteBill,
  dueLabel,
  listBills,
  markBillPaid,
  updateBill,
} from "./recurringApi.js";

const EMPTY_FORM = {
  name: "",
  amount: "",
  currency: "MKD",
  billing_cycle: "monthly",
  next_due_date: "",
  category: "subscription",
  payment_method_label: "",
  reminder_days_before: "7",
  notes: "",
};

function formFromItem(item) {
  return {
    name: item.name,
    amount: String(item.amount),
    currency: item.currency,
    billing_cycle: item.billing_cycle,
    next_due_date: item.next_due_date,
    category: item.category,
    payment_method_label: item.payment_method_label ?? "",
    reminder_days_before: String(item.reminder_days_before),
    notes: item.notes ?? "",
  };
}

export function BillAmount({ amount, currency }) {
  return (
    <>
      <Money value={amount} /> <span className="bill-currency">{currency}</span>
    </>
  );
}

// The calendar tile on the left of each bill: the due date at a glance,
// tinted red when overdue and amber inside the reminder window.
export function DateStub({ item }) {
  const d = parseISODate(item.next_due_date);
  return (
    <div className={`date-stub status-${item.status}`} aria-hidden="true">
      <span className="date-stub-day">{d.getDate()}</span>
      <span className="date-stub-month">{d.toLocaleDateString(undefined, { month: "short" })}</span>
    </div>
  );
}

function BillForm({ initial, isEdit, onSave, onCancel, onDelete }) {
  const [form, setForm] = useState(initial);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (field) => (e) => setForm((f) => ({ ...f, [field]: e.target.value }));

  async function handleSubmit(e) {
    e.preventDefault();
    // Mirrors the server rule; the server enforces it regardless.
    if ((form.payment_method_label.match(/\d/g) || []).length > 4) {
      setError('Payment method is a label like "Visa" or "cash". Use at most the last 4 digits of a card.');
      return;
    }
    setBusy(true);
    setError(null);
    const result = await onSave({ ...form, reminder_days_before: Number(form.reminder_days_before) });
    setBusy(false);
    if (!result.ok) setError(result.error);
  }

  return (
    <form className="quick-add bill-form" onSubmit={handleSubmit}>
      <input type="text" placeholder="Name, e.g. Netflix" value={form.name} onChange={set("name")}
        maxLength={100} required aria-label="Name" />
      <div className="amount-field">
        <input type="number" inputMode="decimal" step="0.01" min="0.01" placeholder="Amount"
          value={form.amount} onChange={set("amount")} required aria-label="Amount" />
        <select value={form.currency} onChange={set("currency")} aria-label="Currency">
          <option value="MKD">MKD</option>
          <option value="EUR">EUR</option>
        </select>
      </div>
      <label className="bill-field">
        <span>Repeats</span>
        <select value={form.billing_cycle} onChange={set("billing_cycle")}>
          {Object.entries(CYCLE_LABEL).map(([v, label]) => (
            <option key={v} value={v}>{label}</option>
          ))}
        </select>
      </label>
      <label className="bill-field">
        <span>Next due</span>
        <input type="date" value={form.next_due_date} onChange={set("next_due_date")} required
          min="2000-01-01" max="2100-12-31" />
      </label>
      <label className="bill-field">
        <span>Category</span>
        <select value={form.category} onChange={set("category")}>
          {Object.entries(CATEGORY_LABEL).map(([v, label]) => (
            <option key={v} value={v}>{label}</option>
          ))}
        </select>
      </label>
      <label className="bill-field">
        <span>Remind me</span>
        <select value={form.reminder_days_before} onChange={set("reminder_days_before")}>
          {[0, 1, 2, 3, 5, 7, 14].map((n) => (
            <option key={n} value={n}>{n === 0 ? "On the day" : `${n} day${n > 1 ? "s" : ""} before`}</option>
          ))}
          {![0, 1, 2, 3, 5, 7, 14].includes(Number(form.reminder_days_before)) && (
            <option value={form.reminder_days_before}>{form.reminder_days_before} days before</option>
          )}
        </select>
      </label>
      <input type="text" placeholder='Paid with (optional), e.g. "Visa" or "cash"'
        value={form.payment_method_label} onChange={set("payment_method_label")} maxLength={40}
        autoComplete="off" aria-label="Paid with" />
      <input type="text" placeholder="Notes (optional)" value={form.notes} onChange={set("notes")}
        maxLength={500} aria-label="Notes" />
      {error && <div className="form-error" role="alert">{error}</div>}
      <div className="bill-form-actions">
        <button type="submit" disabled={busy}>{busy ? "Saving…" : isEdit ? "Save changes" : "Add bill"}</button>
        <button type="button" className="bill-secondary" onClick={onCancel}>Cancel</button>
      </div>
      {onDelete && (
        <button type="button" className="bill-delete" onClick={onDelete} disabled={busy}>
          Delete this bill
        </button>
      )}
    </form>
  );
}

function BillRow({ item, busy, onMarkPaid, onEdit, onToggle }) {
  const meta = [CYCLE_LABEL[item.billing_cycle], item.payment_method_label].filter(Boolean).join(", ");
  return (
    <li className={`bill-row status-${item.status}`}>
      <DateStub item={item} />
      <div className="bill-body">
        <div className="bill-top">
          <span className="bill-name">{item.name}</span>
          <span className="bill-amount"><BillAmount amount={item.amount} currency={item.currency} /></span>
        </div>
        <div className="bill-meta">
          <span>{meta}</span>
          <span className={`bill-due status-${item.status}`}>
            <span className="visually-hidden">Next due {formatDate(item.next_due_date)}. </span>
            {dueLabel(item)}
          </span>
        </div>
        {item.notes && <div className="bill-notes">{item.notes}</div>}
        <div className="bill-actions">
          {item.is_active && (
            <button className="bill-pay" onClick={() => onMarkPaid(item)} disabled={busy}>
              Mark paid
            </button>
          )}
          <button className="bill-secondary" onClick={() => onEdit(item)} disabled={busy}>Edit</button>
          <button className="bill-secondary" onClick={() => onToggle(item)} disabled={busy}>
            {item.is_active ? "Pause" : "Resume"}
          </button>
        </div>
      </div>
    </li>
  );
}

export default function SubscriptionsTab({ authFetch, onChanged }) {
  const [items, setItems] = useState([]);
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [editor, setEditor] = useState(null); // null | { item: null } (add) | { item } (edit)
  const [busyId, setBusyId] = useState(null);
  const formRef = useRef(null);

  const load = useCallback(async () => {
    const [list, sum] = await Promise.all([listBills(authFetch), billSummary(authFetch)]);
    if (list.ok && sum.ok) {
      setItems(list.data);
      setSummary(sum.data);
      setLoadError(null);
    } else {
      setLoadError((list.ok ? sum : list).error);
    }
    setLoading(false);
  }, [authFetch]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (editor) formRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [editor]);

  async function afterChange(message) {
    setNotice(message);
    await load();
    onChanged?.();
  }

  async function runAction(item, fn, successMessage) {
    setBusyId(item.id);
    setActionError(null);
    setNotice(null);
    const result = await fn();
    setBusyId(null);
    if (!result.ok) {
      setActionError(result.error);
      if (result.status === 409 || result.status === 404) load(); // someone else changed it; show the latest
      return;
    }
    await afterChange(typeof successMessage === "function" ? successMessage(result.data) : successMessage);
  }

  const handleMarkPaid = (item) => {
    if (!confirmPayment(item, formatDate)) return;
    runAction(item, () => markBillPaid(authFetch, item), (data) =>
      `${item.name} logged in Spending. Next due ${formatDate(data.item.next_due_date)}.`);
  };

  const handleToggle = (item) =>
    runAction(item, () => updateBill(authFetch, item.id, { is_active: !item.is_active }),
      item.is_active ? `${item.name} paused.` : `${item.name} resumed.`);

  const handleDelete = (item) => {
    if (!window.confirm(`Delete ${item.name}? Payments already logged in Spending are kept.`)) return;
    setEditor(null);
    runAction(item, () => deleteBill(authFetch, item.id), `${item.name} deleted.`);
  };

  async function handleSave(body) {
    const editing = editor?.item;
    const result = editing ? await updateBill(authFetch, editing.id, body) : await createBill(authFetch, body);
    if (result.ok) {
      setEditor(null);
      await afterChange(editing ? `${result.data.name} saved.` : `${result.data.name} added.`);
    }
    return result;
  }

  const active = items.filter((i) => i.is_active);
  const paused = items.filter((i) => !i.is_active);
  const eur = summary?.by_currency.find((c) => c.currency === "EUR");
  const rowProps = {
    onMarkPaid: handleMarkPaid,
    onEdit: (item) => setEditor({ item }),
    onToggle: handleToggle,
  };

  return (
    <div className="tab-content">
      {loadError ? (
        <div className="empty-state">
          {loadError}
          <div>
            <button className="retry-btn" onClick={load}>Retry</button>
          </div>
        </div>
      ) : (
        <>
          {summary && summary.active_count > 0 && (
            <>
              <section className="hero" aria-label="Bill totals">
                <div className="hero-label">Bills per month</div>
                <div className="hero-value">
                  <Money value={summary.monthly_total} decimals={0} />
                  <span className="hero-unit">MKD</span>
                </div>
                <dl className="hero-split">
                  <div>
                    <dt>Per year</dt>
                    <dd><Money value={summary.yearly_total} decimals={0} /></dd>
                  </div>
                  {summary.most_expensive && (
                    <div>
                      <dt>Most expensive</dt>
                      <dd className="bill-hero-top">
                        <span className="bill-hero-top-name">{summary.most_expensive.name}</span>
                        <span className="bill-hero-top-cost">
                          <Money value={summary.most_expensive.monthly_in_base} decimals={0} /> a month
                        </span>
                      </dd>
                    </div>
                  )}
                </dl>
              </section>
              <p className="bills-footnote">
                {eur
                  ? <>Includes <Money value={eur.monthly} /> EUR a month, counted at {summary.eur_to_mkd_rate} MKD per euro.</>
                  : "Paused bills aren't counted."}
              </p>
            </>
          )}

          <div ref={formRef} className="bill-form-anchor">
            {editor ? (
              <BillForm
                key={editor.item?.id ?? "new"}
                initial={editor.item ? formFromItem(editor.item) : { ...EMPTY_FORM, next_due_date: localDateString() }}
                isEdit={Boolean(editor.item)}
                onSave={handleSave}
                onCancel={() => setEditor(null)}
                onDelete={editor.item ? () => handleDelete(editor.item) : undefined}
              />
            ) : (
              <button className="bill-add" onClick={() => { setNotice(null); setEditor({ item: null }); }}>
                Add a bill
              </button>
            )}
          </div>

          <div aria-live="polite">
            {notice && <div className="bill-notice">{notice}</div>}
            {actionError && <div className="form-error" role="alert">{actionError}</div>}
          </div>

          {loading ? (
            <div className="empty-state">Loading…</div>
          ) : items.length === 0 ? (
            <div className="empty-state">
              No bills yet. Add rent, your phone plan or a streaming subscription to see what's due and
              what it all costs per month.
            </div>
          ) : (
            <>
              {active.length > 0 && (
                <ul className="bill-list" aria-label="Active bills">
                  {active.map((item) => (
                    <BillRow key={item.id} item={item} busy={busyId === item.id} {...rowProps} />
                  ))}
                </ul>
              )}
              {paused.length > 0 && (
                <>
                  <h3 className="section-title">Paused</h3>
                  <ul className="bill-list" aria-label="Paused bills">
                    {paused.map((item) => (
                      <BillRow key={item.id} item={item} busy={busyId === item.id} {...rowProps} />
                    ))}
                  </ul>
                </>
              )}
            </>
          )}
          <PushSettings authFetch={authFetch} />
        </>
      )}
    </div>
  );
}
