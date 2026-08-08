# blueprints/backtest.py
# Backtest surface — event-driven backtesting powered by the vendored aladin
# `qbacktest` engine (NSE-correct cost model, Greeks, walk-forward). This is the
# capability OpenAlgo does not otherwise have.
#
# Additive module: a new top-level blueprint that calls services/backtest_service.
# It does not modify any existing OpenAlgo behaviour, so it rebases cleanly onto
# upstream. The only core touch-point is a two-line registration in app.py.
#
# NOTE: routes are intentionally public for this first proof so the loop can be
# verified without a broker session. Before real use, gate the pages behind
# `@check_session_validity` like the other blueprints.

from flask import Blueprint, jsonify, render_template_string, request

from services.backtest_service import DEFAULT_CAPITAL, list_strategies, run_backtest, run_demo_backtest, run_multi_instrument_backtest
from utils.logging import get_logger

logger = get_logger(__name__)

backtest_bp = Blueprint("backtest_bp", __name__, url_prefix="/backtest")


def _equity_svg(equity_curve: list, width: int = 820, height: int = 320) -> str:
    """Render the equity curve as an inline SVG polyline (CSP-safe: no JS)."""
    pts = [(i, float(eq)) for i, (_, eq) in enumerate(equity_curve) if eq is not None]
    if len(pts) < 2:
        return '<svg width="%d" height="%d"></svg>' % (width, height)
    ys = [y for _, y in pts]
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1.0
    pad, n = 40, len(pts)
    base = pts[0][1]  # starting capital baseline

    def X(i: int) -> float:
        return pad + (i / (n - 1)) * (width - 2 * pad)

    def Y(v: float) -> float:
        return (height - pad) - ((v - lo) / span) * (height - 2 * pad)

    line = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in pts)
    base_y = Y(base)
    up = pts[-1][1] >= base
    colour = "#16a34a" if up else "#dc2626"
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Equity curve">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#0b0f14"/>'
        f'<line x1="{pad}" y1="{base_y:.1f}" x2="{width - pad}" y2="{base_y:.1f}" '
        f'stroke="#334155" stroke-dasharray="4 4"/>'
        f'<polyline fill="none" stroke="{colour}" stroke-width="2" points="{line}"/>'
        f'<text x="{pad}" y="20" fill="#94a3b8" font-family="monospace" font-size="12">'
        f'equity {lo:,.0f} – {hi:,.0f}</text>'
        f'</svg>'
    )


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Backtest — OpenAlgo × aladin</title>
<style>
  body{background:#0b0f14;color:#e2e8f0;font-family:system-ui,sans-serif;margin:0;padding:24px}
  h1{font-size:20px;margin:0 0 4px} .sub{color:#94a3b8;font-size:13px;margin-bottom:20px}
  .card{background:#111826;border:1px solid #1f2937;border-radius:10px;padding:16px;margin-bottom:16px}
  table{border-collapse:collapse;font-size:13px} td,th{padding:4px 14px 4px 0;text-align:left}
  th{color:#94a3b8;font-weight:500} .pos{color:#16a34a} .neg{color:#dc2626}
  code{background:#1f2937;padding:2px 6px;border-radius:4px;font-size:12px}
</style></head><body>
  <h1>Backtest — powered by aladin <code>qbacktest</code></h1>
  <div class="sub">Event-driven engine · NSE-correct cost model · running inside OpenAlgo.
    Demo strategy <b>{{ r.strategy }}</b> on synthetic <b>{{ r.symbol }}</b> ·
    JSON at <code>/backtest/api/run</code></div>
  {% if r.status == 'success' %}
  <div class="card">{{ svg|safe }}</div>
  <div class="card">
    <table>
      <tr><th>Net PnL</th><th>Sharpe</th><th>Max DD %</th><th>Trades</th><th>Fees (₹)</th><th>Capital</th><th>Cost model</th></tr>
      <tr>
        <td class="{{ 'pos' if (r.metrics.net_pnl or 0) >= 0 else 'neg' }}">₹{{ '%.2f'|format(r.metrics.net_pnl or 0) }}</td>
        <td>{{ '%.3f'|format(r.metrics.sharpe or 0) }}</td>
        <td>{{ '%.2f'|format(r.metrics.max_drawdown_pct or 0) }}</td>
        <td>{{ r.metrics.n_trades }}</td>
        <td>{{ '%.2f'|format(r.metrics.fees_total or 0) }}</td>
        <td>₹{{ '{:,.0f}'.format(r.capital) }}</td>
        <td><code>{{ r.cost_model }}</code></td>
      </tr>
    </table>
  </div>
  {% else %}
  <div class="card neg">Backtest failed: {{ r.message }}</div>
  {% endif %}
</body></html>"""


@backtest_bp.route("/", methods=["GET"])
def backtest_home():
    """Server-rendered proof page: runs the demo backtest and draws the curve."""
    capital = request.args.get("capital", DEFAULT_CAPITAL, type=float)
    cost = request.args.get("cost", "zerodha")
    r = run_demo_backtest(capital=capital or DEFAULT_CAPITAL, cost=cost)
    svg = _equity_svg(r.get("equity_curve", [])) if r.get("status") == "success" else ""
    return render_template_string(_PAGE, r=r, svg=svg)


@backtest_bp.route("/api/run", methods=["GET", "POST"])
def backtest_run_api():
    """JSON endpoint: run a backtest (source=demo|db) and return metrics + equity curve."""
    params = request.get_json(silent=True) or request.values
    result = run_backtest(
        source=params.get("source", "demo"),
        symbol=params.get("symbol", "NIFTY"),
        exchange=params.get("exchange", "NSE"),
        interval=params.get("interval", "D"),
        start=params.get("start") or None,
        end=params.get("end") or None,
        capital=params.get("capital", DEFAULT_CAPITAL),
        cost=params.get("cost", "zerodha"),
        strategy_key=params.get("strategy", "sma_momentum"),
    )
    status = 200 if result.get("status") == "success" else 500
    return jsonify(result), status


@backtest_bp.route("/api/strategies", methods=["GET"])
def backtest_strategies():
    """List available backtest strategies with descriptions."""
    return jsonify({"status": "success", "strategies": list_strategies()})


@backtest_bp.route("/api/history", methods=["GET"])
def backtest_history():
    """List recent backtest runs (newest first)."""
    from database.backtest_db import list_backtest_runs
    limit = request.args.get("limit", 50, type=int)
    runs = list_backtest_runs(limit=min(limit, 200))
    return jsonify({"status": "success", "runs": runs})


@backtest_bp.route("/api/run/<int:run_id>", methods=["GET"])
def backtest_detail(run_id: int):
    """Get a single backtest run with full equity curve."""
    from database.backtest_db import get_backtest_run
    run = get_backtest_run(run_id)
    if not run:
        return jsonify({"status": "error", "message": "run not found"}), 404
    return jsonify({"status": "success", **run})


@backtest_bp.route("/api/run/<int:run_id>/favorite", methods=["POST"])
def backtest_toggle_favorite(run_id: int):
    """Toggle favorite status on a backtest run."""
    from database.backtest_db import db_session, BacktestRun
    try:
        run = db_session.query(BacktestRun).get(run_id)
        if not run:
            return jsonify({"status": "error", "message": "run not found"}), 404
        run.is_favorite = 0 if run.is_favorite else 1
        db_session.commit()
        return jsonify({"status": "success", "is_favorite": bool(run.is_favorite)})
    finally:
        db_session.remove()


@backtest_bp.route("/api/run/<int:run_id>/remarks", methods=["POST"])
def backtest_update_remarks(run_id: int):
    """Update remarks on a backtest run."""
    from database.backtest_db import db_session, BacktestRun
    params = request.get_json(silent=True) or {}
    remarks = params.get("remarks", "")
    try:
        run = db_session.query(BacktestRun).get(run_id)
        if not run:
            return jsonify({"status": "error", "message": "run not found"}), 404
        run.remarks = remarks
        db_session.commit()
        return jsonify({"status": "success", "remarks": run.remarks})
    finally:
        db_session.remove()


@backtest_bp.route("/api/run/<int:run_id>/montecarlo", methods=["GET"])
def backtest_monte_carlo(run_id: int):
    """Run Monte Carlo simulation on a completed backtest's trade PnLs."""
    from database.backtest_db import get_backtest_run
    from services.monte_carlo_service import run_monte_carlo
    import json

    run = get_backtest_run(run_id)
    if not run:
        return jsonify({"status": "error", "message": "run not found"}), 404

    trades = json.loads(run.get("equity_json", "[]")) if isinstance(run.get("equity_json"), str) else []
    # Extract per-trade PnLs from the trades list
    trade_pnls = []
    for t in (run.get("trades") or []):
        if isinstance(t, dict):
            pnl = t.get("pnl") or t.get("realized_pnl") or 0
            if isinstance(pnl, (int, float)):
                trade_pnls.append(float(pnl))

    if not trade_pnls:
        return jsonify({"status": "error", "message": "no trade PnLs available for simulation"}), 400

    n_sims = request.args.get("n", 1000, type=int)
    result = run_monte_carlo(
        trade_pnls=trade_pnls,
        starting_capital=run.get("capital", 1000000),
        n_simulations=min(n_sims, 10000),
    )
    return jsonify(result)


@backtest_bp.route("/api/multi-run", methods=["POST"])
def backtest_multi_run():
    """Run one strategy across multiple instruments in a single backtest.

    Body: { symbols: ["NIFTY25AUG26FUT", "BANKNIFTY25AUG26FUT", ...],
            exchange, interval, start, end, capital, cost, strategy,
            weights: [0.5, 0.5] (optional, equal if omitted) }
    """
    params = request.get_json(silent=True) or {}
    symbols = params.get("symbols", [])
    if not symbols or not isinstance(symbols, list):
        return jsonify({"status": "error", "message": "symbols must be a non-empty list"}), 400

    result = run_multi_instrument_backtest(
        symbols=symbols,
        exchange=params.get("exchange", "NFO"),
        interval=params.get("interval", "15m"),
        start=params.get("start"),
        end=params.get("end"),
        capital=float(params.get("capital", DEFAULT_CAPITAL)),
        cost=params.get("cost", "zerodha"),
        strategy_key=params.get("strategy", "atr_channel_breakout"),
        source=params.get("source", "db"),
        weights=params.get("weights"),
    )
    return jsonify(result)
