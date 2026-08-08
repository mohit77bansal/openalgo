/**
 * Backtest report — a dedicated page for the result of one run.
 *
 * Kept off the builder page so a long report does not sit permanently below
 * the form: the result lives in useBacktestStore, set by the builder right
 * before it navigates here. No result means the user deep-linked or reloaded,
 * so we send them back to the form rather than render an empty report.
 */
import { useMemo, useState } from 'react'
import { Navigate, useNavigate } from 'react-router'
import {
  type ColumnDef,
  type SortingState,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  useReactTable,
} from '@tanstack/react-table'
import { getMonteCarloSimulation, type MonteCarloResult } from '@/api/backtest'
import { OHLCChart } from '@/components/backtest/OHLCChart'
import { PortfolioLineChart } from '@/components/portfolio/PortfolioLineChart'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
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
    <Card className={cn(
      'border-l-4 transition-colors',
      tone === 'good' && 'border-l-emerald-500/60',
      tone === 'bad' && 'border-l-rose-500/60',
      !tone && 'border-l-border',
    )}>
      <CardContent className="p-4">
        <div className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">{label}</div>
        <div
          className={cn(
            'mt-1.5 text-2xl font-bold tabular-nums tracking-tight',
            tone === 'good' && 'text-emerald-500',
            tone === 'bad' && 'text-rose-500'
          )}
        >
          {value}
        </div>
        {sub && <div className="mt-1 text-xs text-muted-foreground">{sub}</div>}
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
  // PortfolioLineChart's toEpoch handles both bare dates (YYYY-MM-DD) and full
  // ISO timestamps, so pass the original date through — slicing to 10 chars
  // collapsed intraday points onto the same epoch second and left the chart
  // with one point per day, which could strand autoscale at the default 0..1.
  // Guard: equity may be absent when the engine only returns metrics.
  const equity = (result.equity ?? []).map((p) => ({
    date: p.date,
    value: Number(p.value) || 0,
  }))

  // Monte Carlo simulation state
  const [mcData, setMcData] = useState<MonteCarloResult | null>(null)
  const [mcLoading, setMcLoading] = useState(false)
  const [mcError, setMcError] = useState<string | null>(null)

  const handleRunMonteCarlo = async () => {
    if (!result.run_id) return
    setMcLoading(true)
    setMcError(null)
    try {
      const data = await getMonteCarloSimulation(result.run_id)
      if (data.status === 'error') {
        setMcError('Monte Carlo simulation failed')
      } else {
        setMcData(data)
      }
    } catch (err) {
      setMcError(err instanceof Error ? err.message : 'Failed to run Monte Carlo simulation')
    } finally {
      setMcLoading(false)
    }
  }

  /** Convert a trade-step index to a sequential date for the chart x-axis. */
  const tradeStepDate = (i: number): string => {
    const d = new Date(2000, 0, 1 + i)
    return d.toISOString().slice(0, 10)
  }

  const mcSeries = mcData
    ? [
        { name: 'P5 (worst likely)', color: '#dbeafe', data: mcData.percentile_curves.p5.map((v: number, i: number) => ({ date: tradeStepDate(i), value: v })) },
        { name: 'P25', color: '#93c5fd', data: mcData.percentile_curves.p25.map((v: number, i: number) => ({ date: tradeStepDate(i), value: v })) },
        { name: 'Median (P50)', color: '#1d4ed8', data: mcData.percentile_curves.p50.map((v: number, i: number) => ({ date: tradeStepDate(i), value: v })) },
        { name: 'P75', color: '#60a5fa', data: mcData.percentile_curves.p75.map((v: number, i: number) => ({ date: tradeStepDate(i), value: v })) },
        { name: 'P95 (best likely)', color: '#bfdbfe', data: mcData.percentile_curves.p95.map((v: number, i: number) => ({ date: tradeStepDate(i), value: v })) },
      ]
    : []

  // Pre-compute trade-derived stats so the JSX stays readable.
  const tradePnls: number[] = (result.trades ?? []).map((t: Record<string, unknown>) => {
    const ef = (t.entry_fill ?? {}) as Record<string, unknown>
    const xf = (t.exit_fill ?? {}) as Record<string, unknown>
    const ep = Number(ef.price ?? 0)
    const xp = Number(xf.price ?? 0)
    const dir = String(t.direction ?? '')
    return dir === 'BUY' ? xp - ep : ep - xp
  })
  const wins = tradePnls.filter((p) => p >= 0)
  const losses = tradePnls.filter((p) => p < 0)
  const winRate = tradePnls.length > 0
    ? `${((wins.length / tradePnls.length) * 100).toFixed(0)}%`
    : '-'
  const avgWin = wins.length ? wins.reduce((a, b) => a + b, 0) / wins.length : 0
  const avgLoss = losses.length ? losses.reduce((a, b) => a + b, 0) / losses.length : 0
  const riskReward = Math.abs(avgLoss) > 0 && tradePnls.length > 0
    ? `1:${(avgWin / Math.abs(avgLoss)).toFixed(1)}`
    : '-'
  const grossWin = wins.reduce((a, b) => a + b, 0)
  const grossLoss = Math.abs(losses.reduce((a, b) => a + b, 0))
  const profitFactor = grossLoss > 0 && tradePnls.length > 0
    ? (grossWin / grossLoss).toFixed(2)
    : '-'

  // Monthly PnL breakdown — group trades by exit month.
  const monthlyPnl: Record<string, number> = {}
  for (const t of (result.trades ?? []) as Record<string, unknown>[]) {
    const ef = (t.entry_fill ?? {}) as Record<string, unknown>
    const xf = (t.exit_fill ?? {}) as Record<string, unknown>
    const exitTs = String(xf.ts ?? xf.timestamp ?? t.exit_time ?? t.exit_ts ?? '')
    const month = exitTs.slice(0, 7) // YYYY-MM
    if (month.length < 7 || !month.includes('-')) continue

    const entryPrice = Number(ef.price ?? t.entry_price ?? 0)
    const exitPrice = Number(xf.price ?? t.exit_price ?? 0)
    const qty = Number(ef.quantity ?? xf.quantity ?? t.quantity ?? t.qty ?? 0)
    const dir = String(t.direction ?? t.side ?? '')
    const sign = dir === 'BUY' || dir === 'LONG' ? 1 : -1
    const entryFees = Number(ef.fees ?? 0)
    const exitFees = Number(xf.fees ?? 0)
    const totalFees = entryFees + exitFees
    const pnl = Number(
      t.pnl ?? t.realized_pnl ?? ((exitPrice - entryPrice) * qty * sign - totalFees),
    )
    if (!Number.isFinite(pnl)) continue
    monthlyPnl[month] = (monthlyPnl[month] || 0) + pnl
  }
  const monthlyEntries = Object.entries(monthlyPnl).sort(([a], [b]) => a.localeCompare(b))
  const greenMonths = monthlyEntries.filter(([, v]) => v >= 0)
  const redMonths = monthlyEntries.filter(([, v]) => v < 0)
  const bestMonthVal = monthlyEntries.length > 0
    ? Math.max(...monthlyEntries.map(([, v]) => v))
    : 0
  const worstMonthVal = monthlyEntries.length > 0
    ? Math.min(...monthlyEntries.map(([, v]) => v))
    : 0
  const avgMonthVal = monthlyEntries.length > 0
    ? monthlyEntries.reduce((sum, [, v]) => sum + v, 0) / monthlyEntries.length
    : 0
  const maxAbsPnl = Math.max(...monthlyEntries.map(([, v]) => Math.abs(v)), 1)

  return (
    <div className="container mx-auto space-y-6 p-4">
      <div className="flex flex-wrap items-center gap-4 border-b pb-4">
        <Button variant="ghost" size="sm" onClick={() => navigate('/backtest')} className="text-muted-foreground hover:text-foreground">
          &larr; Back
        </Button>
        <div className="flex-1">
          <h1 className="text-2xl font-bold tracking-tight">{result.strategy}</h1>
          <p className="text-sm text-muted-foreground">
            {result.symbol}/{result.exchange} · {result.interval} ·{' '}
            {result.source === 'db' ? 'AngelOne' : 'Synthetic'} · {result.cost_model}
            {result.start ? ` · ${result.start} → ${result.end}` : ''}
          </p>
        </div>
        <Button variant="default" size="sm" className="bg-emerald-600 hover:bg-emerald-700" onClick={() => navigate('/live-strategies')}>
          Go Live
        </Button>
      </div>

      {result.strategy_description && (
        <Card>
          <CardContent className="p-4">
            <div className="text-xs font-medium text-muted-foreground mb-1">Strategy Logic</div>
            <p className="text-sm">{result.strategy_description}</p>
            {result.strategy_source && (
              <p className="text-xs text-muted-foreground mt-2">Source: {result.strategy_source}</p>
            )}
            {result.data_note && (
              <p className="text-xs text-amber-500 mt-1">{result.data_note}</p>
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
        <Stat label="Trades" value={num(m.n_trades, 0)} sub={`Win rate: ${winRate}`} />
        <Stat label="Fees" value={money(m.fees_total)} />
        <Stat label="Risk:Reward" value={riskReward} sub="avg win : avg loss" />
        <Stat label="Profit Factor" value={profitFactor} sub="gross win / gross loss" />
        <Stat label="Capital" value={money(result.capital, 0)} sub={`${result.interval} · ${result.source === 'db' ? 'AngelOne' : 'Synthetic'}`} />
      </div>

      {result.per_instrument && result.per_instrument.length > 1 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Per-Instrument Breakdown</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-card">
                  <tr className="border-b text-left text-muted-foreground">
                    <th className="pb-2 pr-3">Instrument</th>
                    <th className="pb-2 pr-3 text-right">Capital</th>
                    <th className="pb-2 pr-3 text-right">Trades</th>
                    <th className="pb-2 pr-3 text-right">Net P&L</th>
                    <th className="pb-2 pr-3 text-right">P&L %</th>
                    <th className="pb-2 pr-3 text-right">Fees</th>
                    <th className="pb-2 pr-3 text-right">Sharpe</th>
                    <th className="pb-2 text-right">Max DD %</th>
                  </tr>
                </thead>
                <tbody>
                  {result.per_instrument.map((p) => (
                    <tr key={p.symbol} className="border-b border-border/50 last:border-0 even:bg-muted/20 hover:bg-accent/40 transition-colors">
                      <td className="py-2 pr-3 font-medium">{p.symbol.split('25')[0]}</td>
                      <td className="py-2 pr-3 text-right tabular-nums">{money(p.capital_allocated, 0)}</td>
                      <td className="py-2 pr-3 text-right tabular-nums">{p.n_trades}</td>
                      <td className={cn('py-2 pr-3 text-right tabular-nums font-medium', p.net_pnl >= 0 ? 'text-emerald-500' : 'text-rose-500')}>
                        {money(p.net_pnl, 0)}
                      </td>
                      <td className={cn('py-2 pr-3 text-right tabular-nums', p.pnl_pct >= 0 ? 'text-emerald-500' : 'text-rose-500')}>
                        {p.pnl_pct.toFixed(1)}%
                      </td>
                      <td className="py-2 pr-3 text-right tabular-nums">{money(p.fees_total, 0)}</td>
                      <td className="py-2 pr-3 text-right tabular-nums">{p.sharpe != null ? p.sharpe.toFixed(2) : '-'}</td>
                      <td className={cn('py-2 text-right tabular-nums', (p.max_drawdown_pct ?? 0) < -10 ? 'text-rose-500' : '')}>
                        {p.max_drawdown_pct != null ? `${p.max_drawdown_pct.toFixed(1)}%` : '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}

      {result.ohlc && result.ohlc.length > 0 && (!result.per_instrument || result.per_instrument.length <= 1) && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Price Chart with Trades</CardTitle>
          </CardHeader>
          <CardContent>
            <OHLCChart bars={result.ohlc} trades={result.trades} height={400} />
          </CardContent>
        </Card>
      )}

      {equity.length > 0 && (
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
      )}

      {/* Monthly PnL Breakdown */}
      {monthlyEntries.length > 1 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">PnL Breakdown</CardTitle>
            <p className="text-sm text-muted-foreground">
              Monthly profit &amp; loss across {monthlyEntries.length} months of trading.
            </p>
          </CardHeader>
          <CardContent>
            {/* Bar chart — zero line centered, green bars up, red bars down */}
            <div className="relative" style={{ height: 220 }}>
              {/* Zero line */}
              <div
                className="absolute left-0 right-0 border-t border-muted-foreground/30"
                style={{ top: '50%' }}
              />
              <div className="flex items-stretch h-full gap-px">
                {monthlyEntries.map(([month, pnl]) => {
                  const barPct = (Math.abs(pnl) / maxAbsPnl) * 48 // max 48% so it doesn't touch edge
                  const isPositive = pnl >= 0
                  return (
                    <div
                      key={month}
                      className="flex-1 relative group"
                      title={`${month}: ${money(pnl)}`}
                    >
                      {isPositive ? (
                        <div
                          className="absolute left-0.5 right-0.5 bg-emerald-500/80 rounded-t-sm transition-opacity group-hover:opacity-100 opacity-80"
                          style={{ bottom: '50%', height: `${Math.max(barPct, 1)}%` }}
                        />
                      ) : (
                        <div
                          className="absolute left-0.5 right-0.5 bg-rose-500/80 rounded-b-sm transition-opacity group-hover:opacity-100 opacity-80"
                          style={{ top: '50%', height: `${Math.max(barPct, 1)}%` }}
                        />
                      )}
                      {/* Hover tooltip */}
                      <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 hidden group-hover:block z-10">
                        <div className="bg-popover text-popover-foreground border rounded px-2 py-1 text-[10px] tabular-nums whitespace-nowrap shadow-md">
                          <span className="text-muted-foreground">{month}: </span>
                          <span className={isPositive ? 'text-emerald-500' : 'text-rose-500'}>
                            {money(pnl)}
                          </span>
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
              {/* X-axis labels — show every label if <= 12 months, else every other */}
              <div className="flex mt-1.5">
                {monthlyEntries.map(([month], idx) => {
                  const showLabel =
                    monthlyEntries.length <= 12 ||
                    idx % Math.ceil(monthlyEntries.length / 12) === 0
                  return (
                    <div
                      key={month}
                      className="flex-1 text-center text-[9px] text-muted-foreground truncate"
                    >
                      {showLabel ? month.slice(2) : ''}
                    </div>
                  )
                })}
              </div>
            </div>

            {/* Summary stats row */}
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 mt-4 pt-3 border-t text-xs">
              <div>
                <div className="text-muted-foreground">Green Months</div>
                <div className="text-emerald-500 font-semibold tabular-nums">
                  {greenMonths.length}
                </div>
              </div>
              <div>
                <div className="text-muted-foreground">Red Months</div>
                <div className="text-rose-500 font-semibold tabular-nums">
                  {redMonths.length}
                </div>
              </div>
              <div>
                <div className="text-muted-foreground">Best Month</div>
                <div className="text-emerald-500 font-semibold tabular-nums">
                  {money(bestMonthVal)}
                </div>
              </div>
              <div>
                <div className="text-muted-foreground">Worst Month</div>
                <div className="text-rose-500 font-semibold tabular-nums">
                  {money(worstMonthVal)}
                </div>
              </div>
              <div>
                <div className="text-muted-foreground">Avg Month</div>
                <div
                  className={cn(
                    'font-semibold tabular-nums',
                    avgMonthVal >= 0 ? 'text-emerald-500' : 'text-rose-500',
                  )}
                >
                  {money(avgMonthVal)}
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Monte Carlo Simulation */}
      {result.run_id != null && !mcData && (
        <Card>
          <CardContent className="flex items-center justify-between p-4">
            <div>
              <div className="text-sm font-medium">Monte Carlo Simulation</div>
              <div className="text-xs text-muted-foreground">
                Shuffle trade order {mcError ? '' : 'to estimate outcome distribution'}
              </div>
              {mcError && <div className="text-xs text-rose-500 mt-1">{mcError}</div>}
            </div>
            <Button size="sm" onClick={handleRunMonteCarlo} disabled={mcLoading}>
              {mcLoading ? 'Running...' : 'Run Monte Carlo'}
            </Button>
          </CardContent>
        </Card>
      )}

      {mcData && (
        <>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">Monte Carlo Equity Fan</CardTitle>
              <p className="text-sm text-muted-foreground">
                {mcData.n_simulations.toLocaleString()} simulations over {mcData.n_trades} trades
              </p>
            </CardHeader>
            <CardContent>
              <PortfolioLineChart
                height={340}
                format={(v) => money(v, 0)}
                series={mcSeries}
              />
            </CardContent>
          </Card>

          <div className="grid gap-3 md:grid-cols-5">
            <Stat
              label="Prob. of Profit"
              value={`${num(mcData.simulation_results.probability_of_profit, 1)}%`}
              tone={mcData.simulation_results.probability_of_profit > 60 ? 'good' : undefined}
            />
            <Stat
              label="Prob. of Ruin"
              value={`${num(mcData.simulation_results.probability_of_ruin, 1)}%`}
              tone={mcData.simulation_results.probability_of_ruin > 10 ? 'bad' : 'good'}
            />
            <Stat
              label="Median Final Equity"
              value={money(mcData.simulation_results.final_equity_median)}
              tone={mcData.simulation_results.final_equity_median >= (result.capital ?? 0) ? 'good' : 'bad'}
            />
            <Stat
              label="5th Percentile"
              value={money(mcData.simulation_results.final_equity_p5)}
              sub="worst likely"
              tone={mcData.simulation_results.final_equity_p5 >= (result.capital ?? 0) ? 'good' : 'bad'}
            />
            <Stat
              label="95th Percentile"
              value={money(mcData.simulation_results.final_equity_p95)}
              sub="best likely"
              tone="good"
            />
            <Stat
              label="Max DD (95th pct)"
              value={`${num(mcData.simulation_results.max_drawdown_p95, 1)}%`}
              tone="bad"
            />
            <Stat
              label="Win Rate"
              value={`${num(mcData.trade_stats.win_rate, 1)}%`}
              tone={mcData.trade_stats.win_rate > 50 ? 'good' : 'bad'}
            />
            <Stat
              label="Profit Factor"
              value={mcData.trade_stats.profit_factor != null ? num(mcData.trade_stats.profit_factor) : '-'}
              tone={mcData.trade_stats.profit_factor != null && mcData.trade_stats.profit_factor > 1 ? 'good' : 'bad'}
            />
            <Stat
              label="Avg Win"
              value={money(mcData.trade_stats.avg_win)}
              tone="good"
            />
            <Stat
              label="Avg Loss"
              value={money(mcData.trade_stats.avg_loss)}
              tone="bad"
            />
          </div>
        </>
      )}

      {result.trades && result.trades.length > 0 && (
        <TradesTable trades={result.trades} />
      )}
    </div>
  )
}

/* ---------------------------------------------------------------------------
 * Trades Table — TanStack Table with sort, filter, pin
 * ------------------------------------------------------------------------ */

interface ParsedTrade {
  idx: number
  instrument: string
  side: string
  entryTime: string
  entryPrice: number
  exitTime: string
  exitPrice: number
  qty: number
  pnl: number
  fees: number
  sign: number
}

const fmtTs = (ts: string) => {
  if (ts === '-') return '-'
  try { return new Date(ts).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false }) } catch { return ts.slice(0, 16) }
}

const tradeColumns: ColumnDef<ParsedTrade, unknown>[] = [
  { accessorKey: 'idx', header: '#', size: 50, cell: ({ getValue }) => <span className="text-muted-foreground">{getValue<number>()}</span> },
  { accessorKey: 'instrument', header: 'Instrument', size: 180, cell: ({ getValue }) => <span className="font-medium text-xs">{getValue<string>()}</span> },
  { accessorKey: 'side', header: 'Side', size: 60, cell: ({ row }) => <span className={cn('font-medium', row.original.sign > 0 ? 'text-emerald-500' : 'text-rose-500')}>{row.original.side}</span> },
  { accessorKey: 'entryTime', header: 'Entry Time', size: 130, cell: ({ getValue }) => <span className="text-muted-foreground whitespace-nowrap">{fmtTs(getValue<string>())}</span> },
  { accessorKey: 'entryPrice', header: 'Entry Price', size: 100, cell: ({ getValue }) => <span className="text-right tabular-nums block">{getValue<number>().toFixed(2)}</span> },
  { accessorKey: 'exitTime', header: 'Exit Time', size: 130, cell: ({ getValue }) => <span className="text-muted-foreground whitespace-nowrap">{fmtTs(getValue<string>())}</span> },
  { accessorKey: 'exitPrice', header: 'Exit Price', size: 100, cell: ({ getValue }) => <span className="text-right tabular-nums block">{getValue<number>().toFixed(2)}</span> },
  { accessorKey: 'qty', header: 'Qty', size: 60, cell: ({ getValue }) => <span className="text-right tabular-nums block">{getValue<number>()}</span> },
  { accessorKey: 'pnl', header: 'P&L', size: 120, cell: ({ row }) => <span className={cn('text-right tabular-nums font-medium block', row.original.pnl >= 0 ? 'text-emerald-500' : 'text-rose-500')}>{money(row.original.pnl)}</span>, sortingFn: 'basic' },
  { accessorKey: 'fees', header: 'Fees', size: 100, cell: ({ getValue }) => <span className="text-right tabular-nums block">{money(getValue<number>())}</span> },
]

function TradesTable({ trades }: { trades: Record<string, unknown>[] }) {
  const [sorting, setSorting] = useState<SortingState>([])
  const [globalFilter, setGlobalFilter] = useState('')

  const data = useMemo<ParsedTrade[]>(() => trades.map((t, i) => {
    const ef = (t.entry_fill ?? {}) as Record<string, unknown>
    const xf = (t.exit_fill ?? {}) as Record<string, unknown>
    const inst = (t.instrument ?? ef.instrument ?? {}) as Record<string, unknown>
    const entryPrice = Number(ef.price ?? t.entry_price ?? 0)
    const exitPrice = Number(xf.price ?? t.exit_price ?? 0)
    const qty = Number(ef.quantity ?? xf.quantity ?? t.quantity ?? t.qty ?? 0)
    const entryFees = Number(ef.fees ?? 0)
    const exitFees = Number(xf.fees ?? 0)
    const dir = String(t.direction ?? t.side ?? '-')
    const sign = dir === 'BUY' || dir === 'LONG' ? 1 : -1
    return {
      idx: i + 1,
      instrument: String(inst.symbol ?? t.instrument_id ?? '-'),
      side: dir,
      entryTime: String(ef.ts ?? ef.timestamp ?? t.entry_time ?? t.entry_ts ?? '-'),
      entryPrice,
      exitTime: String(xf.ts ?? xf.timestamp ?? t.exit_time ?? t.exit_ts ?? '-'),
      exitPrice,
      qty,
      pnl: Number(t.pnl ?? t.realized_pnl ?? ((exitPrice - entryPrice) * qty * sign - entryFees - exitFees)),
      fees: entryFees + exitFees,
      sign,
    }
  }), [trades])

  const table = useReactTable({
    data,
    columns: tradeColumns,
    state: { sorting, globalFilter, columnPinning: { left: ['idx', 'instrument', 'side'] } },
    onSortingChange: setSorting,
    onGlobalFilterChange: setGlobalFilter,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  })

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-base">Trades ({data.length})</CardTitle>
          <Input placeholder="Filter instrument..." value={globalFilter} onChange={(e) => setGlobalFilter(e.target.value)} className="h-8 w-48 text-xs" />
        </div>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto max-h-[500px] overflow-y-auto">
          <table className="w-full text-xs border-separate border-spacing-0">
            <thead className="sticky top-0 z-10 bg-card">
              {table.getHeaderGroups().map((hg) => (
                <tr key={hg.id} className="border-b text-left text-muted-foreground">
                  {hg.headers.map((h) => {
                    const isPinned = h.column.getIsPinned()
                    return (
                      <th key={h.id} className={cn('pb-2 pr-3 cursor-pointer select-none whitespace-nowrap', isPinned && 'sticky bg-card z-20')}
                        style={isPinned ? { left: `${h.column.getStart('left')}px`, position: 'sticky' } : undefined}
                        onClick={h.column.getToggleSortingHandler()}>
                        {flexRender(h.column.columnDef.header, h.getContext())}
                        {h.column.getIsSorted() === 'asc' ? ' ▲' : h.column.getIsSorted() === 'desc' ? ' ▼' : ''}
                      </th>
                    )
                  })}
                </tr>
              ))}
            </thead>
            <tbody>
              {table.getRowModel().rows.map((row) => (
                <tr key={row.id} className="border-b border-border/50 last:border-0 even:bg-muted/20 hover:bg-accent/40 transition-colors">
                  {row.getVisibleCells().map((cell) => {
                    const isPinned = cell.column.getIsPinned()
                    return (
                      <td key={cell.id} className={cn('py-1.5 pr-3', isPinned && 'sticky bg-card z-10')}
                        style={isPinned ? { left: `${cell.column.getStart('left')}px`, position: 'sticky' } : undefined}>
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  )
}
