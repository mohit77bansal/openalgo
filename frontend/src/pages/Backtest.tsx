/**
 * Backtest — history list page.
 *
 * Shows all past backtest runs in a sortable, filterable table.
 * "Run New Backtest" navigates to /backtest/new; clicking a row
 * navigates to /backtest/results.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  type Column,
  type ColumnDef,
  type ColumnOrderState,
  type ColumnPinningState,
  type SortingState,
  type VisibilityState,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  useReactTable,
} from '@tanstack/react-table'
import { ChevronDown, ChevronUp, LayoutGrid, List, RotateCcw, Settings2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { type BacktestSource, getBacktestHistory, getBacktestDetail, toggleFavorite, getStrategyConfigs, activateStrategy, deactivateStrategy } from '@/api/backtest'
import type { BacktestRunSummary, StrategyConfig } from '@/api/backtest'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { useBacktestStore } from '@/stores/backtestStore'
import { showToast } from '@/utils/toast'

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
    'Sharpe',
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
      r.sharpe != null ? r.sharpe.toFixed(2) : '',
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
 * Column configuration: ordering, visibility, pinning, persistence
 * ------------------------------------------------------------------------ */

const STORAGE_KEY = 'backtest-table-columns'

const DEFAULT_COLUMN_ORDER: string[] = [
  'favorite', 'created_at', 'strategy', 'strategy_source', 'remarks',
  'instrument', 'period', 'interval', 'source', 'capital', 'n_trades',
  'net_pnl', 'pnl_pct', 'sharpe', 'xirr_pct', 'return_on_margin_pct', 'fees_total',
  'cumulative_pnl', 'status',
]

const COLUMN_LABELS: Record<string, string> = {
  favorite: 'Favorite',
  created_at: 'Time',
  strategy: 'Strategy',
  strategy_source: 'Str. Source',
  remarks: 'Remarks',
  instrument: 'Instrument',
  period: 'Period',
  interval: 'Freq',
  source: 'Data Source',
  capital: 'Capital',
  n_trades: 'Trades',
  net_pnl: 'Net P&L',
  pnl_pct: 'P&L %',
  sharpe: 'Sharpe',
  xirr_pct: 'XIRR %',
  return_on_margin_pct: 'RoM %',
  fees_total: 'Fees',
  cumulative_pnl: 'Cum. P&L',
  status: 'Status',
}

/** Columns pinned to the left edge during horizontal scroll. */
const PINNED_LEFT: ColumnPinningState = {
  left: ['favorite', 'created_at', 'strategy'],
}

interface PersistedColumnSettings {
  order: string[]
  visibility: VisibilityState
}

function loadColumnSettings(): PersistedColumnSettings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return { order: DEFAULT_COLUMN_ORDER, visibility: {} }
    const parsed = JSON.parse(raw) as Partial<PersistedColumnSettings>
    return {
      order: Array.isArray(parsed.order) ? parsed.order : DEFAULT_COLUMN_ORDER,
      visibility:
        parsed.visibility && typeof parsed.visibility === 'object'
          ? parsed.visibility
          : {},
    }
  } catch {
    return { order: DEFAULT_COLUMN_ORDER, visibility: {} }
  }
}

function saveColumnSettings(order: string[], visibility: VisibilityState): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ order, visibility }))
  } catch {
    // localStorage full or unavailable — silently skip
  }
}

/* ---------------------------------------------------------------------------
 * Pinning: sticky style helper
 * ------------------------------------------------------------------------ */

function getPinningStyle(
  column: Column<BacktestRunSummary, unknown>,
): React.CSSProperties {
  const isPinned = column.getIsPinned()
  if (!isPinned) return {}
  const isLast = isPinned === 'left' && column.getIsLastColumn('left')
  return {
    left: `${column.getStart('left')}px`,
    position: 'sticky',
    zIndex: 2,
    width: column.getSize(),
    boxShadow: isLast ? '4px 0 8px -4px rgba(0,0,0,0.08)' : undefined,
  }
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
    size: 36,
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
    size: 145,
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
    size: 145,
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
    accessorKey: 'sharpe',
    header: 'Sharpe',
    cell: ({ getValue }) => {
      const v = getValue<number | null>()
      return (
        <span className={`text-right tabular-nums text-xs font-medium block ${v != null && v >= 1.5 ? 'text-emerald-500' : v != null && v < 0 ? 'text-rose-500' : ''}`}>
          {v != null ? v.toFixed(2) : '-'}
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
 * Column config dropdown: visibility toggles + reorder arrows
 * ------------------------------------------------------------------------ */

function BacktestColumnConfig({
  columnOrder,
  columnVisibility,
  onOrderChange,
  onVisibilityChange,
}: {
  columnOrder: ColumnOrderState
  columnVisibility: VisibilityState
  onOrderChange: (order: ColumnOrderState) => void
  onVisibilityChange: (vis: VisibilityState) => void
}) {
  const pinnedIds = PINNED_LEFT.left ?? []

  const moveColumn = (colId: string, direction: -1 | 1) => {
    const idx = columnOrder.indexOf(colId)
    if (idx < 0) return
    const targetIdx = idx + direction
    if (targetIdx < 0 || targetIdx >= columnOrder.length) return
    // Prevent moving into or out of the pinned zone
    if (pinnedIds.includes(colId) || pinnedIds.includes(columnOrder[targetIdx])) return
    const next = [...columnOrder]
    const [removed] = next.splice(idx, 1)
    next.splice(targetIdx, 0, removed)
    onOrderChange(next)
  }

  const toggleVisibility = (colId: string) => {
    const current = columnVisibility[colId] ?? true
    onVisibilityChange({ ...columnVisibility, [colId]: !current })
  }

  const resetAll = () => {
    onOrderChange(DEFAULT_COLUMN_ORDER)
    onVisibilityChange({})
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="h-8 text-xs gap-1.5">
          <Settings2 className="h-3.5 w-3.5" />
          Columns
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56 max-h-80 overflow-y-auto">
        <DropdownMenuLabel className="text-[10px] uppercase tracking-wider text-muted-foreground">
          Pinned
        </DropdownMenuLabel>
        {pinnedIds.map((id) => (
          <DropdownMenuCheckboxItem key={id} checked disabled className="opacity-70 text-xs">
            {COLUMN_LABELS[id] ?? id}
          </DropdownMenuCheckboxItem>
        ))}

        <DropdownMenuSeparator />

        <DropdownMenuLabel className="text-[10px] uppercase tracking-wider text-muted-foreground">
          Columns
        </DropdownMenuLabel>
        {columnOrder
          .filter((id) => !pinnedIds.includes(id))
          .map((id, idx, arr) => {
            const visible = columnVisibility[id] ?? true
            return (
              <div key={id} className="flex items-center">
                <DropdownMenuCheckboxItem
                  checked={visible}
                  onCheckedChange={() => toggleVisibility(id)}
                  onSelect={(e) => e.preventDefault()}
                  className="flex-1 text-xs"
                >
                  {COLUMN_LABELS[id] ?? id}
                </DropdownMenuCheckboxItem>
                <div className="flex flex-col mr-2">
                  <button
                    type="button"
                    className="p-0.5 text-muted-foreground hover:text-foreground disabled:opacity-25"
                    disabled={idx === 0}
                    onClick={(e) => { e.stopPropagation(); moveColumn(id, -1) }}
                    aria-label={`Move ${COLUMN_LABELS[id] ?? id} up`}
                  >
                    <ChevronUp className="h-3 w-3" />
                  </button>
                  <button
                    type="button"
                    className="p-0.5 text-muted-foreground hover:text-foreground disabled:opacity-25"
                    disabled={idx === arr.length - 1}
                    onClick={(e) => { e.stopPropagation(); moveColumn(id, 1) }}
                    aria-label={`Move ${COLUMN_LABELS[id] ?? id} down`}
                  >
                    <ChevronDown className="h-3 w-3" />
                  </button>
                </div>
              </div>
            )
          })}

        <DropdownMenuSeparator />

        <div className="px-2 py-1">
          <Button
            variant="ghost"
            size="sm"
            className="w-full justify-start text-xs h-7"
            onClick={resetAll}
          >
            <RotateCcw className="mr-2 h-3.5 w-3.5" />
            Reset to Defaults
          </Button>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/* ---------------------------------------------------------------------------
 * Strategy Config Panel — Active / Inactive tabs
 * ------------------------------------------------------------------------ */

function StrategyCard({ s, onActivate, onDeactivate, isLoading }: {
  s: StrategyConfig; onActivate: (key: string) => void; onDeactivate: (key: string) => void; isLoading: boolean
}) {
  return (
    <div className={`flex items-start justify-between gap-3 rounded-lg border p-3 transition-colors ${s.is_active ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-border hover:bg-muted/30'}`}>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-medium text-sm">{s.name}</span>
          {s.is_active && <Badge variant="outline" className="text-[10px] border-emerald-500/50 text-emerald-500">ACTIVE</Badge>}
        </div>
        <p className="text-xs text-muted-foreground mt-0.5 line-clamp-2">{s.description}</p>
        {s.is_active && s.default_instruments.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-1.5">
            {s.default_instruments.map((inst: string) => (
              <Badge key={inst} variant="secondary" className="text-[10px] font-normal">{inst.split('25')[0]}</Badge>
            ))}
          </div>
        )}
        {s.source && <p className="text-[10px] text-muted-foreground/60 mt-1">{s.source.split('|')[0].trim()}</p>}
      </div>
      <div className="shrink-0">
        {s.is_active ? (
          <Button variant="outline" size="sm" className="h-7 text-xs text-rose-500 border-rose-500/30 hover:bg-rose-500/10" onClick={() => onDeactivate(s.key)} disabled={isLoading}>
            Deactivate
          </Button>
        ) : (
          <Button variant="outline" size="sm" className="h-7 text-xs text-emerald-500 border-emerald-500/30 hover:bg-emerald-500/10" onClick={() => onActivate(s.key)} disabled={isLoading}>
            Activate
          </Button>
        )}
      </div>
    </div>
  )
}

function StrategyPanel() {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ['strategy-configs'],
    queryFn: getStrategyConfigs,
    staleTime: 30_000,
  })

  const activateMut = useMutation({
    mutationFn: (key: string) => activateStrategy(key),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['strategy-configs'] }); queryClient.invalidateQueries({ queryKey: ['backtest', 'history'] }) },
  })

  const deactivateMut = useMutation({
    mutationFn: (key: string) => deactivateStrategy(key),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['strategy-configs'] }),
  })

  const active = data?.active ?? []
  const inactive = data?.inactive ?? []
  const mutLoading = activateMut.isPending || deactivateMut.isPending

  return (
    <Card>
      <CardContent className="p-4">
        <Tabs defaultValue="active">
          <div className="flex items-center justify-between mb-3">
            <TabsList className="h-8">
              <TabsTrigger value="active" className="text-xs px-3">Active <Badge variant="secondary" className="ml-1.5 text-[10px]">{active.length}</Badge></TabsTrigger>
              <TabsTrigger value="inactive" className="text-xs px-3">Inactive <Badge variant="secondary" className="ml-1.5 text-[10px]">{inactive.length}</Badge></TabsTrigger>
            </TabsList>
            {activateMut.isPending && <span className="text-xs text-muted-foreground animate-pulse">Running backtests...</span>}
          </div>
          <TabsContent value="active" className="mt-0">
            {isLoading ? <p className="text-xs text-muted-foreground">Loading...</p> :
              active.length === 0 ? <p className="text-xs text-muted-foreground">No active strategies. Activate one from the Inactive tab.</p> :
              <div className="space-y-2">{active.map((s: StrategyConfig) => <StrategyCard key={s.key} s={s} onActivate={(k) => activateMut.mutate(k)} onDeactivate={(k) => deactivateMut.mutate(k)} isLoading={mutLoading} />)}</div>
            }
          </TabsContent>
          <TabsContent value="inactive" className="mt-0">
            {isLoading ? <p className="text-xs text-muted-foreground">Loading...</p> :
              <div className="space-y-2">{inactive.map((s: StrategyConfig) => <StrategyCard key={s.key} s={s} onActivate={(k) => activateMut.mutate(k)} onDeactivate={(k) => deactivateMut.mutate(k)} isLoading={mutLoading} />)}</div>
            }
          </TabsContent>
        </Tabs>
      </CardContent>
    </Card>
  )
}

/* ---------------------------------------------------------------------------
 * Backtest (list page)
 * ------------------------------------------------------------------------ */

export default function Backtest() {
  const navigate = useNavigate()
  const setResult = useBacktestStore((s) => s.setResult)
  const [loadingId, setLoadingId] = useState<number | null>(null)
  const [sorting, setSorting] = useState<SortingState>([{ id: 'created_at', desc: true }])
  const [globalFilter, setGlobalFilter] = useState('')
  const [viewMode, setViewMode] = useState<'table' | 'grid'>('table')

  // Column ordering + visibility — persisted to localStorage
  const [persisted] = useState(loadColumnSettings)
  const [columnOrder, setColumnOrder] = useState<ColumnOrderState>(persisted.order)
  const [columnVisibility, setColumnVisibility] = useState<VisibilityState>(persisted.visibility)

  useEffect(() => {
    saveColumnSettings(columnOrder, columnVisibility)
  }, [columnOrder, columnVisibility])

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
    state: {
      sorting,
      globalFilter,
      columnOrder,
      columnVisibility,
      columnPinning: PINNED_LEFT,
    },
    onSortingChange: setSorting,
    onGlobalFilterChange: setGlobalFilter,
    onColumnOrderChange: setColumnOrder,
    onColumnVisibilityChange: setColumnVisibility,
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

  return (
    <div className="container mx-auto space-y-4 p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-bold tracking-tight">Backtests</h1>
          <div className="flex items-center border rounded-md overflow-hidden ml-3">
            <button onClick={() => setViewMode('grid')} className={`px-3 py-1.5 text-xs font-medium transition-colors ${viewMode === 'grid' ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:bg-muted/50'}`}>
              <LayoutGrid className="h-3.5 w-3.5 inline mr-1.5" />Strategies
            </button>
            <button onClick={() => setViewMode('table')} className={`px-3 py-1.5 text-xs font-medium transition-colors ${viewMode === 'table' ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:bg-muted/50'}`}>
              <List className="h-3.5 w-3.5 inline mr-1.5" />History
            </button>
          </div>
        </div>
        <Button onClick={() => navigate('/backtest/new')}>Run New Backtest</Button>
      </div>

      {viewMode === 'grid' ? (
        <StrategyPanel />
      ) : isLoading ? (
        <p className="text-sm text-muted-foreground">Loading history...</p>
      ) : runs.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No backtests run yet. Click &ldquo;Run New Backtest&rdquo; to get started.
        </p>
      ) : (
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
                <BacktestColumnConfig
                  columnOrder={columnOrder}
                  columnVisibility={columnVisibility}
                  onOrderChange={setColumnOrder}
                  onVisibilityChange={setColumnVisibility}
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
                            className={`pb-2 pr-3 ${align} ${canSort ? 'cursor-pointer select-none' : ''} ${header.column.getIsPinned() ? 'bg-card' : ''}`}
                            style={getPinningStyle(header.column)}
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
                        className={`group/row border-b border-border/50 last:border-0 ${
                          r.status === 'success'
                            ? 'cursor-pointer hover:bg-accent/50 transition-colors'
                            : 'opacity-60'
                        }`}
                        onClick={() => handleRowClick(r)}
                      >
                        {row.getVisibleCells().map((cell) => {
                          const pinned = cell.column.getIsPinned()
                          return (
                            <td
                              key={cell.id}
                              className={`py-2 pr-3 ${pinned ? 'bg-card group-hover/row:bg-accent/50 transition-colors' : ''}`}
                              style={getPinningStyle(cell.column)}
                              onClick={cell.column.id === 'favorite' ? (e) => {
                                e.stopPropagation()
                                toggleFavorite(r.id).then(() => refetch())
                              } : undefined}
                            >
                              {loadingId === r.id && cell.column.id === 'created_at'
                                ? '...'
                                : flexRender(cell.column.columnDef.cell, cell.getContext())}
                            </td>
                          )
                        })}
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
