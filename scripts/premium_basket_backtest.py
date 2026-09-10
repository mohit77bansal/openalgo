from dotenv import load_dotenv; load_dotenv()
import duckdb, warnings
warnings.filterwarnings("ignore")
from services.backtest_service import run_backtest

conn = duckdb.connect('db/historify.duckdb', read_only=True)
# Liquid ATM-ish contracts: NIFTY spot ~24500, take strikes 24000-25000, both expiries, both CE/PE
rows = conn.execute("""
  SELECT symbol, COUNT(*) n FROM market_data
  WHERE (symbol LIKE 'NIFTY%CE' OR symbol LIKE 'NIFTY%PE')
    AND interval='1m'
  GROUP BY symbol HAVING n > 1500
  ORDER BY n DESC
""").fetchall()
conn.close()

# keep near-ATM strikes 24000..25000
import re
def strike(sym):
    m = re.match(r'^NIFTY\d{2}[A-Z]{3}\d{2}(\d+)(CE|PE)$', sym)
    return int(m.group(1)) if m else None
basket = [s for s,_ in rows if (k:=strike(s)) and 24000 <= k <= 25000]
print(f"basket size: {len(basket)} contracts")

STRATS = ['options_premium_vwap','options_premium_orb','options_premium_coil','options_volume_surge','options_gamma_blast_premium']
agg = {k: {'trades':0,'pnl':0.0,'wins':0,'fees':0.0,'contracts':0,'contracts_traded':0} for k in STRATS}

for sym in basket:
    for sk in STRATS:
        try:
            r = run_backtest(source='db', symbol=sym, exchange='NFO', interval='1m',
                             capital=10000, cost='zerodha', strategy_key=sk, _save=False)
            if r.get('status') != 'success': continue
            m = r.get('metrics', {})
            n = m.get('n_trades',0) or 0
            a = agg[sk]; a['contracts'] += 1
            if n>0:
                a['contracts_traded'] += 1
                a['trades'] += n
                a['pnl'] += m.get('net_pnl',0) or 0
                a['fees'] += m.get('fees_total',0) or 0
                a['wins'] += round((m.get('win_rate',0) or 0)*n)
        except Exception as e:
            pass

print(f"\n{'strategy':<32}{'contracts':>10}{'traded':>8}{'trades':>8}{'net_pnl':>12}{'win%':>7}{'fees':>10}")
for sk in STRATS:
    a=agg[sk]
    wr = (a['wins']/a['trades']*100) if a['trades'] else 0
    print(f"{sk:<32}{a['contracts']:>10}{a['contracts_traded']:>8}{a['trades']:>8}{a['pnl']:>12.0f}{wr:>7.1f}{a['fees']:>10.0f}")
