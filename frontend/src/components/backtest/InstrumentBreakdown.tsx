import { useMemo } from 'react'
import { type ColumnDef } from '@tanstack/react-table'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { DataTable } from '@/components/ui/data-table'
import { cn } from '@/lib/utils'

const money = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined
    ? '-'
    : `₹${v.toLocaleString('en-IN', { maximumFractionDigits: dp })}`

export type InstrumentRow = {
  symbol: string
  exchange: string
  n_trades: number
  net_pnl: number
  pnl_pct: number
  fees_total: number
  sharpe: number | null
  max_drawdown_pct: number | null
  capital_allocated: number
  total_trading_days?: number
  active_trading_days?: number
  recent_pnl?: number | null
  recent_sharpe?: number | null
}

const R = { align: 'right' as const }

const instrumentColumns: ColumnDef<InstrumentRow, unknown>[] = [
  { accessorKey: 'symbol', header: 'Instrument', cell: ({ getValue }) => {
    const v = String(getValue<string>())
    return <span className={cn('font-medium', v === 'TOTAL' && 'font-bold')}>{v === 'TOTAL' ? 'TOTAL' : v.split('25')[0]}</span>
  }},
  { accessorKey: 'capital_allocated', header: 'Capital', meta: R, cell: ({ getValue }) => <span className="tabular-nums">{money(getValue<number>(), 0)}</span> },
  { accessorKey: 'n_trades', header: 'Trades', meta: R, cell: ({ getValue }) => <span className="tabular-nums">{getValue<number>()}</span> },
  { accessorKey: 'total_trading_days', header: 'Total Days', meta: R, cell: ({ getValue }) => <span className="tabular-nums">{getValue<number>() ?? '-'}</span> },
  { accessorKey: 'active_trading_days', header: 'Active Days', meta: R, cell: ({ row }) => {
    const active = row.original.active_trading_days ?? 0
    const total = row.original.total_trading_days ?? 0
    const pct = total > 0 ? Math.round((active / total) * 100) : 0
    return <span className="tabular-nums">{active > 0 ? `${active} (${pct}%)` : '-'}</span>
  }},
  { accessorKey: 'net_pnl', header: 'Net P&L (after fees)', meta: R, cell: ({ row }) => <span className={cn('tabular-nums font-medium', row.original.net_pnl >= 0 ? 'text-emerald-500' : 'text-rose-500')}>{money(row.original.net_pnl, 0)}</span>, sortingFn: 'basic' },
  { accessorKey: 'pnl_pct', header: 'P&L %', meta: R, cell: ({ row }) => <span className={cn('tabular-nums', row.original.pnl_pct >= 0 ? 'text-emerald-500' : 'text-rose-500')}>{row.original.pnl_pct.toFixed(1)}%</span>, sortingFn: 'basic' },
  { accessorKey: 'fees_total', header: 'Fees', meta: R, cell: ({ getValue }) => <span className="tabular-nums">{money(getValue<number>(), 0)}</span> },
  { accessorKey: 'sharpe', header: 'Sharpe', meta: R, cell: ({ getValue }) => { const v = getValue<number | null>(); return <span className={cn('tabular-nums font-medium', v != null && v >= 1.5 ? 'text-emerald-500' : '')}>{v != null ? v.toFixed(2) : '-'}</span> }, sortingFn: 'basic' },
  { accessorKey: 'max_drawdown_pct', header: 'Max DD %', meta: R, cell: ({ getValue }) => { const v = getValue<number | null>(); return <span className={cn('tabular-nums', v != null && v < -10 ? 'text-rose-500' : '')}>{v != null ? `${v.toFixed(1)}%` : '-'}</span> }, sortingFn: 'basic' },
  { accessorKey: 'recent_sharpe', header: 'Sharpe (30d)', meta: R, cell: ({ getValue }) => { const v = getValue<number | null>(); return <span className={cn('tabular-nums font-medium', v != null && v >= 1.0 ? 'text-emerald-500' : v != null && v < 0 ? 'text-rose-500' : '')}>{v != null ? v.toFixed(2) : '-'}</span> }, sortingFn: 'basic' },
  { accessorKey: 'recent_pnl', header: 'P&L (30d)', meta: R, cell: ({ getValue }) => { const v = getValue<number | null>(); return <span className={cn('tabular-nums font-medium', v != null && v >= 0 ? 'text-emerald-500' : v != null ? 'text-rose-500' : '')}>{v != null ? money(v, 0) : '-'}</span> }, sortingFn: 'basic' },
]

const COLUMN_LABELS: Record<string, string> = {
  symbol: 'Instrument', capital_allocated: 'Capital', n_trades: 'Trades',
  total_trading_days: 'Total Days', active_trading_days: 'Active Days',
  net_pnl: 'Net P&L (after fees)', pnl_pct: 'P&L %', fees_total: 'Fees',
  sharpe: 'Sharpe', max_drawdown_pct: 'Max DD %',
  recent_sharpe: 'Sharpe (30d)', recent_pnl: 'P&L (30d)',
}

export function InstrumentBreakdown({ data }: { data: InstrumentRow[] }) {
  const dataWithTotal = useMemo(() => {
    if (data.length === 0) return data
    const totalCapital = data.reduce((s, r) => s + r.capital_allocated, 0)
    const totalPnl = data.reduce((s, r) => s + r.net_pnl, 0)
    const totalRow: InstrumentRow = {
      symbol: 'TOTAL',
      exchange: '',
      capital_allocated: totalCapital,
      n_trades: data.reduce((s, r) => s + r.n_trades, 0),
      total_trading_days: Math.max(...data.map(r => r.total_trading_days ?? 0), 0),
      active_trading_days: Math.max(...data.map(r => r.active_trading_days ?? 0), 0),
      net_pnl: totalPnl,
      pnl_pct: totalCapital > 0 ? (totalPnl / totalCapital) * 100 : 0,
      fees_total: data.reduce((s, r) => s + r.fees_total, 0),
      sharpe: null,
      max_drawdown_pct: null,
    }
    return [...data, totalRow]
  }, [data])

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Per-Instrument Breakdown</CardTitle>
      </CardHeader>
      <CardContent>
        <DataTable
          columns={instrumentColumns}
          data={dataWithTotal}
          storageKey="backtest-instrument-breakdown"
          defaultPinned={['symbol']}
          columnLabels={COLUMN_LABELS}
          filterPlaceholder="Filter instruments..."
          showDownload
          downloadFilename="instrument-breakdown"
          csvColumns={[
            { key: 'symbol', header: 'Instrument', accessor: (r) => r.symbol },
            { key: 'capital_allocated', header: 'Capital', accessor: (r) => String(r.capital_allocated) },
            { key: 'n_trades', header: 'Trades', accessor: (r) => String(r.n_trades) },
            { key: 'total_trading_days', header: 'Total Trading Days', accessor: (r) => String(r.total_trading_days ?? '') },
            { key: 'active_trading_days', header: 'Active Trading Days', accessor: (r) => String(r.active_trading_days ?? '') },
            { key: 'net_pnl', header: 'Net P&L (after fees)', accessor: (r) => String(r.net_pnl) },
            { key: 'pnl_pct', header: 'P&L %', accessor: (r) => r.pnl_pct.toFixed(1) },
            { key: 'fees_total', header: 'Fees', accessor: (r) => String(r.fees_total) },
            { key: 'sharpe', header: 'Sharpe', accessor: (r) => r.sharpe != null ? r.sharpe.toFixed(2) : '' },
            { key: 'max_drawdown_pct', header: 'Max DD %', accessor: (r) => r.max_drawdown_pct != null ? r.max_drawdown_pct.toFixed(1) : '' },
            { key: 'recent_sharpe', header: 'Sharpe (30d)', accessor: (r) => r.recent_sharpe != null ? r.recent_sharpe.toFixed(2) : '' },
            { key: 'recent_pnl', header: 'P&L (30d)', accessor: (r) => r.recent_pnl != null ? String(r.recent_pnl) : '' },
          ]}
        />
      </CardContent>
    </Card>
  )
}
