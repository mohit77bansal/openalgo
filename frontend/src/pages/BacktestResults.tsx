/**
 * Backtest report — a dedicated page for the result of one run.
 *
 * Kept off the builder page so a long report does not sit permanently below
 * the form: the result lives in useBacktestStore, set by the builder right
 * before it navigates here. No result means the user deep-linked or reloaded,
 * so we send them back to the form rather than render an empty report.
 */
import { Navigate, useNavigate } from 'react-router'
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
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Backtest Report</h1>
          <p className="text-sm text-muted-foreground">
            {result.strategy} · {result.symbol} {result.exchange} · {result.interval} ·{' '}
            {result.source} · {result.cost_model}
          </p>
        </div>
        <Button variant="outline" onClick={() => navigate('/backtest')}>
          Back to Builder
        </Button>
      </div>

      <div className="grid gap-3 md:grid-cols-4">
        <Stat
          label="Net P&L"
          value={money(m.net_pnl)}
          tone={(m.net_pnl ?? 0) >= 0 ? 'good' : 'bad'}
        />
        <Stat label="Sharpe" value={num(m.sharpe)} />
        <Stat label="Max Drawdown" value={`${num(m.max_drawdown_pct)}%`} tone="bad" />
        <Stat label="Trades" value={num(m.n_trades, 0)} />
        <Stat label="Fees" value={money(m.fees_total)} />
        <Stat label="Bars" value={String(result.n_bars)} />
      </div>

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
                    const pnl = Number(t.pnl ?? t.realized_pnl ?? 0)
                    return (
                      <tr key={i} className="border-b border-border/50 last:border-0">
                        <td className="py-1.5 pr-3 text-muted-foreground">{i + 1}</td>
                        <td className="py-1.5 pr-3 font-medium">{String(t.instrument_id ?? t.instrument ?? t.symbol ?? '-')}</td>
                        <td className={cn('py-1.5 pr-3 font-medium', String(t.side ?? '').includes('BUY') || String(t.direction ?? '') === 'LONG' ? 'text-emerald-500' : 'text-rose-500')}>
                          {String(t.side ?? t.direction ?? '-')}
                        </td>
                        <td className="py-1.5 pr-3 text-muted-foreground whitespace-nowrap">{String(t.entry_time ?? t.entry_ts ?? '-').slice(0, 19)}</td>
                        <td className="py-1.5 pr-3 text-right tabular-nums">{Number(t.entry_price ?? 0).toFixed(2)}</td>
                        <td className="py-1.5 pr-3 text-muted-foreground whitespace-nowrap">{String(t.exit_time ?? t.exit_ts ?? '-').slice(0, 19)}</td>
                        <td className="py-1.5 pr-3 text-right tabular-nums">{Number(t.exit_price ?? 0).toFixed(2)}</td>
                        <td className="py-1.5 pr-3 text-right tabular-nums">{String(t.quantity ?? t.qty ?? '-')}</td>
                        <td className={cn('py-1.5 pr-3 text-right tabular-nums font-medium', pnl >= 0 ? 'text-emerald-500' : 'text-rose-500')}>
                          {money(pnl)}
                        </td>
                        <td className="py-1.5 text-right tabular-nums">{money(Number(t.fees ?? t.total_fees ?? 0))}</td>
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
