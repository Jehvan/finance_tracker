import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import {
  Tooltip,
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
} from "recharts";
import {
  API_BASE,
  CSRF_HEADERS,
  EUR_TO_MKD_RATE,
  Money,
  errorMessageFrom,
  localDateString,
  makeAuthFetch,
} from "./shared.jsx";
import SubscriptionsTab from "./Subscriptions.jsx";
import DueBanner from "./DueBanner.jsx";
import { useReminders } from "./reminders.js";

// Recharts writes these as SVG attributes, where CSS variables aren't
// reliable, so the chart's palette is mirrored here from index.css.
const CHART = { bar: "#F2789F", grid: "#3A2E46", muted: "#A597B2", text: "#F3EDF5", card: "#271D31" };

// Older builds kept the login token here. It now lives only in an HttpOnly
// cookie; this key is just cleared on startup so no stale token lingers.
const LEGACY_TOKEN_KEY = "finance_tracker_token";

function currentMonthString() {
  return localDateString().slice(0, 7);
}

function shiftMonth(month, delta) {
  const [y, m] = month.split("-").map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function formatMonth(month) {
  const [y, m] = month.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

function LoginForm({ onLogin }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/login`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...CSRF_HEADERS },
        body: JSON.stringify({ username, password }),
      });
      if (res.status === 429) {
        setError(await errorMessageFrom(res, "Too many failed sign-in attempts. Try again later."));
        return;
      }
      if (!res.ok) {
        setError("Invalid username or password.");
        return;
      }
      // The password was right; check the browser actually kept the session
      // cookie before showing the app, instead of bouncing back to sign-in.
      const check = await fetch(`${API_BASE}/session`, { credentials: "same-origin", headers: CSRF_HEADERS });
      if (!check.ok) {
        setError(
          "Signed in, but this browser didn't keep the session. The site must reach the API on its own address (/api)."
        );
        return;
      }
      onLogin();
    } catch (e) {
      setError("Can't reach the server.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="app app-login">
      <header className="app-header">
        <h1 className="wordmark">Finance</h1>
      </header>
      <main className="tab-content">
        <p className="login-intro">Sign in to see your spending and holdings.</p>
        <form className="quick-add" onSubmit={handleSubmit}>
          <input
            type="text"
            placeholder="Username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            required
          />
          <input
            type="password"
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
          <button type="submit" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
        {error && <div className="form-error">{error}</div>}
      </main>
    </div>
  );
}

// Custom dropdown instead of <datalist> — native datalist support/appearance
// is inconsistent across browsers (notably Firefox and Safari/iOS), so this
// renders its own suggestion list for a reliable experience everywhere.
// It renders in normal document flow (not absolutely positioned) so opening
// it pushes later fields down instead of covering them — on a phone-sized
// form, an overlay here would sit on top of the next field or the Add
// button, and a tap meant for those would silently hit a suggestion instead.
function TypeaheadInput({ id, value, onChange, options, placeholder, required }) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const listId = `${id}-listbox`;

  const matches = useMemo(() => {
    const q = value.trim().toLowerCase();
    const pool = q ? options.filter((o) => o.toLowerCase().includes(q) && o !== value) : options;
    return pool.slice(0, 8);
  }, [value, options]);

  function selectOption(m) {
    onChange(m);
    setOpen(false);
    setActiveIndex(-1);
  }

  function handleKeyDown(e) {
    if (!open || matches.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) => (i + 1) % matches.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => (i <= 0 ? matches.length - 1 : i - 1));
    } else if (e.key === "Enter" && activeIndex >= 0) {
      e.preventDefault();
      selectOption(matches[activeIndex]);
    } else if (e.key === "Escape") {
      setOpen(false);
      setActiveIndex(-1);
    }
  }

  return (
    <div className="typeahead">
      <input
        id={id}
        type="text"
        placeholder={placeholder}
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          setActiveIndex(-1);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 100)}
        onKeyDown={handleKeyDown}
        required={required}
        autoComplete="off"
        role="combobox"
        aria-expanded={open && matches.length > 0}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={activeIndex >= 0 ? `${listId}-opt-${activeIndex}` : undefined}
      />
      {open && matches.length > 0 && (
        <ul className="typeahead-list" id={listId} role="listbox">
          {matches.map((m, i) => (
            <li
              key={m}
              id={`${listId}-opt-${i}`}
              role="option"
              aria-selected={i === activeIndex}
              className={i === activeIndex ? "active" : ""}
              onMouseDown={() => selectOption(m)}
            >
              {m}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function SpendingTab({ authFetch, refreshKey }) {
  const [transactions, setTransactions] = useState([]);
  const [summary, setSummary] = useState({ by_category: [], income_by_category: [], spent: 0, earned: 0 });
  const [allCategories, setAllCategories] = useState([]);
  const [allNotes, setAllNotes] = useState([]);
  const [form, setForm] = useState({ amount: "", currency: "MKD", category: "", note: "", type: "expense" });
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);
  const [formError, setFormError] = useState(null);
  const [viewMode, setViewMode] = useState("month"); // "month" | "all"
  const [month, setMonth] = useState(currentMonthString);
  const [categoryFilter, setCategoryFilter] = useState("");
  const requestIdRef = useRef(0);

  const load = useCallback(async () => {
    const requestId = ++requestIdRef.current;
    setLoading(true);
    try {
      setLoadError(null);
      const txQuery = viewMode === "all" ? "" : `?month=${month}`;
      const sumQuery = viewMode === "all" ? "?range=all" : `?month=${month}`;
      const [txRes, sumRes] = await Promise.all([
        authFetch(`${API_BASE}/transactions${txQuery}`),
        authFetch(`${API_BASE}/summary${sumQuery}`),
      ]);
      const txJson = await txRes.json();
      const sumJson = await sumRes.json();
      if (requestId !== requestIdRef.current) return; // a newer request has since started; drop this stale result
      setTransactions(txJson);
      setSummary(sumJson);
    } catch (e) {
      if (requestId === requestIdRef.current) {
        setLoadError("Can't reach the server. Is the Flask backend running?");
      }
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  }, [authFetch, viewMode, month, refreshKey]); // refreshKey: reload after a bill is marked paid elsewhere

  const loadCategories = useCallback(async () => {
    try {
      const res = await authFetch(`${API_BASE}/categories`);
      if (res.ok) {
        const data = await res.json();
        setAllCategories(data.categories);
        setAllNotes(data.notes);
      }
    } catch (e) {
      // best-effort — autocomplete just has no suggestions if this fails
    }
  }, [authFetch]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    loadCategories();
  }, [loadCategories]);

  useEffect(() => {
    if (categoryFilter && !summary.by_category.some((c) => c.category === categoryFilter)) {
      setCategoryFilter("");
    }
  }, [summary, categoryFilter]);

  async function handleAdd(e) {
    e.preventDefault();
    if (!form.amount || !form.category) return;
    setFormError(null);
    const amountInMkd =
      form.currency === "EUR" ? Number(form.amount) * EUR_TO_MKD_RATE : Number(form.amount);
    try {
      const res = await authFetch(`${API_BASE}/transactions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          amount: amountInMkd,
          category: form.category,
          note: form.note,
          type: form.type,
          date: localDateString(),
        }),
      });
      if (!res.ok) {
        setFormError(await errorMessageFrom(res, "Couldn't save that transaction."));
        return;
      }
      setForm((f) => ({ amount: "", currency: f.currency, category: "", note: "", type: "expense" }));
      load();
      loadCategories();
    } catch (e) {
      setFormError("Can't reach the server — nothing was saved.");
    }
  }

  async function handleDelete(id) {
    if (!window.confirm("Delete this transaction?")) return;
    try {
      const res = await authFetch(`${API_BASE}/transactions/${id}`, { method: "DELETE" });
      if (!res.ok) {
        setLoadError("Couldn't delete that transaction.");
        return;
      }
      load();
    } catch (e) {
      setLoadError("Can't reach the server — nothing was deleted.");
    }
  }

  const net = summary.earned - summary.spent;

  return (
    <div className="tab-content">
      <section className="hero" aria-live="polite">
        <div className="range-bar">
          {viewMode === "month" ? (
            <>
              <button className="range-nav" onClick={() => setMonth((m) => shiftMonth(m, -1))} aria-label="Previous month">
                ‹
              </button>
              <span className="range-label">{formatMonth(month)}</span>
              <button
                className="range-nav"
                onClick={() => setMonth((m) => shiftMonth(m, 1))}
                disabled={month >= currentMonthString()}
                aria-label="Next month"
              >
                ›
              </button>
            </>
          ) : (
            <span className="range-label">All time</span>
          )}
          <button
            className="range-toggle"
            onClick={() => setViewMode((v) => (v === "month" ? "all" : "month"))}
          >
            {viewMode === "month" ? "All time" : "By month"}
          </button>
        </div>
        {!loadError && (
          <>
            <div className="hero-label">{viewMode === "all" ? "Net, all time" : "Net this month"}</div>
            <div className={`hero-value ${net >= 0 ? "earn" : "spend"}`}>
              {net >= 0 ? "+" : ""}<Money value={net} />
              <span className="hero-unit">MKD</span>
            </div>
            <dl className="hero-split">
              <div>
                <dt>Spent</dt>
                <dd className="spend"><Money value={summary.spent} /></dd>
              </div>
              <div>
                <dt>Earned</dt>
                <dd className="earn"><Money value={summary.earned} /></dd>
              </div>
            </dl>
          </>
        )}
      </section>

      <form className="quick-add" onSubmit={handleAdd}>
        <div className="segmented" role="radiogroup" aria-label="Type">
          {["expense", "income"].map((t) => (
            <button
              key={t}
              type="button"
              role="radio"
              aria-checked={form.type === t}
              className={form.type === t ? `on ${t}` : ""}
              onClick={() => setForm({ ...form, type: t })}
            >
              {t === "expense" ? "Expense" : "Income"}
            </button>
          ))}
        </div>
        <div className="amount-field">
          <input
            type="number"
            inputMode="decimal"
            step="0.01"
            min="0.01"
            placeholder="Amount"
            value={form.amount}
            onChange={(e) => setForm({ ...form, amount: e.target.value })}
            required
          />
          <select
            value={form.currency}
            onChange={(e) => setForm({ ...form, currency: e.target.value })}
            aria-label="Currency"
          >
            <option value="MKD">MKD</option>
            <option value="EUR">EUR</option>
          </select>
        </div>
        {form.currency === "EUR" && form.amount && (
          <div className="amount-conversion">
            ≈ <Money value={Number(form.amount) * EUR_TO_MKD_RATE} /> MKD
          </div>
        )}
        <TypeaheadInput
          id="tx-category"
          placeholder="Category"
          value={form.category}
          onChange={(v) => setForm({ ...form, category: v })}
          options={allCategories}
          required
        />
        <TypeaheadInput
          id="tx-note"
          placeholder="Note (optional)"
          value={form.note}
          onChange={(v) => setForm({ ...form, note: v })}
          options={allNotes}
        />
        <button type="submit" className="primary-btn">
          {form.type === "expense" ? "Add expense" : "Add income"}
        </button>
      </form>
      {formError && <div className="form-error">{formError}</div>}

      {loadError ? (
        <div className="empty-state">
          {loadError}
          <div>
            <button className="retry-btn" onClick={load}>
              Retry
            </button>
          </div>
        </div>
      ) : (
        <>
          {summary.by_category.length > 0 && (
            <>
              <div className="category-filter">
                <h3 className="section-title">Spending by category</h3>
                <select value={categoryFilter} onChange={(e) => setCategoryFilter(e.target.value)}>
                  <option value="">All categories</option>
                  {summary.by_category.map((c) => (
                    <option key={c.category} value={c.category}>
                      {c.category}
                    </option>
                  ))}
                </select>
              </div>

              {categoryFilter ? (
                <div className="stat-card category-total">
                  <div className="stat-label">Spent on {categoryFilter}</div>
                  <div className="stat-value spend">
                    <Money
                      value={summary.by_category.find((c) => c.category === categoryFilter)?.total ?? 0}
                    />
                  </div>
                </div>
              ) : (
                <div className="chart-wrap">
                  <ResponsiveContainer width="100%" height={Math.max(160, summary.by_category.length * 40)}>
                    <BarChart data={summary.by_category} layout="vertical" margin={{ top: 8, right: 24, bottom: 8, left: 0 }}>
                      <CartesianGrid stroke={CHART.grid} horizontal={false} />
                      <XAxis type="number" tick={{ fill: CHART.muted, fontSize: 11 }} axisLine={false} tickLine={false} />
                      <YAxis type="category" dataKey="category" width={96} tick={{ fill: CHART.text, fontSize: 12 }} axisLine={false} tickLine={false} />
                      <Tooltip
                        formatter={(v) => v.toFixed(2)}
                        cursor={{ fill: "rgba(255,255,255,0.04)" }}
                        contentStyle={{ background: CHART.card, border: `1px solid ${CHART.grid}`, borderRadius: 10, color: CHART.text }}
                        itemStyle={{ color: CHART.text }}
                        labelStyle={{ color: CHART.muted }}
                      />
                      <Bar dataKey="total" fill={CHART.bar} radius={[0, 6, 6, 0]} barSize={18} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}
            </>
          )}

          {summary.income_by_category?.length > 0 && (
            <>
              <h3 className="section-title">Income by category</h3>
              <table className="category-table">
                <tbody>
                  {summary.income_by_category.map((c) => (
                    <tr key={c.category}>
                      <td>{c.category}</td>
                      <td className="category-table-amount">
                        +<Money value={c.total} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          <h3 className="section-title">{viewMode === "all" ? "All transactions" : "Transactions"}</h3>
          {loading ? (
            <div className="empty-state">Loading…</div>
          ) : transactions.length === 0 ? (
            <div className="empty-state">
              {viewMode === "all" ? "No transactions yet." : "No transactions this month."}
            </div>
          ) : (
            <ul className="tx-list">
              {transactions.map((tx) => (
                <li key={tx.id} className="tx-row">
                  <div className="tx-main">
                    <span className="tx-category">{tx.category}</span>
                    {tx.note && <span className="tx-note">{tx.note}</span>}
                  </div>
                  <div className="tx-side">
                    <div className="tx-figures">
                      <span className={`tx-amount ${tx.type}`}>
                        {tx.type === "expense" ? "−" : "+"}<Money value={tx.amount} />
                      </span>
                      <span className="tx-date">{tx.date}</span>
                    </div>
                    <button className="delete-btn" onClick={() => handleDelete(tx.id)} aria-label="Delete">
                      ✕
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

function InvestingTab({ authFetch }) {
  const [holdings, setHoldings] = useState([]);
  const [form, setForm] = useState({ symbol: "", asset_type: "crypto", quantity: "", avg_buy_price: "" });
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);
  const [formError, setFormError] = useState(null);

  const load = useCallback(async () => {
    try {
      setLoadError(null);
      const res = await authFetch(`${API_BASE}/holdings`);
      setHoldings(await res.json());
    } catch (e) {
      setLoadError("Can't reach the server. Is the Flask backend running?");
    } finally {
      setLoading(false);
    }
  }, [authFetch]);

  useEffect(() => {
    load();
  }, [load]);

  async function handleAdd(e) {
    e.preventDefault();
    if (!form.symbol || !form.quantity || !form.avg_buy_price) return;
    setFormError(null);
    try {
      const res = await authFetch(`${API_BASE}/holdings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      if (!res.ok) {
        setFormError(await errorMessageFrom(res, "Couldn't save that holding."));
        return;
      }
      setForm({ symbol: "", asset_type: "crypto", quantity: "", avg_buy_price: "" });
      load();
    } catch (e) {
      setFormError("Can't reach the server — nothing was saved.");
    }
  }

  async function handleDelete(id) {
    if (!window.confirm("Delete this holding?")) return;
    try {
      const res = await authFetch(`${API_BASE}/holdings/${id}`, { method: "DELETE" });
      if (!res.ok) {
        setLoadError("Couldn't delete that holding.");
        return;
      }
      load();
    } catch (e) {
      setLoadError("Can't reach the server — nothing was deleted.");
    }
  }

  // Holdings with no priced data (e.g. a rate-limited price API) are excluded
  // from both value and cost so the totals don't look like a fake loss.
  const pricedHoldings = holdings.filter((h) => h.current_value != null);
  const unpricedCount = holdings.length - pricedHoldings.length;
  const totalValue = pricedHoldings.reduce((sum, h) => sum + h.current_value, 0);
  const totalCost = pricedHoldings.reduce((sum, h) => sum + h.cost_basis, 0);
  const totalGain = totalValue - totalCost;

  return (
    <div className="tab-content">
      {!loadError && (
        <section className="hero" aria-live="polite">
          <div className="hero-label">Portfolio value</div>
          <div className="hero-value">
            <Money value={totalValue} />
          </div>
          <dl className="hero-split">
            <div>
              <dt>Gain / loss</dt>
              <dd className={totalGain >= 0 ? "earn" : "spend"}>
                {totalGain >= 0 ? "+" : ""}<Money value={totalGain} />
              </dd>
            </div>
            <div>
              <dt>Cost</dt>
              <dd><Money value={totalCost} /></dd>
            </div>
          </dl>
        </section>
      )}

      <form className="quick-add" onSubmit={handleAdd}>
        <input
          type="text"
          placeholder="Symbol (e.g. bitcoin, AAPL)"
          value={form.symbol}
          onChange={(e) => setForm({ ...form, symbol: e.target.value })}
          required
        />
        <select
          value={form.asset_type}
          onChange={(e) => setForm({ ...form, asset_type: e.target.value })}
        >
          <option value="crypto">Crypto</option>
          <option value="stock">Stock</option>
        </select>
        <input
          type="number"
          inputMode="decimal"
          step="any"
          min="0.00000001"
          placeholder="Quantity"
          value={form.quantity}
          onChange={(e) => setForm({ ...form, quantity: e.target.value })}
          required
        />
        <input
          type="number"
          step="0.01"
          min="0.01"
          inputMode="decimal"
          placeholder="Avg buy price"
          value={form.avg_buy_price}
          onChange={(e) => setForm({ ...form, avg_buy_price: e.target.value })}
          required
        />
        <button type="submit" className="primary-btn">Add holding</button>
      </form>
      {formError && <div className="form-error">{formError}</div>}

      {loadError ? (
        <div className="empty-state">
          {loadError}
          <div>
            <button className="retry-btn" onClick={load}>
              Retry
            </button>
          </div>
        </div>
      ) : (
        <>
          {unpricedCount > 0 && (
            <div className="notice">
              {unpricedCount} holding{unpricedCount > 1 ? "s" : ""} missing price data — excluded from totals above.
            </div>
          )}

          <h3 className="section-title">Holdings</h3>
          {loading ? (
            <div className="empty-state">Loading…</div>
          ) : holdings.length === 0 ? (
            <div className="empty-state">No holdings yet. Add crypto by its CoinGecko id (e.g. "bitcoin") or a stock ticker.</div>
          ) : (
            <ul className="tx-list">
              {holdings.map((h) => (
                <li key={h.id} className="tx-row">
                  <div className="tx-main">
                    <span className="tx-category">{h.symbol.toUpperCase()}</span>
                    <span className="tx-note">{h.quantity} at avg <Money value={h.avg_buy_price} /></span>
                  </div>
                  <div className="tx-side">
                    <div className="tx-figures">
                      <span className={`tx-amount ${h.current_value == null ? "unpriced" : h.gain_loss >= 0 ? "income" : "expense"}`}>
                        {h.current_value != null ? <Money value={h.current_value} /> : "No price"}
                      </span>
                    </div>
                    <button className="delete-btn" onClick={() => handleDelete(h.id)} aria-label="Delete">
                      ✕
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

const TABS = [
  ["spending", "Spending"],
  ["investing", "Investing"],
  ["bills", "Bills"],
];

export default function App() {
  // A tapped bill notification opens /?tab=bills.
  const [tab, setTab] = useState(() => {
    const requested = new URLSearchParams(window.location.search).get("tab");
    return TABS.some(([id]) => id === requested) ? requested : "spending";
  });
  // "checking" until /api/session answers; the HttpOnly cookie itself is unreadable here.
  const [auth, setAuth] = useState("checking"); // "checking" | "signed-in" | "signed-out"

  const handleSessionEnded = useCallback(() => setAuth("signed-out"), []);

  useEffect(() => {
    try {
      localStorage.removeItem(LEGACY_TOKEN_KEY);
    } catch {
      // storage unavailable: nothing to clean up
    }
    fetch(`${API_BASE}/session`, { credentials: "same-origin", headers: CSRF_HEADERS })
      .then((res) => setAuth(res.ok ? "signed-in" : "signed-out"))
      .catch(() => setAuth("signed-out"));
  }, []);

  async function handleLogout() {
    try {
      await fetch(`${API_BASE}/logout`, { method: "POST", credentials: "same-origin", headers: CSRF_HEADERS });
    } catch {
      // offline: the cookie stays, but we still drop to the sign-in screen
    }
    setAuth("signed-out");
  }

  // Stable per session, so re-rendering App doesn't make every tab refetch.
  const authFetch = useMemo(
    () => (auth === "signed-in" ? makeAuthFetch(handleSessionEnded) : null),
    [auth, handleSessionEnded]
  );
  const { reminders, refresh: refreshReminders } = useReminders(authFetch);
  const [spendingVersion, setSpendingVersion] = useState(0);

  if (auth === "checking") return <div className="app app-login" aria-busy="true" />;
  if (auth === "signed-out") return <LoginForm onLogin={() => setAuth("signed-in")} />;

  return (
    <div className="app">
      <header className="app-header">
        <h1 className="wordmark">{TABS.find(([id]) => id === tab)[1]}</h1>
        <button className="logout-btn" onClick={handleLogout}>Log out</button>
      </header>
      <main>
        {tab === "spending" && (
          <>
            <DueBanner
              authFetch={authFetch}
              items={reminders.dueThisWeek}
              onPaid={() => {
                refreshReminders();
                setSpendingVersion((v) => v + 1);
              }}
              onOpenBills={() => setTab("bills")}
            />
            <SpendingTab authFetch={authFetch} refreshKey={spendingVersion} />
          </>
        )}
        {tab === "investing" && <InvestingTab authFetch={authFetch} />}
        {tab === "bills" && <SubscriptionsTab authFetch={authFetch} onChanged={refreshReminders} />}
      </main>
      <nav className="tabs" aria-label="Sections">
        {TABS.map(([id, label]) => {
          const badge = id === "bills" ? reminders.needsAttention.length : 0;
          return (
            <button
              key={id}
              className={tab === id ? "active" : ""}
              aria-current={tab === id ? "page" : undefined}
              aria-label={badge ? `${label}, ${badge} need${badge === 1 ? "s" : ""} attention` : undefined}
              onClick={() => setTab(id)}
            >
              {label}
              {badge > 0 && (
                <span className={`nav-badge ${reminders.overdueCount > 0 ? "overdue" : ""}`} aria-hidden="true">
                  {badge}
                </span>
              )}
            </button>
          );
        })}
      </nav>
    </div>
  );
}
