"""Basket backtest: round-1 (baseline) vs round-2 (selectivity-first) premium strategies.

Runs every strategy across the near-ATM NIFTY option basket and prints a
comparison table with per-contract trade counts, net PnL, win%, fees, and
avg PnL/trade so the fee-vs-edge story is explicit.
"""
from dotenv import load_dotenv; load_dotenv()
import duckdb, re, warnings, time as _time
warnings.filterwarnings("ignore")
from services.backtest_service import run_backtest

conn = duckdb.connect('db/historify.duckdb', read_only=True)
rows = conn.execute("""
  SELECT symbol, COUNT(*) n FROM market_data
  WHERE (symbol LIKE 'NIFTY%CE' OR symbol LIKE 'NIFTY%PE')
    AND interval='1m'
  GROUP BY symbol HAVING n > 1500
  ORDER BY n DESC
""").fetchall()
conn.close()

def strike(sym):
    m = re.match(r'^NIFTY\d{2}[A-Z]{3}\d{2}(\d+)(CE|PE)$', sym)
    return int(m.group(1)) if m else None
basket = [s for s, _ in rows if (k := strike(s)) and 24000 <= k <= 25000]
print(f"basket size: {len(basket)} contracts")

BASELINE = ['options_premium_vwap', 'options_premium_orb', 'options_premium_coil',
            'options_volume_surge', 'options_gamma_blast_premium']
NEW = ['options_oi_momentum', 'options_opening_drive', 'options_expiry_gamma']
STRATS = BASELINE + NEW

agg = {k: {'trades': 0, 'pnl': 0.0, 'wins': 0, 'fees': 0.0,
           'contracts_traded': 0} for k in STRATS}

t0 = _time.time()
for sym in basket:
    for sk in STRATS:
        try:
            r = run_backtest(source='db', symbol=sym, exchange='NFO', interval='1m',
                             capital=10000, cost='zerodha', strategy_key=sk, _save=False)
            if r.get('status') != 'success':
                continue
            m = r.get('metrics', {})
            n = m.get('n_trades', 0) or 0
            if n > 0:
                a = agg[sk]
                a['contracts_traded'] += 1
                a['trades'] += n
                a['pnl'] += m.get('net_pnl', 0) or 0
                a['fees'] += m.get('fees_total', 0) or 0
                a['wins'] += round((m.get('win_rate', 0) or 0) * n)
        except Exception:
            pass

hdr = (f"{'strategy':<30}{'traded':>7}{'trades':>8}{'net_pnl':>12}"
       f"{'win%':>7}{'fees':>10}{'avg/trade':>11}")
print("\n=== BASELINE (round 1) ===")
print(hdr)
def line(sk):
    a = agg[sk]
    wr = (a['wins'] / a['trades'] * 100) if a['trades'] else 0
    avg = (a['pnl'] / a['trades']) if a['trades'] else 0
    print(f"{sk:<30}{a['contracts_traded']:>7}{a['trades']:>8}{a['pnl']:>12.0f}"
          f"{wr:>7.1f}{a['fees']:>10.0f}{avg:>11.0f}")
for sk in BASELINE: line(sk)
print("\n=== NEW (round 2, selectivity-first) ===")
print(hdr)
for sk in NEW: line(sk)
print(f"\nelapsed: {_time.time()-t0:.0f}s")
