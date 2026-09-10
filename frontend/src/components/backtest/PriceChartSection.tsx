import { useMemo, useState } from 'react'
import { CandlestickChart, Maximize2, Minimize2 } from 'lucide-react'
import { OHLCChart } from '@/components/backtest/OHLCChart'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { cn } from '@/lib/utils'

const INTERVAL_LABELS: Record<string, string> = {
  '1m': '1 min', '5m': '5 min', '15m': '15 min', '30m': '30 min',
  '1h': '1 hour', 'D': 'Daily', '1d': 'Daily',
}

export function PriceChartSection({ ohlc, trades, interval, symbol, nBars }: {
  ohlc: { time: string; open: number; high: number; low: number; close: number }[]
  trades: Record<string, unknown>[]
  interval?: string
  symbol?: string
  nBars?: number
}) {
  const instruments = useMemo(() => {
    const set = new Set<string>()
    for (const t of trades) {
      const inst = (t.instrument ?? (t.entry_fill as Record<string, unknown>)?.instrument ?? {}) as Record<string, unknown>
      const sym = String(inst.symbol ?? '')
      if (sym) set.add(sym)
    }
    return Array.from(set).sort()
  }, [trades])

  const [selectedInstrument, setSelectedInstrument] = useState(instruments[0] ?? '')
  const [expanded, setExpanded] = useState(false)

  const filteredTrades = useMemo(() => {
    if (!selectedInstrument || instruments.length <= 1) return trades
    return trades.filter((t) => {
      const inst = (t.instrument ?? (t.entry_fill as Record<string, unknown>)?.instrument ?? {}) as Record<string, unknown>
      return String(inst.symbol ?? '') === selectedInstrument
    })
  }, [trades, selectedInstrument, instruments])

  const buyCount = filteredTrades.filter(t => {
    const dir = String((t as Record<string, unknown>).direction ?? '')
    return dir === 'BUY' || dir === 'LONG'
  }).length
  const sellCount = filteredTrades.length - buyCount

  if (!ohlc || ohlc.length === 0) return null

  const displaySymbol = (selectedInstrument || symbol || '').replace(/\d{2}[A-Z]{3}\d{2}FUT$/, '').replace(/\d{2}[A-Z]{3}\d{2}/, '')
  const intervalLabel = INTERVAL_LABELS[interval ?? ''] ?? interval ?? ''

  return (
    <Card>
      {/* TradingView-style toolbar */}
      <div className="flex items-center justify-between border-b px-4 py-2">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1.5">
            <CandlestickChart className="h-4 w-4 text-muted-foreground" />
            <span className="font-semibold text-sm">{displaySymbol || 'Chart'}</span>
          </div>

          {intervalLabel && (
            <Badge variant="secondary" className="text-[10px] font-mono px-2 py-0.5">
              {intervalLabel}
            </Badge>
          )}

          <div className="h-4 w-px bg-border" />

          <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
            <span>{ohlc.length.toLocaleString()} bars</span>
            {nBars && nBars !== ohlc.length && (
              <span>({nBars.toLocaleString()} total)</span>
            )}
          </div>

          {filteredTrades.length > 0 && (
            <>
              <div className="h-4 w-px bg-border" />
              <div className="flex items-center gap-2 text-[11px]">
                <span className="text-emerald-500 font-medium">{buyCount} buys</span>
                <span className="text-rose-500 font-medium">{sellCount} sells</span>
              </div>
            </>
          )}
        </div>

        <div className="flex items-center gap-2">
          {instruments.length > 1 && (
            <select
              value={selectedInstrument}
              onChange={(e) => setSelectedInstrument(e.target.value)}
              className="h-7 rounded border bg-background px-2 text-[11px]"
            >
              {instruments.map((inst) => (
                <option key={inst} value={inst}>
                  {inst.replace(/\d{2}[A-Z]{3}\d{2}FUT$/, '').replace(/\d{2}[A-Z]{3}\d{2}/, '')}
                </option>
              ))}
            </select>
          )}

          <button
            onClick={() => setExpanded(e => !e)}
            className="p-1 rounded hover:bg-accent text-muted-foreground"
            title={expanded ? 'Collapse chart' : 'Expand chart'}
          >
            {expanded ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
          </button>
        </div>
      </div>

      {instruments.length > 1 && (
        <div className="px-4 pt-1.5">
          <p className="text-[10px] text-muted-foreground">
            Showing {(selectedInstrument || '').replace(/\d{2}[A-Z]{3}\d{2}FUT$/, '')} {intervalLabel} candles. Trade markers filtered to this instrument.
          </p>
        </div>
      )}

      <CardContent className={cn('p-2', expanded && 'p-0')}>
        <OHLCChart bars={ohlc} trades={filteredTrades} height={expanded ? 600 : 450} />
      </CardContent>
    </Card>
  )
}
