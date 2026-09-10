import { useState } from 'react'
import { getMonteCarloSimulation, type MonteCarloResult } from '@/api/backtest'
import { PortfolioLineChart } from '@/components/portfolio/PortfolioLineChart'
import { StatCard } from '@/components/backtest/StatCard'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

const money = (v: number | null | undefined, dp = 0) =>
  v === null || v === undefined
    ? '-'
    : `₹${v.toLocaleString('en-IN', { maximumFractionDigits: dp })}`
const num = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined ? '-' : v.toFixed(dp)

function tradeStepDate(i: number): string {
  const d = new Date(2000, 0, 1 + i)
  return d.toISOString().slice(0, 10)
}

export function MonteCarloSection({ runId, capital }: { runId: number; capital: number }) {
  const [mcData, setMcData] = useState<MonteCarloResult | null>(null)
  const [mcLoading, setMcLoading] = useState(false)
  const [mcError, setMcError] = useState<string | null>(null)

  const handleRunMonteCarlo = async () => {
    setMcLoading(true)
    setMcError(null)
    try {
      const data = await getMonteCarloSimulation(runId)
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

  const mcSeries = mcData
    ? [
        { name: 'P5 (worst likely)', color: '#fca5a5', data: mcData.percentile_curves.p5.map((v: number, i: number) => ({ date: tradeStepDate(i), value: v })) },
        { name: 'P25', color: '#93c5fd', data: mcData.percentile_curves.p25.map((v: number, i: number) => ({ date: tradeStepDate(i), value: v })) },
        { name: 'Median (P50)', color: '#3b82f6', data: mcData.percentile_curves.p50.map((v: number, i: number) => ({ date: tradeStepDate(i), value: v })) },
        { name: 'P75', color: '#60a5fa', data: mcData.percentile_curves.p75.map((v: number, i: number) => ({ date: tradeStepDate(i), value: v })) },
        { name: 'P95 (best likely)', color: '#86efac', data: mcData.percentile_curves.p95.map((v: number, i: number) => ({ date: tradeStepDate(i), value: v })) },
      ]
    : []

  if (!mcData) {
    return (
      <Card>
        <CardContent className="flex items-center justify-between p-4">
          <div>
            <div className="text-sm font-medium">Monte Carlo Simulation</div>
            <div className="text-xs text-muted-foreground">
              {mcError ? '' : 'Block bootstrap with 1,000 simulations. Hedge-fund methodology.'}
            </div>
            {mcError && <div className="text-xs text-rose-500 mt-1">{mcError}</div>}
          </div>
          <Button size="sm" onClick={handleRunMonteCarlo} disabled={mcLoading}>
            {mcLoading ? 'Running...' : 'Run Monte Carlo'}
          </Button>
        </CardContent>
      </Card>
    )
  }

  const sim = mcData.simulation_results
  const ts = mcData.trade_stats
  const method = (mcData as unknown as Record<string, unknown>).method ?? 'block'

  return (
    <>
      <Card>
        <CardHeader className="pb-2">
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-base">Monte Carlo Equity Fan</CardTitle>
              <p className="text-xs text-muted-foreground mt-0.5">
                {mcData.n_simulations.toLocaleString()} sims x {mcData.n_trades} trades ({String(method)} bootstrap)
              </p>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <PortfolioLineChart
            height={300}
            format={(v) => money(v)}
            series={mcSeries}
          />
        </CardContent>
      </Card>

      {/* Risk metrics - hedge fund style */}
      <div className="rounded-lg border bg-card p-3">
        <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground mb-2">Outcome Distribution</div>
        <div className="grid grid-cols-5 gap-x-4 gap-y-2">
          <StatCard
            label="Prob. of Profit"
            value={`${num(sim.probability_of_profit, 1)}%`}
            tone={sim.probability_of_profit > 60 ? 'good' : sim.probability_of_profit < 40 ? 'bad' : undefined}
          />
          <StatCard
            label="Prob. of Ruin"
            value={`${num(sim.probability_of_ruin, 1)}%`}
            tone={sim.probability_of_ruin > 10 ? 'bad' : 'good'}
          />
          <StatCard
            label="Median Equity"
            value={money(sim.final_equity_median)}
            tone={sim.final_equity_median >= capital ? 'good' : 'bad'}
          />
          <StatCard label="5th Pctl" value={money(sim.final_equity_p5)} sub="worst likely" tone="bad" />
          <StatCard label="95th Pctl" value={money(sim.final_equity_p95)} sub="best likely" tone="good" />
        </div>
      </div>

      <div className="rounded-lg border bg-card p-3">
        <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground mb-2">Risk Metrics</div>
        <div className="grid grid-cols-5 gap-x-4 gap-y-2">
          <StatCard
            label="CVaR (5%)"
            value={money((sim as unknown as Record<string, unknown>).cvar_5pct as number)}
            sub="expected loss, worst 5%"
            tone="bad"
          />
          <StatCard
            label="Max DD (95th)"
            value={`${num(sim.max_drawdown_p95, 1)}%`}
            tone="bad"
          />
          <StatCard
            label="Underwater (med)"
            value={`${(sim as unknown as Record<string, unknown>).max_underwater_median ?? '-'} bars`}
            sub="consecutive bars in DD"
          />
          <StatCard
            label="Calmar Ratio"
            value={num((sim as unknown as Record<string, unknown>).calmar_ratio as number)}
            sub="return / max DD"
            tone={((sim as unknown as Record<string, unknown>).calmar_ratio as number) > 1 ? 'good' : undefined}
          />
          <StatCard
            label="Sortino"
            value={num((ts as unknown as Record<string, unknown>).sortino_ratio as number)}
            sub="downside-adj return"
            tone={((ts as unknown as Record<string, unknown>).sortino_ratio as number) > 1 ? 'good' : undefined}
          />
        </div>
      </div>

      <div className="rounded-lg border bg-card p-3">
        <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground mb-2">Edge Analysis</div>
        <div className="grid grid-cols-5 gap-x-4 gap-y-2">
          <StatCard
            label="Win Rate"
            value={`${num(ts.win_rate, 1)}%`}
            tone={ts.win_rate > 50 ? 'good' : 'bad'}
          />
          <StatCard
            label="Profit Factor"
            value={ts.profit_factor != null ? num(ts.profit_factor) : '-'}
            tone={ts.profit_factor != null && ts.profit_factor > 1 ? 'good' : 'bad'}
          />
          <StatCard
            label="Kelly Fraction"
            value={`${num(((ts as unknown as Record<string, unknown>).kelly_fraction as number) * 100, 1)}%`}
            sub="optimal bet size"
            tone={((ts as unknown as Record<string, unknown>).kelly_fraction as number) > 0 ? 'good' : 'bad'}
          />
          <StatCard
            label="Edge Z-Score"
            value={num((ts as unknown as Record<string, unknown>).edge_z_score as number)}
            sub={(ts as unknown as Record<string, unknown>).edge_significant ? 'statistically significant' : 'not significant'}
            tone={(ts as unknown as Record<string, unknown>).edge_significant ? 'good' : 'bad'}
          />
          <StatCard
            label="Avg P&L/Trade"
            value={money(ts.avg_pnl_per_trade, 2)}
            tone={ts.avg_pnl_per_trade > 0 ? 'good' : 'bad'}
          />
        </div>
      </div>
    </>
  )
}
