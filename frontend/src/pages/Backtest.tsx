/**
 * Backtest — configure one run of the event-driven engine and read the result.
 *
 * The result lives in useBacktestStore, set here right before we navigate to
 * the report page, so a long report does not sit permanently below the form.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import {
  type ColumnDef,
  type SortingState,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  useReactTable,
} from '@tanstack/react-table'
import { useCallback, useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { type BacktestSource, runBacktest, getBacktestHistory, getBacktestDetail, getBacktestStrategies, toggleFavorite } from '@/api/backtest'
import type { BacktestRunSummary, StrategyOption } from '@/api/backtest'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useBacktestStore } from '@/stores/backtestStore'
import { showToast } from '@/utils/toast'

const INTERVALS: { value: string; label: string }[] = [
  { value: 'D', label: 'Daily' },
  { value: '5m', label: '5 minute' },
  { value: '15m', label: '15 minute' },
  { value: '1h', label: '1 hour' },
]

const COSTS: { value: string; label: string }[] = [
  { value: 'zerodha', label: 'Zerodha' },
  { value: 'angelone', label: 'AngelOne' },
  { value: 'upstox', label: 'Upstox' },
  { value: 'fyers', label: 'Fyers' },
  { value: 'zero', label: 'Zero (no costs)' },
]

function todayISO(offsetYears = 0): string {
  const d = new Date()
  d.setFullYear(d.getFullYear() - offsetYears)
  return d.toISOString().slice(0, 10)
}

export default function Backtest() {
  const navigate = useNavigate()
  const setResult = useBacktestStore((s) => s.setResult)

  const [source, setSource] = useState<BacktestSource>('db')
  const [symbol, setSymbol] = useState('NIFTY25AUG26FUT')
  const [exchange, setExchange] = useState('NFO')
  const [interval, setInterval] = useState('15m')
  const [start, setStart] = useState(todayISO(1))
  const [end, setEnd] = useState(todayISO(0))
  const [capital, setCapital] = useState(1000000)
  const [cost, setCost] = useState('zerodha')
  const [strategyKey, setStrategyKey] = useState('sma_momentum')

  const { data: strategies = [] } = useQuery({
    queryKey: ['backtest', 'strategies'],
    queryFn: getBacktestStrategies,
    staleTime: 10 * 60_000,
  })

  const selectedStrategy = strategies.find((s: StrategyOption) => s.key === strategyKey)

  const mutation = useMutation({
    mutationFn: runBacktest,
    onSuccess: (res, req) => {
      if (res.status !== 'success') {
        showToast.error(res.message || 'backtest failed')
        return
      }
      setResult(res, req)
      navigate('/backtest/results')
    },
    onError: (err: Error) => {
      const e = err as Error & { response?: { data?: { message?: unknown } } }
      const msg = e.response?.data?.message
      showToast.error(typeof msg === 'string' ? msg : e.message || 'backtest failed')
    },
  })

  const run = () =>
    mutation.mutate({
      source,
      symbol: symbol.trim().toUpperCase(),
      exchange,
      interval,
      start,
      end,
      capital: Number(capital),
      cost,
      strategy: strategyKey,
    })

  return (
    <div className="container mx-auto space-y-4 p-4">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Backtest</h1>
        <p className="text-sm text-muted-foreground">
          Run the strategy over a symbol and read the equity curve, P&amp;L and costs. The demo
          source is synthetic, so it works without a broker login.
        </p>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Backtest Configuration</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
            <div className="space-y-1">
              <Label className="text-xs">Data source</Label>
              <Select value={source} onValueChange={(v) => setSource(v as BacktestSource)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="demo">Synthetic (no data needed)</SelectItem>
                  <SelectItem value="db">AngelOne (live data)</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1">
              <Label className="text-xs">Symbol</Label>
              <Input value={symbol} onChange={(e) => setSymbol(e.target.value)} />
            </div>

            <div className="space-y-1">
              <Label className="text-xs">Exchange</Label>
              <Select value={exchange} onValueChange={setExchange}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="NSE">NSE</SelectItem>
                  <SelectItem value="BSE">BSE</SelectItem>
                  <SelectItem value="NFO">NFO</SelectItem>
                  <SelectItem value="NSE_INDEX">NSE Index</SelectItem>
                  <SelectItem value="MCX">MCX</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1">
              <Label className="text-xs">Interval</Label>
              <Select value={interval} onValueChange={setInterval}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {INTERVALS.map((iv) => (
                    <SelectItem key={iv.value} value={iv.value}>
                      {iv.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1">
              <Label className="text-xs">Start</Label>
              <Input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
            </div>

            <div className="space-y-1">
              <Label className="text-xs">End</Label>
              <Input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
            </div>

            <div className="space-y-1">
              <Label className="text-xs">Capital (₹)</Label>
              <Input
                type="number"
                min={0}
                value={capital}
                onChange={(e) => setCapital(Number(e.target.value))}
              />
            </div>

            <div className="space-y-1">
              <Label className="text-xs">Strategy</Label>
              <Select value={strategyKey} onValueChange={setStrategyKey}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {strategies.map((s: StrategyOption) => (
                    <SelectItem key={s.key} value={s.key}>
                      {s.name}
                    </SelectItem>
                  ))}
                  {strategies.length === 0 && (
                    <SelectItem value="sma_momentum">SMA Momentum</SelectItem>
                  )}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1">
              <Label className="text-xs">Cost model</Label>
              <Select value={cost} onValueChange={setCost}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {COSTS.map((c) => (
                    <SelectItem key={c.value} value={c.value}>
                      {c.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          {selectedStrategy && (
            <p className="text-xs text-muted-foreground border-t pt-3">
              <span className="font-medium text-foreground">{selectedStrategy.name}:</span>{' '}
              {selectedStrategy.description}
            </p>
          )}

          <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-3">
            <p className="text-xs text-muted-foreground">
              {source === 'demo'
                ? 'Demo prices are synthetic and always available.'
                : 'Historify reads locally ingested history, no broker login needed.'}
            </p>
            <Button onClick={run} disabled={mutation.isPending}>
              {mutation.isPending ? 'Running…' : 'Run backtest'}
            </Button>
          </div>
        </CardContent>
      </Card>

      <BacktestHistory />
    </div>
  )
}

/* ---------------------------------------------------------------------------
 * Helpers: formatting + CSV
 * ------------------------------------------------------------------------ */

function formatTime(created_at: string | null | undefined): string {
  if (!created_at) return '-'
  return new Date(created_at + 'Z').toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    year: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: true,
  })
}

function formatSource(source: string): string {
  if (source === 'db') return 'AngelOne'
  if (source === 'demo') return 'Synthetic'
  return source
}

function pnlPct(row: BacktestRunSummary): number | null {
  if (row.capital && row.net_pnl != null) return (row.net_pnl / row.capital) * 100
  return null
}

function pnlColor(value: number | null | undefined): string {
  if (value == null) return ''
  return value >= 0 ? 'text-emerald-500' : 'text-rose-500'
}

function downloadCsv(rows: BacktestRunSummary[]): void {
  const headers = [
    'Time',
    'Strategy',
    'Symbol',
    'Exchange',
    'Start',
    'End',
    'Interval',
    'Source',
    'Capital',
    'Trades',
    'Net P&L',
    'P&L %',
    'XIRR %',
    'RoM %',
    'Fees',
    'Cum. P&L',
    'Status',
  ]

  const escape = (v: string) => (v.includes(',') || v.includes('"') ? `"${v.replace(/"/g, '""')}"` : v)

  let cumPnl = 0
  const csvRows = rows.map((r) => {
    cumPnl += r.net_pnl ?? 0
    const pct = pnlPct(r)
    return [
      formatTime(r.created_at),
      r.strategy,
      r.symbol,
      r.exchange,
      r.start_date ?? '',
      r.end_date ?? '',
      r.interval,
      formatSource(r.source),
      String(r.capital ?? ''),
      String(r.n_trades ?? ''),
      r.net_pnl != null ? String(r.net_pnl) : '',
      pct != null ? pct.toFixed(2) : '',
      r.xirr_pct != null ? r.xirr_pct.toFixed(1) : '',
      r.return_on_margin_pct != null ? r.return_on_margin_pct.toFixed(1) : '',
      r.fees_total != null ? String(r.fees_total) : '',
      String(cumPnl),
      r.status,
    ]
      .map(escape)
      .join(',')
  })

  const csvContent = [headers.join(','), ...csvRows].join('\n')
  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `backtest_history_${new Date().toISOString().slice(0, 10)}.csv`
  link.click()
  URL.revokeObjectURL(url)
}

/* ---------------------------------------------------------------------------
 * Sort indicator
 * ------------------------------------------------------------------------ */

function SortIndicator({ direction }: { direction: false | 'asc' | 'desc' }) {
  if (!direction) return <span className="ml-1 text-muted-foreground/40">&#8597;</span>
  return <span className="ml-1">{direction === 'asc' ? '▲' : '▼'}</span>
}

/* ---------------------------------------------------------------------------
 * Column definitions (stable reference via module scope)
 * ------------------------------------------------------------------------ */

const columns: ColumnDef<BacktestRunSummary, unknown>[] = [
  {
    id: 'favorite',
    header: '',
    cell: ({ row }) => (
      <span className={`cursor-pointer text-sm ${row.original.is_favorite ? 'text-amber-400' : 'text-muted-foreground/30 hover:text-amber-400/60'}`}
            title={row.original.is_favorite ? 'Favorited' : 'Click to favorite'}>
        {row.original.is_favorite ? '★' : '☆'}
      </span>
    ),
    enableSorting: false,
    size: 30,
  },
  {
    accessorKey: 'created_at',
    header: 'Time',
    cell: ({ row }) => (
      <span className="text-xs text-muted-foreground whitespace-nowrap">
        {formatTime(row.original.created_at)}
      </span>
    ),
    sortingFn: 'datetime',
  },
  {
    accessorKey: 'strategy',
    header: 'Strategy',
    cell: ({ row }) => (
      <span
        className="font-medium text-xs max-w-[140px] truncate block"
        title={row.original.strategy_description || row.original.strategy}
      >
        {row.original.strategy}
      </span>
    ),
    enableGlobalFilter: true,
  },
  {
    accessorKey: 'strategy_source',
    header: 'Source',
    cell: ({ getValue }) => {
      const v = getValue<string | null>()
      if (!v) return <span className="text-xs text-muted-foreground">-</span>
      const short = v.split('|')[0].trim()
      return <span className="text-xs text-muted-foreground max-w-[120px] truncate block" title={v}>{short}</span>
    },
    enableSorting: false,
  },
  {
    accessorKey: 'remarks',
    header: 'Remarks',
    cell: ({ getValue }) => {
      const v = getValue<string | null>()
      return <span className="text-xs text-muted-foreground max-w-[100px] truncate block" title={v || ''}>{v || '-'}</span>
    },
    enableSorting: false,
  },
  {
    id: 'instrument',
    accessorFn: (row) => `${row.symbol}/${row.exchange}`,
    header: 'Instrument',
    cell: ({ row }) => (
      <span className="font-medium text-xs whitespace-nowrap">
        {row.original.symbol}
        <span className="text-muted-foreground">/{row.original.exchange}</span>
      </span>
    ),
  },
  {
    id: 'period',
    accessorFn: (row) => row.start_date ?? '',
    header: 'Period',
    cell: ({ row }) => (
      <span className="text-xs text-muted-foreground whitespace-nowrap">
        {row.original.start_date || '—'} &rarr; {row.original.end_date || '—'}
      </span>
    ),
    enableSorting: false,
  },
  {
    accessorKey: 'interval',
    header: 'Freq',
    cell: ({ getValue }) => <span className="text-xs">{getValue<string>()}</span>,
    enableSorting: false,
  },
  {
    accessorKey: 'source',
    header: 'Source',
    cell: ({ getValue }) => <span className="text-xs">{formatSource(getValue<string>())}</span>,
    enableSorting: false,
  },
  {
    accessorKey: 'capital',
    header: 'Capital',
    cell: ({ getValue }) => (
      <span className="text-right tabular-nums text-xs block">
        &#8377;{getValue<number>()?.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
      </span>
    ),
    meta: { align: 'right' },
  },
  {
    accessorKey: 'n_trades',
    header: 'Trades',
    cell: ({ getValue }) => <span className="tabular-nums text-xs">{getValue<number>()}</span>,
  },
  {
    accessorKey: 'net_pnl',
    header: 'Net P&L',
    cell: ({ row }) => {
      const v = row.original.net_pnl
      return (
        <span className={`text-right tabular-nums text-xs font-medium block ${pnlColor(v)}`}>
          {v != null ? `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 0 })}` : '-'}
        </span>
      )
    },
    meta: { align: 'right' },
  },
  {
    id: 'pnl_pct',
    accessorFn: (row) => pnlPct(row),
    header: 'P&L %',
    cell: ({ row }) => {
      const v = pnlPct(row.original)
      return (
        <span className={`text-right tabular-nums text-xs font-medium block ${pnlColor(v)}`}>
          {v != null ? `${v.toFixed(2)}%` : '-'}
        </span>
      )
    },
    meta: { align: 'right' },
  },
  {
    accessorKey: 'xirr_pct',
    header: 'XIRR %',
    cell: ({ getValue }) => {
      const v = getValue<number | null>()
      return (
        <span className={`text-right tabular-nums text-xs block ${pnlColor(v)}`}>
          {v != null ? `${v.toFixed(1)}%` : '-'}
        </span>
      )
    },
    meta: { align: 'right' },
  },
  {
    accessorKey: 'return_on_margin_pct',
    header: 'RoM %',
    cell: ({ getValue }) => {
      const v = getValue<number | null>()
      return (
        <span className={`text-right tabular-nums text-xs font-medium block ${pnlColor(v)}`}>
          {v != null ? `${v.toFixed(1)}%` : '-'}
        </span>
      )
    },
    meta: { align: 'right' },
  },
  {
    accessorKey: 'fees_total',
    header: 'Fees',
    cell: ({ getValue }) => {
      const v = getValue<number | null>()
      return (
        <span className="text-right tabular-nums text-xs block">
          {v != null ? `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 0 })}` : '-'}
        </span>
      )
    },
    meta: { align: 'right' },
  },
  {
    id: 'cumulative_pnl',
    header: 'Cum. P&L',
    cell: ({ row, table }) => {
      const sortedRows = table.getSortedRowModel().rows
      let cumPnl = 0
      for (const r of sortedRows) {
        cumPnl += (r.original as BacktestRunSummary).net_pnl ?? 0
        if (r.id === row.id) break
      }
      return (
        <span className={`text-right tabular-nums text-xs font-medium block ${cumPnl >= 0 ? 'text-emerald-500' : 'text-rose-500'}`}>
          {`₹${cumPnl.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`}
        </span>
      )
    },
    meta: { align: 'right' },
    enableSorting: false,
  },
  {
    accessorKey: 'status',
    header: 'Status',
    cell: ({ getValue }) => {
      const s = getValue<string>()
      return (
        <span
          className={`inline-block rounded-full px-2 py-0.5 text-[10px] font-medium ${
            s === 'success' ? 'bg-emerald-500/10 text-emerald-500' : 'bg-rose-500/10 text-rose-500'
          }`}
        >
          {s}
        </span>
      )
    },
    enableSorting: false,
  },
]

/* ---------------------------------------------------------------------------
 * BacktestHistory component
 * ------------------------------------------------------------------------ */

function BacktestHistory() {
  const navigate = useNavigate()
  const setResult = useBacktestStore((s) => s.setResult)
  const [loadingId, setLoadingId] = useState<number | null>(null)
  const [sorting, setSorting] = useState<SortingState>([{ id: 'created_at', desc: true }])
  const [globalFilter, setGlobalFilter] = useState('')

  const { data: runs = [], isLoading, refetch } = useQuery({
    queryKey: ['backtest', 'history'],
    queryFn: () => getBacktestHistory(200),
    refetchOnWindowFocus: true,
  })

  const handleRowClick = useCallback(
    async (run: BacktestRunSummary) => {
      if (run.status !== 'success') return
      setLoadingId(run.id)
      try {
        const detail = await getBacktestDetail(run.id)
        if (detail.status === 'success' || detail.equity?.length) {
          setResult(detail, {
            source: (run.source as BacktestSource) || 'demo',
            symbol: run.symbol,
            exchange: run.exchange,
            interval: run.interval,
            start: run.start_date || '',
            end: run.end_date || '',
            capital: run.capital,
            cost: run.cost_model,
          })
          navigate('/backtest/results')
        } else {
          showToast.error('Could not load backtest details')
        }
      } catch {
        showToast.error('Failed to load backtest run')
      } finally {
        setLoadingId(null)
      }
    },
    [navigate, setResult],
  )

  const table = useReactTable({
    data: runs,
    columns,
    state: { sorting, globalFilter },
    onSortingChange: setSorting,
    onGlobalFilterChange: setGlobalFilter,
    globalFilterFn: (row, _columnId, filterValue: string) => {
      const strategy = row.original.strategy?.toLowerCase() ?? ''
      return strategy.includes(filterValue.toLowerCase())
    },
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  })

  const visibleRows = useMemo(
    () => table.getRowModel().rows.map((r) => r.original),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [table.getRowModel().rows],
  )

  const handleDownloadCsv = useCallback(() => downloadCsv(visibleRows), [visibleRows])

  if (isLoading) return <p className="text-sm text-muted-foreground">Loading history...</p>
  if (runs.length === 0)
    return (
      <p className="text-sm text-muted-foreground">
        No backtests run yet. Run one above to see it here.
      </p>
    )

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="text-base">Backtest History</CardTitle>
          <div className="flex items-center gap-2">
            <Input
              placeholder="Filter strategy..."
              value={globalFilter}
              onChange={(e) => setGlobalFilter(e.target.value)}
              className="h-8 w-48 text-xs"
            />
            <Button variant="outline" size="sm" className="h-8 text-xs" onClick={handleDownloadCsv}>
              Download CSV
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              {table.getHeaderGroups().map((headerGroup) => (
                <tr key={headerGroup.id} className="border-b text-left text-xs text-muted-foreground">
                  {headerGroup.headers.map((header) => {
                    const canSort = header.column.getCanSort()
                    const align =
                      (header.column.columnDef.meta as { align?: string } | undefined)?.align === 'right'
                        ? 'text-right'
                        : 'text-left'
                    return (
                      <th
                        key={header.id}
                        className={`pb-2 pr-3 ${align} ${canSort ? 'cursor-pointer select-none' : ''}`}
                        onClick={canSort ? header.column.getToggleSortingHandler() : undefined}
                      >
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        {canSort && <SortIndicator direction={header.column.getIsSorted()} />}
                      </th>
                    )
                  })}
                </tr>
              ))}
            </thead>
            <tbody>
              {table.getRowModel().rows.map((row) => {
                const r = row.original
                return (
                  <tr
                    key={row.id}
                    className={`border-b border-border/50 last:border-0 ${
                      r.status === 'success'
                        ? 'cursor-pointer hover:bg-accent/50 transition-colors'
                        : 'opacity-60'
                    }`}
                    onClick={() => handleRowClick(r)}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <td
                        key={cell.id}
                        className="py-2 pr-3"
                        onClick={cell.column.id === 'favorite' ? (e) => {
                          e.stopPropagation()
                          toggleFavorite(r.id).then(() => refetch())
                        } : undefined}
                      >
                        {loadingId === r.id && cell.column.id === 'created_at'
                          ? '...'
                          : flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </td>
                    ))}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  )
}
