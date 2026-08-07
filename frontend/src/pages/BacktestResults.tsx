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
    </div>
  )
}
