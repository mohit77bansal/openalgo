import { useMemo, useState } from 'react'
import { BarChart3, LineChart, TrendingDown, TrendingUp } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

const money = (v: number | null | undefined, dp = 0) =>
  v === null || v === undefined
    ? '-'
    : `₹${v.toLocaleString('en-IN', { maximumFractionDigits: dp })}`

type Granularity = 'daily' | 'weekly' | 'monthly' | 'quarterly'
type ChartType = 'bar' | 'line'
type Metric = 'pnl' | 'fees' | 'trades' | 'gross_win' | 'gross_loss' | 'cumulative' | 'balance'

const METRIC_LABELS: Record<Metric, string> = {
  pnl: 'Net P&L', fees: 'Fees', trades: 'Trades',
  gross_win: 'Gross Win', gross_loss: 'Gross Loss', cumulative: 'Cumulative P&L', balance: 'Balance',
}

const METRIC_COLORS: Record<Metric, { pos: string; neg: string }> = {
  pnl: { pos: 'rgb(34,197,94)', neg: 'rgb(239,68,68)' },
  fees: { pos: 'rgb(99,102,241)', neg: 'rgb(99,102,241)' },
  trades: { pos: 'rgb(59,130,246)', neg: 'rgb(59,130,246)' },
  gross_win: { pos: 'rgb(34,197,94)', neg: 'rgb(34,197,94)' },
  gross_loss: { pos: 'rgb(239,68,68)', neg: 'rgb(239,68,68)' },
  cumulative: { pos: 'rgb(34,197,94)', neg: 'rgb(239,68,68)' },
  balance: { pos: 'rgb(59,130,246)', neg: 'rgb(239,68,68)' },
}

interface BucketData {
  key: string
  label: string
  pnl: number
  fees: number
  trades: number
  gross_win: number
  gross_loss: number
  cumulative: number
  balance: number
}

function parseTrade(t: Record<string, unknown>) {
  const ef = (t.entry_fill ?? {}) as Record<string, unknown>
  const xf = (t.exit_fill ?? {}) as Record<string, unknown>
  const exitTs = String(xf.ts ?? xf.timestamp ?? t.exit_time ?? t.exit_ts ?? '')
  const entryPrice = Number(ef.price ?? t.entry_price ?? 0)
  const exitPrice = Number(xf.price ?? t.exit_price ?? 0)
  const qty = Number(ef.quantity ?? xf.quantity ?? t.quantity ?? t.qty ?? 0)
  const dir = String(t.direction ?? t.side ?? '')
  const sign = dir === 'BUY' || dir === 'LONG' ? 1 : -1
  const entryFees = Number(ef.fees ?? 0)
  const exitFees = Number(xf.fees ?? 0)
  const fees = entryFees + exitFees
  const pnl = Number(t.pnl ?? t.realized_pnl ?? ((exitPrice - entryPrice) * qty * sign - fees))
  return { exitTs, pnl: Number.isFinite(pnl) ? pnl : 0, fees, isWin: pnl >= 0 }
}

function getWeekKey(dateStr: string): string {
  const d = new Date(dateStr)
  const jan1 = new Date(d.getFullYear(), 0, 1)
  const week = Math.ceil(((d.getTime() - jan1.getTime()) / 86400000 + jan1.getDay() + 1) / 7)
  return `${d.getFullYear()}-W${String(week).padStart(2, '0')}`
}

function getQuarterKey(dateStr: string): string {
  const y = dateStr.slice(0, 4)
  const m = parseInt(dateStr.slice(5, 7), 10)
  return `${y}-Q${Math.ceil(m / 3)}`
}

function bucketKey(exitTs: string, gran: Granularity): string | null {
  if (!exitTs || exitTs.length < 10) return null
  const dateStr = exitTs.slice(0, 10)
  switch (gran) {
    case 'daily': return dateStr
    case 'weekly': return getWeekKey(dateStr)
    case 'monthly': return dateStr.slice(0, 7)
    case 'quarterly': return getQuarterKey(dateStr)
  }
}

function bucketLabel(key: string, gran: Granularity): string {
  switch (gran) {
    case 'daily': return key.slice(5)
    case 'weekly': return key.slice(2)
    case 'monthly': return key.slice(2)
    case 'quarterly': return key.slice(2)
  }
}

function buildBuckets(trades: Record<string, unknown>[], gran: Granularity, startingCapital: number): BucketData[] {
  const map: Record<string, Omit<BucketData, 'label' | 'cumulative' | 'balance'>> = {}
  for (const t of trades) {
    const { exitTs, pnl, fees, isWin } = parseTrade(t)
    const k = bucketKey(exitTs, gran)
    if (!k) continue
    if (!map[k]) map[k] = { key: k, pnl: 0, fees: 0, trades: 0, gross_win: 0, gross_loss: 0 }
    map[k].pnl += pnl
    map[k].fees += fees
    map[k].trades += 1
    if (isWin) map[k].gross_win += pnl
    else map[k].gross_loss += Math.abs(pnl)
  }
  const sorted = Object.values(map).sort((a, b) => a.key.localeCompare(b.key))
  let cum = 0
  return sorted.map(b => {
    cum += b.pnl
    return { ...b, label: bucketLabel(b.key, gran), cumulative: cum, balance: startingCapital + cum }
  })
}

function BarChartView({ data, metric }: { data: BucketData[]; metric: Metric }) {
  const vals = data.map(d => d[metric])
  const maxAbs = Math.max(...vals.map(v => Math.abs(v)), 1)
  const hasNeg = vals.some(v => v < 0)
  const colors = METRIC_COLORS[metric]

  return (
    <div className="relative" style={{ height: 200 }}>
      {hasNeg && (
        <div className="absolute left-0 right-0 border-t border-muted-foreground/20" style={{ top: '50%' }} />
      )}
      <div className="flex items-stretch h-full gap-px">
        {data.map((d) => {
          const val = d[metric]
          const barPct = hasNeg ? (Math.abs(val) / maxAbs) * 48 : (val / maxAbs) * 96
          const isPos = val >= 0
          const color = isPos ? colors.pos : colors.neg
          return (
            <div key={d.key} className="flex-1 relative group" title={`${d.label}: ${metric === 'trades' ? val : money(val)}`}>
              {hasNeg ? (
                isPos ? (
                  <div className="absolute left-0.5 right-0.5 rounded-t-sm opacity-80 group-hover:opacity-100 transition-opacity"
                    style={{ bottom: '50%', height: `${Math.max(barPct, 1)}%`, backgroundColor: color }} />
                ) : (
                  <div className="absolute left-0.5 right-0.5 rounded-b-sm opacity-80 group-hover:opacity-100 transition-opacity"
                    style={{ top: '50%', height: `${Math.max(barPct, 1)}%`, backgroundColor: color }} />
                )
              ) : (
                <div className="absolute left-0.5 right-0.5 rounded-t-sm opacity-80 group-hover:opacity-100 transition-opacity"
                  style={{ bottom: 0, height: `${Math.max(barPct, 1)}%`, backgroundColor: color }} />
              )}
              <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 hidden group-hover:block z-10">
                <div className="bg-popover text-popover-foreground border rounded px-2 py-1 text-[10px] tabular-nums whitespace-nowrap shadow-md">
                  <span className="text-muted-foreground">{d.label}: </span>
                  <span style={{ color }}>{metric === 'trades' ? val : money(val)}</span>
                </div>
              </div>
            </div>
          )
        })}
      </div>
      <div className="flex mt-1">
        {data.map((d, idx) => {
          const show = data.length <= 15 || idx % Math.ceil(data.length / 15) === 0
          return (
            <div key={d.key} className="flex-1 text-center text-[8px] text-muted-foreground truncate">
              {show ? d.label : ''}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function LineChartView({ data, metric }: { data: BucketData[]; metric: Metric }) {
  const vals = data.map(d => d[metric])
  const minVal = Math.min(...vals)
  const maxVal = Math.max(...vals)
  const range = maxVal - minVal || 1
  const w = 600
  const h = 180
  const pad = 4
  const colors = METRIC_COLORS[metric]

  const points = data.map((d, i) => {
    const x = pad + (i / Math.max(data.length - 1, 1)) * (w - 2 * pad)
    const y = h - pad - ((d[metric] - minVal) / range) * (h - 2 * pad)
    return { x, y, d }
  })

  const pathD = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')
  const zeroY = minVal < 0 ? h - pad - ((0 - minVal) / range) * (h - 2 * pad) : h - pad
  const areaD = `${pathD} L${points[points.length - 1].x.toFixed(1)},${zeroY.toFixed(1)} L${points[0].x.toFixed(1)},${zeroY.toFixed(1)} Z`
  const lastVal = vals[vals.length - 1]
  const lineColor = lastVal >= 0 ? colors.pos : colors.neg

  return (
    <div style={{ height: 200 }}>
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full" style={{ height: 180 }}>
        {minVal < 0 && <line x1={pad} y1={zeroY} x2={w - pad} y2={zeroY} stroke="currentColor" strokeOpacity={0.15} strokeDasharray="4 4" />}
        <path d={areaD} fill={lineColor} fillOpacity={0.08} />
        <path d={pathD} fill="none" stroke={lineColor} strokeWidth={2} />
        {points.map((p, i) => (
          <circle key={i} cx={p.x} cy={p.y} r={3} fill={lineColor} className="opacity-0 hover:opacity-100 transition-opacity cursor-crosshair">
            <title>{p.d.label}: {metric === 'trades' ? p.d[metric] : money(p.d[metric])}</title>
          </circle>
        ))}
      </svg>
      <div className="flex mt-0.5">
        {data.map((d, idx) => {
          const show = data.length <= 15 || idx % Math.ceil(data.length / 15) === 0
          return (
            <div key={d.key} className="flex-1 text-center text-[8px] text-muted-foreground truncate">
              {show ? d.label : ''}
            </div>
          )
        })}
      </div>
    </div>
  )
}

export function MonthlyPnlChart({ trades, capital = 0 }: { trades: Record<string, unknown>[]; capital?: number }) {
  const [gran, setGran] = useState<Granularity>('monthly')
  const [chartType, setChartType] = useState<ChartType>('bar')
  const [metrics, setMetrics] = useState<Metric[]>(['pnl'])

  const data = useMemo(() => buildBuckets(trades, gran, capital), [trades, gran, capital])

  if (data.length <= 1) return null

  const green = data.filter(d => d.pnl >= 0).length
  const red = data.filter(d => d.pnl < 0).length
  const best = Math.max(...data.map(d => d.pnl))
  const worst = Math.min(...data.map(d => d.pnl))
  const avg = data.reduce((s, d) => s + d.pnl, 0) / data.length
  const granLabel = { daily: 'days', weekly: 'weeks', monthly: 'months', quarterly: 'quarters' }[gran]

  const toggleMetric = (m: Metric) => {
    setMetrics(prev => prev.includes(m) ? (prev.length > 1 ? prev.filter(x => x !== m) : prev) : [...prev, m])
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <div>
            <CardTitle className="text-base">PnL Breakdown</CardTitle>
            <p className="text-xs text-muted-foreground mt-0.5">
              {data.length} {granLabel} of trading
            </p>
          </div>

          <div className="flex items-center gap-2">
            {/* Granularity toggle */}
            <div className="flex items-center border rounded-md overflow-hidden text-[10px]">
              {(['daily', 'weekly', 'monthly', 'quarterly'] as Granularity[]).map(g => (
                <button key={g} onClick={() => setGran(g)}
                  className={cn('px-2 py-1 transition-colors', gran === g ? 'bg-accent text-accent-foreground font-medium' : 'text-muted-foreground hover:bg-muted/50')}>
                  {g.charAt(0).toUpperCase() + g.slice(1, 3)}
                </button>
              ))}
            </div>

            {/* Chart type toggle */}
            <div className="flex items-center border rounded-md overflow-hidden">
              <button onClick={() => setChartType('bar')}
                className={cn('p-1 transition-colors', chartType === 'bar' ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:bg-muted/50')}
                title="Bar chart">
                <BarChart3 className="h-3.5 w-3.5" />
              </button>
              <button onClick={() => setChartType('line')}
                className={cn('p-1 transition-colors', chartType === 'line' ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:bg-muted/50')}
                title="Line chart">
                <LineChart className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        </div>

        {/* Metric pills */}
        <div className="flex flex-wrap gap-1 mt-2">
          {(Object.keys(METRIC_LABELS) as Metric[]).map(m => (
            <button key={m} onClick={() => toggleMetric(m)}
              className={cn(
                'text-[10px] px-2 py-0.5 rounded-full border transition-colors',
                metrics.includes(m)
                  ? 'bg-foreground/10 border-foreground/20 text-foreground font-medium'
                  : 'border-border text-muted-foreground hover:border-foreground/20',
              )}>
              {METRIC_LABELS[m]}
            </button>
          ))}
        </div>
      </CardHeader>

      <CardContent>
        {/* Render one chart per selected metric */}
        {metrics.map(m => (
          <div key={m} className={metrics.length > 1 ? 'mb-4' : ''}>
            {metrics.length > 1 && (
              <div className="text-[10px] font-medium text-muted-foreground uppercase tracking-wider mb-1">{METRIC_LABELS[m]}</div>
            )}
            {chartType === 'bar'
              ? <BarChartView data={data} metric={m} />
              : <LineChartView data={data} metric={m} />
            }
          </div>
        ))}

        {/* Summary stats */}
        <div className="grid grid-cols-5 gap-3 mt-3 pt-2 border-t text-xs">
          <div>
            <div className="text-muted-foreground">Green</div>
            <div className="text-emerald-500 font-semibold tabular-nums flex items-center gap-1">
              <TrendingUp className="h-3 w-3" /> {green}
            </div>
          </div>
          <div>
            <div className="text-muted-foreground">Red</div>
            <div className="text-rose-500 font-semibold tabular-nums flex items-center gap-1">
              <TrendingDown className="h-3 w-3" /> {red}
            </div>
          </div>
          <div>
            <div className="text-muted-foreground">Best</div>
            <div className="text-emerald-500 font-semibold tabular-nums">{money(best)}</div>
          </div>
          <div>
            <div className="text-muted-foreground">Worst</div>
            <div className="text-rose-500 font-semibold tabular-nums">{money(worst)}</div>
          </div>
          <div>
            <div className="text-muted-foreground">Avg</div>
            <div className={cn('font-semibold tabular-nums', avg >= 0 ? 'text-emerald-500' : 'text-rose-500')}>
              {money(avg)}
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}
