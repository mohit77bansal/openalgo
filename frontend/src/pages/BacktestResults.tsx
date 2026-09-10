import { useEffect, useState } from 'react'
import { Navigate, useNavigate, useParams } from 'react-router'
import { getBacktestDetail } from '@/api/backtest'
import { InstrumentBreakdown } from '@/components/backtest/InstrumentBreakdown'
import { MonteCarloSection } from '@/components/backtest/MonteCarloSection'
import { MonthlyPnlChart } from '@/components/backtest/MonthlyPnlChart'
import { PriceChartSection } from '@/components/backtest/PriceChartSection'
import { StatCard } from '@/components/backtest/StatCard'
import { TradesTable } from '@/components/backtest/TradesTable'
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

export default function BacktestResults() {
  const navigate = useNavigate()
  const { runId: urlRunId } = useParams<{ runId: string }>()
  const { result: storeResult, setResult } = useBacktestStore()
  const [loading, setLoading] = useState(false)
  const [result, setLocalResult] = useState(storeResult)

  useEffect(() => {
    if (result?.metrics) return
    if (!urlRunId) return
    setLoading(true)
    getBacktestDetail(Number(urlRunId)).then((detail) => {
      if (detail.equity?.length || detail.metrics) {
        setLocalResult(detail)
        setResult(detail, { source: 'db' as const, symbol: detail.symbol, exchange: detail.exchange || 'NFO',
          interval: detail.interval || '15m', start: detail.start || '', end: detail.end || '',
          capital: detail.capital, cost: detail.cost_model })
      }
    }).catch(() => {}).finally(() => setLoading(false))
  }, [urlRunId, result, setResult])

  useEffect(() => { if (storeResult?.metrics) setLocalResult(storeResult) }, [storeResult])

  if (loading) return <div className="container mx-auto p-4"><p className="text-sm text-muted-foreground">Loading backtest results...</p></div>

  if (!result || !result.metrics) {
    if (urlRunId) return <div className="container mx-auto p-4"><p className="text-sm text-muted-foreground">Backtest run not found.</p></div>
    return <Navigate to="/backtest" replace />
  }

  const m = result.metrics
  const equity = (result.equity ?? []).map((p) => ({
    date: p.date,
    value: Number(p.value) || 0,
  }))

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

  const runId = result.run_id ?? (result as unknown as Record<string, unknown>).id as number | undefined

  return (
    <div className="container mx-auto space-y-6 p-4">
      <div className="flex flex-wrap items-center gap-4 border-b pb-4">
        <Button variant="ghost" size="sm" onClick={() => navigate('/backtest')} className="text-muted-foreground hover:text-foreground">
          &larr; Back
        </Button>
        <div className="flex-1">
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-bold tracking-tight">{result.strategy}</h1>
            {(() => {
              const itype = (result as unknown as Record<string, unknown>).instrument_type as string | undefined
              if (!itype) return null
              const isOpt = itype.includes('Option')
              return <span className={cn('text-[10px] px-2 py-0.5 rounded-full font-medium', isOpt ? 'bg-purple-500/10 text-purple-500' : 'bg-blue-500/10 text-blue-500')}>{itype}</span>
            })()}
          </div>
          <p className="text-sm text-muted-foreground">
            {result.symbol}/{result.exchange} · {result.interval} ·{' '}
            {result.source === 'db' ? 'AngelOne' : 'Synthetic'} · {result.cost_model}
            {result.start ? ` · ${result.start} → ${result.end}` : ''}
          </p>
        </div>
        <Button variant="default" size="sm" className="bg-emerald-600 hover:bg-emerald-700" onClick={() => {
          const stratKey = (result as unknown as Record<string, unknown>).strategy_key ?? result.strategy?.toLowerCase().replace(/[^a-z0-9]+/g, '_') ?? ''
          navigate(`/live-strategies?strategy=${encodeURIComponent(String(stratKey))}&symbol=${encodeURIComponent(result.symbol)}`)
        }}>
          Go Live
        </Button>
      </div>

      {result.strategy_description && (
        <div className="text-xs text-muted-foreground border-l-2 border-border pl-3 py-1">
          <span className="font-medium">Logic:</span> {result.strategy_description}
          {result.strategy_source && <span className="ml-2 text-muted-foreground/60">({result.strategy_source})</span>}
          {result.data_note && <span className="ml-2 text-amber-500">{result.data_note}</span>}
        </div>
      )}

      <div className="grid grid-cols-5 gap-x-4 gap-y-2 rounded-lg border bg-card p-3">
        <StatCard
          label="Net P&L"
          value={money(m.net_pnl)}
          sub={result.capital ? `${(((m.net_pnl ?? 0) / result.capital) * 100).toFixed(1)}% of capital` : undefined}
          tone={(m.net_pnl ?? 0) >= 0 ? 'good' : 'bad'}
        />
        <StatCard label="Sharpe" value={num(m.sharpe)} />
        <StatCard label="Max DD" value={`${num(m.max_drawdown_pct)}%`} tone="bad" />
        <StatCard label="Trades" value={num(m.n_trades, 0)} sub={`Win ${winRate}`} />
        <StatCard
          label="XIRR"
          value={result.xirr_pct != null ? `${result.xirr_pct.toFixed(1)}%` : '-'}
          tone={result.xirr_pct != null ? ((result.xirr_pct ?? 0) >= 0 ? 'good' : 'bad') : undefined}
        />
        <StatCard
          label="RoM"
          value={result.return_on_margin_pct != null ? `${result.return_on_margin_pct.toFixed(1)}%` : '-'}
          sub={result.margin_used ? `${money(result.margin_used, 0)}/lot` : undefined}
          tone={result.return_on_margin_pct != null ? ((result.return_on_margin_pct ?? 0) >= 0 ? 'good' : 'bad') : undefined}
        />
        <StatCard label="Fees" value={money(m.fees_total)} />
        <StatCard label="R:R" value={riskReward} sub={profitFactor !== '-' ? `PF ${profitFactor}` : undefined} />
        <StatCard label="Capital" value={money(result.capital, 0)} sub={`${result.interval} · ${result.source === 'db' ? 'AngelOne' : 'Synth'}`} />
      </div>

      {result.per_instrument && result.per_instrument.length > 1 && (
        <InstrumentBreakdown data={result.per_instrument} />
      )}

      <PriceChartSection
        ohlc={result.ohlc ?? []}
        trades={result.trades ?? []}
        interval={result.interval}
        symbol={result.symbol}
        nBars={result.n_bars}
      />

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

      <MonthlyPnlChart trades={(result.trades ?? []) as Record<string, unknown>[]} capital={result.capital ?? 0} />

      {runId != null && (
        <MonteCarloSection runId={runId} capital={result.capital ?? 0} />
      )}

      {result.trades && result.trades.length > 0 && (
        <TradesTable trades={result.trades} />
      )}
    </div>
  )
}
