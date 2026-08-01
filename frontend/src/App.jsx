import { useEffect, useState, useCallback, useMemo } from "react";
import {
  PieChart,
  Pie,
  Cell,
  Tooltip,
  ResponsiveContainer,
} from "recharts";

// In production (Render), set VITE_API_BASE to your backend's Render URL,
// e.g. https://finance-tracker-api.onrender.com/api
// Locally, it falls back to whatever host you loaded the page from,
// so it works whether you open it via localhost or your LAN IP.
const API_BASE = (
  import.meta.env.VITE_API_BASE || `http://${window.location.hostname}:5000/api`
).replace(/\/+$/, "");

const COLORS = ["#34D399", "#60A5FA", "#F472B6", "#FBBF24", "#A78BFA", "#F87171", "#4ADE80"];

const TOKEN_KEY = "finance_tracker_token";

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
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) {
        setError("Invalid username or password.");
        return;
      }
      const { token } = await res.json();
      onLogin(token);
    } catch (e) {
      setError("Can't reach the server.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>Finance Tracker</h1>
      </header>
      <main className="tab-content">
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
        {error && <div className="empty-state">{error}</div>}
      </main>
    </div>
  );
}

function Money({ value }) {
  const n = Number(value ?? 0);
  return <span>{n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>;
}

// Custom dropdown instead of <datalist> — native datalist support/appearance
// is inconsistent across browsers (notably Firefox and Safari/iOS), so this
// renders its own suggestion list for a reliable experience everywhere.
function TypeaheadInput({ value, onChange, options, placeholder, required }) {
  const [open, setOpen] = useState(false);

  const matches = useMemo(() => {
    const q = value.trim().toLowerCase();
    const pool = q ? options.filter((o) => o.toLowerCase().includes(q) && o !== value) : options;
    return pool.slice(0, 8);
  }, [value, options]);

  return (
    <div className="typeahead">
      <input
        type="text"
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 100)}
        required={required}
        autoComplete="off"
      />
      {open && matches.length > 0 && (
        <ul className="typeahead-list">
          {matches.map((m) => (
            <li key={m} onMouseDown={() => onChange(m)}>
              {m}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function makeAuthFetch(token, onUnauthorized) {
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

function currentMonthString() {
  return new Date().toISOString().slice(0, 7);
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

function SpendingTab({ authFetch }) {
  const [transactions, setTransactions] = useState([]);
  const [summary, setSummary] = useState({ by_category: [], spent: 0, earned: 0 });
  const [form, setForm] = useState({ amount: "", category: "", note: "", type: "expense" });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [viewMode, setViewMode] = useState("month"); // "month" | "all"
  const [month, setMonth] = useState(currentMonthString);

  const load = useCallback(async () => {
    try {
      setError(null);
      const txQuery = viewMode === "all" ? "" : `?month=${month}`;
      const sumQuery = viewMode === "all" ? "?range=all" : `?month=${month}`;
      const [txRes, sumRes] = await Promise.all([
        authFetch(`${API_BASE}/transactions${txQuery}`),
        authFetch(`${API_BASE}/summary${sumQuery}`),
      ]);
      setTransactions(await txRes.json());
      setSummary(await sumRes.json());
    } catch (e) {
      setError("Can't reach the server. Is the Flask backend running?");
    } finally {
      setLoading(false);
    }
  }, [authFetch, viewMode, month]);

  useEffect(() => {
    load();
  }, [load]);

  const categoryOptions = useMemo(
    () => [...new Set(transactions.map((tx) => tx.category).filter(Boolean))].sort(),
    [transactions]
  );
  const noteOptions = useMemo(
    () => [...new Set(transactions.map((tx) => tx.note).filter(Boolean))].sort(),
    [transactions]
  );

  async function handleAdd(e) {
    e.preventDefault();
    if (!form.amount || !form.category) return;
    await authFetch(`${API_BASE}/transactions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(form),
    });
    setForm({ amount: "", category: "", note: "", type: "expense" });
    load();
  }

  async function handleDelete(id) {
    await authFetch(`${API_BASE}/transactions/${id}`, { method: "DELETE" });
    load();
  }

  if (error) return <div className="empty-state">{error}</div>;

  const net = summary.earned - summary.spent;

  return (
    <div className="tab-content">
      <form className="quick-add" onSubmit={handleAdd}>
        <input
          type="number"
          step="0.01"
          placeholder="Amount"
          value={form.amount}
          onChange={(e) => setForm({ ...form, amount: e.target.value })}
          required
        />
        <TypeaheadInput
          placeholder="Category"
          value={form.category}
          onChange={(v) => setForm({ ...form, category: v })}
          options={categoryOptions}
          required
        />
        <TypeaheadInput
          placeholder="Note (optional)"
          value={form.note}
          onChange={(v) => setForm({ ...form, note: v })}
          options={noteOptions}
        />
        <select
          value={form.type}
          onChange={(e) => setForm({ ...form, type: e.target.value })}
        >
          <option value="expense">Expense</option>
          <option value="income">Income</option>
        </select>
        <button type="submit">Add</button>
      </form>

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
          {viewMode === "month" ? "View all time" : "View by month"}
        </button>
      </div>

      <div className="summary-row">
        <div className="stat-card">
          <div className="stat-label">{viewMode === "all" ? "Spent" : "Spent this month"}</div>
          <div className="stat-value spend"><Money value={summary.spent} /></div>
        </div>
        <div className="stat-card">
          <div className="stat-label">{viewMode === "all" ? "Earned" : "Earned this month"}</div>
          <div className="stat-value earn"><Money value={summary.earned} /></div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Net</div>
          <div className={`stat-value ${net >= 0 ? "earn" : "spend"}`}>
            {net >= 0 ? "+" : ""}<Money value={net} />
          </div>
        </div>
      </div>

      {summary.by_category.length > 0 && (
        <div className="chart-wrap">
          <ResponsiveContainer width="100%" height={220}>
            <PieChart>
              <Pie
                data={summary.by_category}
                dataKey="total"
                nameKey="category"
                innerRadius={50}
                outerRadius={80}
                paddingAngle={2}
              >
                {summary.by_category.map((_, i) => (
                  <Cell key={i} fill={COLORS[i % COLORS.length]} />
                ))}
              </Pie>
              <Tooltip formatter={(v) => v.toFixed(2)} />
            </PieChart>
          </ResponsiveContainer>
        </div>
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
                <span className={`tx-amount ${tx.type}`}>
                  {tx.type === "expense" ? "-" : "+"}<Money value={tx.amount} />
                </span>
                <span className="tx-category">{tx.category}</span>
                {tx.note && <span className="tx-note">{tx.note}</span>}
              </div>
              <div className="tx-side">
                <span className="tx-date">{tx.date}</span>
                <button className="delete-btn" onClick={() => handleDelete(tx.id)} aria-label="Delete">
                  ✕
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function InvestingTab({ authFetch }) {
  const [holdings, setHoldings] = useState([]);
  const [form, setForm] = useState({ symbol: "", asset_type: "crypto", quantity: "", avg_buy_price: "" });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      setError(null);
      const res = await authFetch(`${API_BASE}/holdings`);
      setHoldings(await res.json());
    } catch (e) {
      setError("Can't reach the server. Is the Flask backend running?");
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
    await authFetch(`${API_BASE}/holdings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(form),
    });
    setForm({ symbol: "", asset_type: "crypto", quantity: "", avg_buy_price: "" });
    load();
  }

  async function handleDelete(id) {
    await authFetch(`${API_BASE}/holdings/${id}`, { method: "DELETE" });
    load();
  }

  const totalValue = holdings.reduce((sum, h) => sum + (h.current_value || 0), 0);
  const totalCost = holdings.reduce((sum, h) => sum + (h.cost_basis || 0), 0);
  const totalGain = totalValue - totalCost;

  if (error) return <div className="empty-state">{error}</div>;

  return (
    <div className="tab-content">
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
          step="any"
          placeholder="Quantity"
          value={form.quantity}
          onChange={(e) => setForm({ ...form, quantity: e.target.value })}
          required
        />
        <input
          type="number"
          step="0.01"
          placeholder="Avg buy price"
          value={form.avg_buy_price}
          onChange={(e) => setForm({ ...form, avg_buy_price: e.target.value })}
          required
        />
        <button type="submit">Add</button>
      </form>

      <div className="summary-row">
        <div className="stat-card">
          <div className="stat-label">Portfolio value</div>
          <div className="stat-value"><Money value={totalValue} /></div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Gain / loss</div>
          <div className={`stat-value ${totalGain >= 0 ? "earn" : "spend"}`}>
            {totalGain >= 0 ? "+" : ""}<Money value={totalGain} />
          </div>
        </div>
      </div>

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
                <span className="tx-note">{h.quantity} @ avg <Money value={h.avg_buy_price} /></span>
              </div>
              <div className="tx-side">
                <span className={`tx-amount ${h.gain_loss >= 0 ? "income" : "expense"}`}>
                  {h.current_value != null ? <><Money value={h.current_value} /></> : "price unavailable"}
                </span>
                <button className="delete-btn" onClick={() => handleDelete(h.id)} aria-label="Delete">
                  ✕
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState("spending");
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY));

  function handleLogin(newToken) {
    localStorage.setItem(TOKEN_KEY, newToken);
    setToken(newToken);
  }

  function handleLogout() {
    localStorage.removeItem(TOKEN_KEY);
    setToken(null);
  }

  if (!token) return <LoginForm onLogin={handleLogin} />;

  const authFetch = makeAuthFetch(token, handleLogout);

  return (
    <div className="app">
      <header className="app-header">
        <h1>Finance Tracker</h1>
        <nav className="tabs">
          <button
            className={tab === "spending" ? "active" : ""}
            onClick={() => setTab("spending")}
          >
            Spending
          </button>
          <button
            className={tab === "investing" ? "active" : ""}
            onClick={() => setTab("investing")}
          >
            Investing
          </button>
          <button onClick={handleLogout}>Log out</button>
        </nav>
      </header>
      <main>
        {tab === "spending" ? <SpendingTab authFetch={authFetch} /> : <InvestingTab authFetch={authFetch} />}
      </main>
    </div>
  );
}
