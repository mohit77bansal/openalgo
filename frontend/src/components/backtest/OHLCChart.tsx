/**
 * Candlestick chart with buy/sell trade markers for backtest results.
 *
 * Uses lightweight-charts v5 directly (not openalgo-charts) because the
 * candlestick series and series-marker plugin are only in the upstream lib.
 * Theme colours are read from the same CSS tokens the rest of the app uses.
 */
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useEffect, useRef } from 'react'
import { useThemeStore } from '@/stores/themeStore'

// ── Types ──────────────────────────────────────────────────────────────────
export interface OHLCBar {
  time: string
  open: number
  high: number
  low: number
  close: number
}

export interface OHLCChartProps {
  bars: OHLCBar[]
  trades?: Record<string, unknown>[]
  height?: number
}

// ── Helpers ────────────────────────────────────────────────────────────────
/** ISO timestamp -> epoch seconds (lightweight-charts indexes on seconds). */
const toEpoch = (iso: string): number => Math.floor(new Date(iso).getTime() / 1000)

/**
 * Build lightweight-charts markers from the raw trade objects the engine
 * returns. Entry BUY = green arrow up below bar, entry SELL = red arrow down
 * above bar. Exits are shown as grey markers.
 */
function buildMarkers(trades: Record<string, unknown>[]): SeriesMarker<UTCTimestamp>[] {
  const markers: SeriesMarker<UTCTimestamp>[] = []

  for (const t of trades) {
    const ef = (t.entry_fill ?? {}) as Record<string, unknown>
    const xf = (t.exit_fill ?? {}) as Record<string, unknown>
    const dir = String(t.direction ?? t.side ?? '')
    const isBuy = dir === 'BUY' || dir === 'LONG'

    // Entry marker
    const entryTs = String(ef.ts ?? ef.timestamp ?? t.entry_time ?? t.entry_ts ?? '')
    if (entryTs) {
      markers.push({
        time: toEpoch(entryTs) as UTCTimestamp,
        position: isBuy ? 'belowBar' : 'aboveBar',
        color: isBuy ? '#22c55e' : '#ef4444',
        shape: isBuy ? 'arrowUp' : 'arrowDown',
        text: isBuy ? 'B' : 'S',
      })
    }

    // Exit marker
    const exitTs = String(xf.ts ?? xf.timestamp ?? t.exit_time ?? t.exit_ts ?? '')
    if (exitTs) {
      markers.push({
        time: toEpoch(exitTs) as UTCTimestamp,
        position: isBuy ? 'aboveBar' : 'belowBar',
        color: '#9ca3af',
        shape: isBuy ? 'arrowDown' : 'arrowUp',
        text: 'X',
      })
    }
  }

  // Markers must be sorted by time for lightweight-charts.
  return markers.sort((a, b) => (a.time as number) - (b.time as number))
}

// ── Component ──────────────────────────────────────────────────────────────
export function OHLCChart({ bars, trades, height = 400 }: OHLCChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const { mode, appMode } = useThemeStore()

  const isDark = mode === 'dark' || appMode === 'analyzer'

  useEffect(() => {
    const el = containerRef.current
    if (!el || bars.length === 0) return

    // Tear down any previous chart instance.
    if (chartRef.current) {
      chartRef.current.remove()
      chartRef.current = null
    }

    const textColor = isDark ? '#a6adbb' : '#333333'
    const gridColor = isDark ? 'rgba(166,173,187,0.1)' : 'rgba(0,0,0,0.08)'
    const borderColor = isDark ? 'rgba(166,173,187,0.2)' : 'rgba(0,0,0,0.15)'

    const chart = createChart(el, {
      width: el.clientWidth,
      height,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor,
      },
      grid: {
        vertLines: { color: gridColor },
        horzLines: { color: gridColor },
      },
      rightPriceScale: {
        borderColor,
        scaleMargins: { top: 0.05, bottom: 0.05 },
      },
      timeScale: {
        borderColor,
        timeVisible: true,
        secondsVisible: false,
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: isDark ? 'rgba(166,173,187,0.5)' : 'rgba(0,0,0,0.3)', style: 2 },
        horzLine: { color: isDark ? 'rgba(166,173,187,0.5)' : 'rgba(0,0,0,0.3)', style: 2 },
      },
    })
    chartRef.current = chart

    // Candlestick series
    const series: ISeriesApi<'Candlestick'> = chart.addSeries(CandlestickSeries, {
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderUpColor: '#22c55e',
      borderDownColor: '#ef4444',
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
    })

    const mapped = bars.map((b) => ({
      time: toEpoch(b.time) as UTCTimestamp,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
    }))
    series.setData(mapped)

    // Trade markers via the v5 series-markers plugin.
    if (trades?.length) {
      const markers = buildMarkers(trades)
      if (markers.length > 0) {
        createSeriesMarkers(series, markers)
      }
    }

    chart.timeScale().fitContent()

    // ResizeObserver keeps the chart responsive.
    const ro = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (entry && chartRef.current) {
        chartRef.current.applyOptions({ width: entry.contentRect.width })
      }
    })
    ro.observe(el)

    return () => {
      ro.disconnect()
      chart.remove()
      chartRef.current = null
    }
  }, [bars, trades, height, isDark])

  if (bars.length === 0) return null

  return <div ref={containerRef} style={{ height }} />
}
