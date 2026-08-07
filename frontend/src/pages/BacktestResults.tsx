/**
 * Backtest report — a dedicated page for the result of one run.
 *
 * Kept off the builder page so a long report does not sit permanently below
 * the form: the result lives in useBacktestStore, set by the builder right
 * before it navigates here. No result means the user deep-linked or reloaded,
 * so we send them back to the form rather than render an empty report.
 */
import { Navigate, useNavigate } from 'react-router'
import { OHLCChart } from '@/components/backtest/OHLCChart'
import { PortfolioLineChart } from '@/components/portfolio/PortfolioLineChart'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'
import { useBacktestStore } from '@/stores/backtestStore'

const money = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined
    ? '-'
    : `₹${v.toLocaleString('en-IN', { maximumFractionDigits: dp })}`
const num = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined ? '-' : v.toFixed(dp)

/** One headline number, coloured by whether it is good or bad news. */
function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string
  value: string
  sub?: string
  tone?: 'good' | 'bad'
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-xs font-medium text-muted-foreground">{label}</div>
        <div
          className={cn(
            'mt-1 text-2xl font-semibold tabular-nums',
            tone === 'good' && 'text-emerald-500',
            tone === 'bad' && 'text-rose-500'
          )}
        >
          {value}
        </div>
        {sub && <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>}
      </CardContent>
    </Card>
  )
}

export default function BacktestResults() {
  const navigate = useNavigate()
  const { result } = useBacktestStore()

  if (!result || !result.metrics) {
    return <Navigate to="/backtest" replace />
  }

  const m = result.metrics
  // The chart indexes on a bare YYYY-MM-DD; slice keeps it working whether the
  // engine sends a plain date or a full session timestamp.
  const equity = result.equity.map((p) => ({ date: p.date.slice(0, 10), value: p.value }))

  return (
    <div className="container mx-auto space-y-4 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="outline" size="sm" onClick={() => navigate('/backtest')}>
          Back to Builder
        </Button>
        <div>
          <h1 className="text-2xl font-bold tracking-tight">{result.strategy}</h1>
          <p className="text-sm text-muted-foreground">
            {result.symbol}/{result.exchange} · {result.interval} ·{' '}
            {result.source === 'db' ? 'AngelOne' : 'Synthetic'} · {result.cost_model}
            {result.start ? ` · ${result.start} → ${result.end}` : ''}
          </p>
        </div>
      </div>

      {result.strategy_description && (
        <Card>
          <CardContent className="p-4">
            <div className="text-xs font-medium text-muted-foreground mb-1">Strategy Logic</div>
            <p className="text-sm">{result.strategy_description}</p>
            {result.data_note && (
              <p className="text-xs text-amber-500 mt-2">{result.data_note}</p>
            )}
          </CardContent>
        </Card>
      )}

      <div className="grid gap-3 md:grid-cols-4">
        <Stat
          label="Net P&L"
          value={money(m.net_pnl)}
          sub={result.capital ? `${(((m.net_pnl ?? 0) / result.capital) * 100).toFixed(2)}% of capital` : undefined}
          tone={(m.net_pnl ?? 0) >= 0 ? 'good' : 'bad'}
        />
        <Stat
          label="XIRR (Annualized)"
          value={result.xirr_pct != null ? `${result.xirr_pct.toFixed(1)}%` : '-'}
          tone={result.xirr_pct != null ? ((result.xirr_pct ?? 0) >= 0 ? 'good' : 'bad') : undefined}
        />
        <Stat
          label="Return on Margin"
          value={result.return_on_margin_pct != null ? `${result.return_on_margin_pct.toFixed(1)}%` : '-'}
          sub={result.margin_used ? `Margin: ${money(result.margin_used, 0)}/lot` : undefined}
          tone={result.return_on_margin_pct != null ? ((result.return_on_margin_pct ?? 0) >= 0 ? 'good' : 'bad') : undefined}
        />
        <Stat label="Sharpe" value={num(m.sharpe)} />
        <Stat label="Max Drawdown" value={`${num(m.max_drawdown_pct)}%`} tone="bad" />
        <Stat label="Trades" value={num(m.n_trades, 0)} sub={`Win rate: ${m.n_trades && result.trades?.length ? `${((result.trades.filter((t: Record<string, unknown>) => { const ef = (t.entry_fill ?? {}) as Record<string, unknown>; const xf = (t.exit_fill ?? {}) as Record<string, unknown>; const ep = Number(ef.price ?? 0); const xp = Number(xf.price ?? 0); const dir = String(t.direction ?? ''); return dir === 'BUY' ? xp > ep : xp < ep; }).length / result.trades.length) * 100).toFixed(0)}%` : '-'}`} />
        <Stat label="Fees" value={money(m.fees_total)} />
        <Stat
          label="Risk:Reward"
          value={(() => {
            if (!result.trades?.length) return '-'
            const wins: number[] = []; const losses: number[] = []
            for (const t of result.trades as Record<string, unknown>[]) {
              const ef = (t.entry_fill ?? {}) as Record<string, unknown>
              const xf = (t.exit_fill ?? {}) as Record<string, unknown>
              const ep = Number(ef.price ?? 0); const xp = Number(xf.price ?? 0)
              const dir = String(t.direction ?? '')
              const pnl = dir === 'BUY' ? xp - ep : ep - xp
              if (pnl >= 0) wins.push(pnl); else losses.push(Math.abs(pnl))
            }
            const avgWin = wins.length ? wins.reduce((a, b) => a + b, 0) / wins.length : 0
            const avgLoss = losses.length ? losses.reduce((a, b) => a + b, 0) / losses.length : 1
            return avgLoss > 0 ? `1:${(avgWin / avgLoss).toFixed(1)}` : '-'
          })()}
          sub="avg win : avg loss"
        />
        <Stat label="Profit Factor" value={(() => {
          if (!result.trades?.length) return '-'
          let grossWin = 0; let grossLoss = 0
          for (const t of result.trades as Record<string, unknown>[]) {
            const ef = (t.entry_fill ?? {}) as Record<string, unknown>
            const xf = (t.exit_fill ?? {}) as Record<string, unknown>
            const ep = Number(ef.price ?? 0); const xp = Number(xf.price ?? 0)
            const dir = String(t.direction ?? '')
            const pnl = dir === 'BUY' ? xp - ep : ep - xp
            if (pnl >= 0) grossWin += pnl; else grossLoss += Math.abs(pnl)
          }
          return grossLoss > 0 ? (grossWin / grossLoss).toFixed(2) : '-'
        })()} sub="gross win / gross loss" />
        <Stat label="Capital" value={money(result.capital, 0)} sub={`${result.interval} · ${result.source === 'db' ? 'AngelOne' : 'Synthetic'}`} />
      </div>

      {result.ohlc && result.ohlc.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Price Chart with Trades</CardTitle>
          </CardHeader>
          <CardContent>
            <OHLCChart bars={result.ohlc} trades={result.trades} height={400} />
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Equity Curve</CardTitle>
          <p className="text-sm text-muted-foreground">
            Value of {money(result.capital, 0)} over {result.n_bars} bars.
          </p>
        </CardHeader>
        <CardContent>
          <PortfolioLineChart
            height={340}
            format={(v) => money(v, 0)}
            series={[{ name: 'Equity', color: '#3b82f6', data: equity, area: true }]}
          />
        </CardContent>
      </Card>

      {result.strategy_description && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Strategy Logic</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">{result.strategy_description}</p>
          </CardContent>
        </Card>
      )}

      {result.trades && result.trades.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Trades ({result.trades.length})</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b text-left text-muted-foreground">
                    <th className="pb-2 pr-3">#</th>
                    <th className="pb-2 pr-3">Instrument</th>
                    <th className="pb-2 pr-3">Side</th>
                    <th className="pb-2 pr-3">Entry Time</th>
                    <th className="pb-2 pr-3 text-right">Entry Price</th>
                    <th className="pb-2 pr-3">Exit Time</th>
                    <th className="pb-2 pr-3 text-right">Exit Price</th>
                    <th className="pb-2 pr-3 text-right">Qty</th>
                    <th className="pb-2 pr-3 text-right">P&L</th>
                    <th className="pb-2 text-right">Fees</th>
                  </tr>
                </thead>
                <tbody>
                  {result.trades.map((t: Record<string, unknown>, i: number) => {
                    const ef = (t.entry_fill ?? {}) as Record<string, unknown>
                    const xf = (t.exit_fill ?? {}) as Record<string, unknown>
                    const inst = (t.instrument ?? ef.instrument ?? {}) as Record<string, unknown>
                    const entryPrice = Number(ef.price ?? t.entry_price ?? 0)
                    const exitPrice = Number(xf.price ?? t.exit_price ?? 0)
                    const qty = Number(ef.quantity ?? xf.quantity ?? t.quantity ?? t.qty ?? 0)
                    const entryFees = Number(ef.fees ?? 0)
                    const exitFees = Number(xf.fees ?? 0)
                    const totalFees = entryFees + exitFees
                    const dir = String(t.direction ?? t.side ?? '-')
                    const sign = dir === 'BUY' || dir === 'LONG' ? 1 : -1
                    const pnl = Number(t.pnl ?? t.realized_pnl ?? ((exitPrice - entryPrice) * qty * sign - totalFees))
                    const entryTs = String(ef.ts ?? ef.timestamp ?? t.entry_time ?? t.entry_ts ?? '-')
                    const exitTs = String(xf.ts ?? xf.timestamp ?? t.exit_time ?? t.exit_ts ?? '-')
                    const formatTs = (ts: string) => {
                      if (ts === '-') return '-'
                      try { return new Date(ts).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false }) } catch { return ts.slice(0, 16) }
                    }
                    return (
                      <tr key={i} className="border-b border-border/50 last:border-0">
                        <td className="py-1.5 pr-3 text-muted-foreground">{i + 1}</td>
                        <td className="py-1.5 pr-3 font-medium text-xs">{String(inst.symbol ?? t.instrument_id ?? '-')}</td>
                        <td className={cn('py-1.5 pr-3 font-medium', sign > 0 ? 'text-emerald-500' : 'text-rose-500')}>
                          {dir}
                        </td>
                        <td className="py-1.5 pr-3 text-muted-foreground whitespace-nowrap">{formatTs(entryTs)}</td>
                        <td className="py-1.5 pr-3 text-right tabular-nums">{entryPrice.toFixed(2)}</td>
                        <td className="py-1.5 pr-3 text-muted-foreground whitespace-nowrap">{formatTs(exitTs)}</td>
                        <td className="py-1.5 pr-3 text-right tabular-nums">{exitPrice.toFixed(2)}</td>
                        <td className="py-1.5 pr-3 text-right tabular-nums">{qty || '-'}</td>
                        <td className={cn('py-1.5 pr-3 text-right tabular-nums font-medium', pnl >= 0 ? 'text-emerald-500' : 'text-rose-500')}>
                          {money(pnl)}
                        </td>
                        <td className="py-1.5 text-right tabular-nums">{money(totalFees)}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
