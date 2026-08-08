import {
  AreaSeries,
  ColorType,
  CrosshairMode,
  LineStyle,
  createChart,
  type IChartApi,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useEffect, useRef } from 'react'

import { useThemeStore } from '@/stores/themeStore'

interface EquityCurveDataPoint {
  readonly date: string
  readonly value: number
}

interface EquityCurveChartProps {
  readonly data: EquityCurveDataPoint[]
  readonly height?: number
  readonly capital?: number
}

function toEpoch(iso: string): UTCTimestamp {
  return (new Date(iso).getTime() / 1000) as UTCTimestamp
}

function formatLakhsPrice(value: number): string {
  const abs = Math.abs(value)
  const sign = value < 0 ? '-' : ''

  if (abs >= 100_000) {
    const lakhs = abs / 100_000
    return `${sign}₹${lakhs.toFixed(1)}L`
  }
  if (abs >= 1_000) {
    const thousands = abs / 1_000
    return `${sign}₹${thousands.toFixed(0)}K`
  }
  return `${sign}₹${abs.toFixed(0)}`
}

export function EquityCurveChart({
  data,
  height = 380,
}: EquityCurveChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const { mode, appMode } = useThemeStore()

  const isDark = mode === 'dark' || appMode === 'analyzer'

  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const bgColor = isDark ? '#0b1117' : 'transparent'
    const textColor = isDark ? '#a6adbb' : '#333333'
    const gridColor = isDark ? 'rgba(166,173,187,0.07)' : 'rgba(0,0,0,0.06)'
    const borderColor = isDark ? 'rgba(166,173,187,0.12)' : 'rgba(0,0,0,0.1)'

    const chart = createChart(container, {
      width: container.clientWidth,
      height,
      layout: {
        background: { type: ColorType.Solid, color: bgColor },
        textColor,
      },
      grid: {
        vertLines: { color: gridColor },
        horzLines: { color: gridColor },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { style: LineStyle.Dashed },
        horzLine: { style: LineStyle.Dashed },
      },
      rightPriceScale: {
        borderColor,
      },
      timeScale: {
        borderColor,
      },
    })

    chartRef.current = chart

    const areaSeries = chart.addSeries(AreaSeries, {
      topColor: '#3b82f633',
      bottomColor: '#3b82f600',
      lineColor: '#3b82f6',
      lineWidth: 2,
      priceFormat: {
        type: 'custom',
        formatter: formatLakhsPrice,
      },
    })

    const seriesData = data.map((point) => ({
      time: toEpoch(point.date),
      value: point.value,
    }))

    areaSeries.setData(seriesData)
    chart.timeScale().fitContent()

    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width } = entry.contentRect
        chart.applyOptions({ width })
      }
    })
    resizeObserver.observe(container)

    return () => {
      resizeObserver.disconnect()
      chart.remove()
      chartRef.current = null
    }
  }, [data, height, isDark])

  return <div ref={containerRef} style={{ height }} />
}
