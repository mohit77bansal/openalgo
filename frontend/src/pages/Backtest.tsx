/**
 * Backtests — strategy management page.
 *
 * Single page with:
 * 1. Active / Inactive / All tabs to filter strategies
 * 2. Table / Grid toggle to change layout
 * 3. Full performance columns (P&L, Sharpe, XIRR, DD, fees, etc.)
 * 4. Click any row → navigate to results
 * 5. Table uses the shared DataTable component (sort, filter, pin, column config)
 */

import { type ColumnDef } from '@tanstack/react-table'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { LayoutGrid, List, Play, Star } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { getStrategyOverview, activateStrategy, deactivateStrategy, toggleFavorite } from '@/api/backtest'
import type { StrategyOverview } from '@/api/backtest'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { DataTable } from '@/components/ui/data-table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { cn } from '@/lib/utils'
import { showToast } from '@/utils/toast'

const money = (v: number | null | undefined, dp = 0) =>
  v == null ? '-' : `₹${v.toLocaleString('en-IN', { maximumFractionDigits: dp })}`

const pct = (v: number | null | undefined, dp = 1) =>
  v == null ? '-' : `${v.toFixed(dp)}%`

// created_at is a UTC-naive ISO string (backend datetime.utcnow). Append 'Z'
// when it carries no timezone so the browser interprets it as UTC and renders
// in the user's local (IST) time.
const parseCreated = (v: string | null | undefined): number => {
  if (!v) return 0
  const iso = /[Z+]|[+-]\d{2}:\d{2}$/.test(v) ? v : `${v}Z`
  const t = new Date(iso).getTime()
  return Number.isNaN(t) ? 0 : t
}

const formatCreated = (v: string | null | undefined): string => {
  const t = parseCreated(v)
  if (!t) return '-'
  return new Date(t).toLocaleString('en-IN', {
    day: '2-digit',
    month: 'short',
    year: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: true,
  })
}

/* ---------------------------------------------------------------------------
 * Grid card for a strategy
 * ------------------------------------------------------------------------ */

function StrategyCard({ s, onToggle, onClick, loading }: {
  s: StrategyOverview; onToggle: () => void; onClick: () => void; loading: boolean
}) {
  const pnl = s.net_pnl ?? 0
  return (
    <div className={cn('rounded-lg border p-4 cursor-pointer transition-all hover:shadow-md',
      s.is_active ? 'border-emerald-500/20' : 'border-border',
      pnl > 0 ? 'hover:border-emerald-500/40' : pnl < 0 ? 'hover:border-rose-500/40' : ''
    )} onClick={onClick}>
      <div className="flex items-start justify-between mb-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-sm">{s.name}</span>
            {s.is_active && <Badge className="bg-emerald-500/10 text-emerald-500 border-emerald-500/30 text-[10px]">Active</Badge>}
          </div>
          <p className="text-xs text-muted-foreground mt-0.5 line-clamp-1">{s.description}</p>
        </div>
      </div>
      {s.net_pnl != null && (
        <div className={cn('text-xl font-bold tabular-nums mt-2', pnl >= 0 ? 'text-emerald-500' : 'text-rose-500')}>
          {pnl >= 0 ? '+' : ''}{money(pnl)}
        </div>
      )}
      <div className="text-xs text-muted-foreground mt-1">
        {s.n_trades != null ? `${s.n_trades} trades` : ''} {s.sharpe != null ? `· Sharpe ${s.sharpe.toFixed(2)}` : ''}
        {s.xirr_pct != null ? ` · XIRR ${s.xirr_pct.toFixed(0)}%` : ''}
      </div>
      {s.default_instruments.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-2">
          {s.default_instruments.map((inst) => (
            <Badge key={inst} variant="secondary" className="text-[10px] font-normal">{inst.split('25')[0]}</Badge>
          ))}
        </div>
      )}
      <div className="mt-3 pt-2 border-t">
        <Button variant="outline" size="sm"
          className={cn("h-7 text-xs w-full", s.is_active ? "text-rose-500 border-rose-500/30 hover:bg-rose-500/10" : "text-emerald-500 border-emerald-500/30 hover:bg-emerald-500/10")}
          onClick={(e) => { e.stopPropagation(); onToggle() }} disabled={loading}>
          {loading ? 'Running...' : s.is_active ? 'Deactivate' : 'Activate'}
        </Button>
      </div>
    </div>
  )
}

/* ---------------------------------------------------------------------------
 * Main page
 * ------------------------------------------------------------------------ */

const COLUMN_LABELS: Record<string, string> = {
  name: 'Strategy', type: 'Type', instruments: 'Instruments', net_pnl: 'Net P&L', n_trades: 'Trades',
  sharpe: 'Sharpe', recent_sharpe: 'Sharpe (30d)', recent_pnl: 'P&L (30d)',
  max_drawdown_pct: 'Max DD', xirr_pct: 'XIRR %', return_on_margin_pct: 'RoM %',
  fees_total: 'Fees', capital: 'Capital', created_at: 'Created', active: 'Active',
  favorite: 'Favourite',
}

export default function Backtest() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [viewMode, setViewMode] = useState<'table' | 'grid'>('table')

  const { data, isLoading } = useQuery({
    queryKey: ['strategy-overview'],
    queryFn: getStrategyOverview,
    staleTime: 30_000,
  })

  const activateMut = useMutation({
    mutationFn: (key: string) => activateStrategy(key),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['strategy-overview'] }); showToast.success('Strategy activated — backtests running') },
  })

  const deactivateMut = useMutation({
    mutationFn: (key: string) => deactivateStrategy(key),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['strategy-overview'] }); showToast.success('Strategy deactivated') },
  })

  const favoriteMut = useMutation({
    mutationFn: (runId: number) => toggleFavorite(runId),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['strategy-overview'] }) },
    onError: () => showToast.error('Failed to update favourite'),
  })

  const active = data?.active ?? []
  const inactive = data?.inactive ?? []
  const all = data?.all ?? []
  const mutLoading = activateMut.isPending || deactivateMut.isPending

  const handleToggle = (s: StrategyOverview) => {
    if (s.is_active) deactivateMut.mutate(s.key)
    else activateMut.mutate(s.key)
  }

  const handleFavorite = (s: StrategyOverview) => {
    if (!s.run_id) { showToast.info('Backtest the strategy first, then you can favourite it'); return }
    favoriteMut.mutate(s.run_id)
  }

  const handleRowClick = (s: StrategyOverview) => {
    if (!s.run_id) { showToast.info('No backtest results yet — activate this strategy first'); return }
    navigate(`/backtest/results/${s.run_id}`)
  }

  const R = { align: 'right' as const }
  const strategyColumns = useMemo<ColumnDef<StrategyOverview, unknown>[]>(() => [
    { id: 'favorite', header: '', size: 40, enableSorting: true,
      accessorFn: (row) => (row.is_favorite ? 1 : 0),
      cell: ({ row }) => (
        <button
          onClick={(e) => { e.stopPropagation(); handleFavorite(row.original) }}
          disabled={favoriteMut.isPending}
          aria-label={row.original.is_favorite ? 'Unfavourite strategy' : 'Favourite strategy'}
          className="p-1 hover:scale-110 transition-transform"
        >
          <Star className={cn('h-4 w-4', row.original.is_favorite
            ? 'fill-yellow-400 text-yellow-400' : 'text-muted-foreground/40 hover:text-yellow-400')} />
        </button>
      ),
    },
    { accessorKey: 'name', header: 'Strategy', size: 200, cell: ({ row }) => (
      <div><div className="font-medium text-sm">{row.original.name}</div>
        <div className="text-xs text-muted-foreground mt-0.5 line-clamp-1 max-w-[200px]">{row.original.description}</div></div>
    )},
    { id: 'type', header: 'Type', size: 70,
      accessorFn: (row) => {
        const k = row.key
        return (k.includes('option') || k.includes('scalp_') || k.includes('straddle') || k.includes('orb_') || k.includes('iv_crush') || k.includes('expiry_fade')) ? 'Options' : 'Futures'
      },
      cell: ({ getValue }) => {
        const v = getValue<string>()
        const isOpt = v === 'Options'
        return <Badge variant="secondary" className={cn('text-[10px]', isOpt ? 'bg-purple-500/10 text-purple-500' : 'bg-blue-500/10 text-blue-500')}>{v}</Badge>
      },
      filterFn: 'includesString',
    },
    { id: 'instruments', header: 'Instruments', cell: ({ row }) => row.original.default_instruments.length > 0
      ? <div className="flex flex-wrap gap-1">{row.original.default_instruments.map(i => <Badge key={i} variant="secondary" className="text-[10px] font-normal">{i.split('25')[0]}</Badge>)}</div>
      : <span className="text-xs text-muted-foreground">--</span>, enableSorting: false },
    { accessorKey: 'net_pnl', header: 'Net P&L', meta: R, cell: ({ getValue }) => {
      const v = getValue<number | null>(); return <span className={cn('tabular-nums text-xs font-medium', v != null && v >= 0 ? 'text-emerald-500' : v != null ? 'text-rose-500' : '')}>{v != null ? money(v) : '-'}</span>
    }, sortingFn: 'basic' },
    { accessorKey: 'n_trades', header: 'Trades', meta: R, cell: ({ getValue }) => <span className="tabular-nums text-xs">{getValue<number | null>() ?? '-'}</span> },
    { accessorKey: 'sharpe', header: 'Sharpe', meta: R, cell: ({ getValue }) => {
      const v = getValue<number | null>(); return <span className={cn('tabular-nums text-xs font-medium', v != null && v >= 1.5 ? 'text-emerald-500' : v != null && v < 0 ? 'text-rose-500' : '')}>{v != null ? v.toFixed(2) : '-'}</span>
    }, sortingFn: 'basic' },
    { accessorKey: 'recent_sharpe', header: 'Sharpe (30d)', meta: R, cell: ({ getValue }) => {
      const v = getValue<number | null>(); return <span className={cn('tabular-nums text-xs font-medium', v != null && v >= 1.0 ? 'text-emerald-500' : v != null && v < 0 ? 'text-rose-500' : '')}>{v != null ? v.toFixed(2) : '-'}</span>
    }, sortingFn: 'basic' },
    { accessorKey: 'recent_pnl', header: 'P&L (30d)', meta: R, cell: ({ getValue }) => {
      const v = getValue<number | null>(); return <span className={cn('tabular-nums text-xs font-medium', v != null && v >= 0 ? 'text-emerald-500' : v != null ? 'text-rose-500' : '')}>{v != null ? money(v) : '-'}</span>
    }, sortingFn: 'basic' },
    { accessorKey: 'max_drawdown_pct', header: 'Max DD', meta: R, cell: ({ getValue }) => {
      const v = getValue<number | null>(); return <span className={cn('tabular-nums text-xs', v != null && v < -10 ? 'text-rose-500' : '')}>{v != null ? pct(v) : '-'}</span>
    }, sortingFn: 'basic' },
    { accessorKey: 'xirr_pct', header: 'XIRR %', meta: R, cell: ({ getValue }) => {
      const v = getValue<number | null>(); return <span className={cn('tabular-nums text-xs', v != null && v >= 0 ? 'text-emerald-500' : v != null ? 'text-rose-500' : '')}>{v != null ? pct(v) : '-'}</span>
    }, sortingFn: 'basic' },
    { accessorKey: 'return_on_margin_pct', header: 'RoM %', meta: R, cell: ({ getValue }) => {
      const v = getValue<number | null>(); return <span className={cn('tabular-nums text-xs', v != null && v >= 0 ? 'text-emerald-500' : v != null ? 'text-rose-500' : '')}>{v != null ? pct(v) : '-'}</span>
    }, sortingFn: 'basic' },
    { accessorKey: 'fees_total', header: 'Fees', meta: R, cell: ({ getValue }) => <span className="tabular-nums text-xs">{getValue<number | null>() != null ? money(getValue<number>()) : '-'}</span> },
    { accessorKey: 'capital', header: 'Capital', meta: R, cell: ({ getValue }) => <span className="tabular-nums text-xs">{getValue<number | null>() != null ? money(getValue<number>()) : '-'}</span> },
    { accessorKey: 'created_at', header: 'Created', meta: R,
      // created_at is UTC-naive from the backend (datetime.utcnow); mark it UTC
      // so the browser renders it in the user's local (IST) time.
      sortingFn: (a, b) => (parseCreated(a.original.created_at) - parseCreated(b.original.created_at)),
      cell: ({ getValue }) => <span className="tabular-nums text-xs text-muted-foreground whitespace-nowrap">{formatCreated(getValue<string | null>())}</span> },
    { id: 'active', header: 'Active', cell: ({ row }) => (
      <button
        onClick={(e) => { e.stopPropagation(); handleToggle(row.original) }}
        disabled={mutLoading}
        className={cn(
          'relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors',
          row.original.is_active ? 'bg-emerald-500' : 'bg-muted-foreground/30',
          mutLoading && 'opacity-50 cursor-not-allowed',
        )}
      >
        <span className={cn(
          'pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow-sm transition-transform',
          row.original.is_active ? 'translate-x-4' : 'translate-x-0',
        )} />
      </button>
    ), enableSorting: false },
  ], [mutLoading, favoriteMut.isPending])

  const renderStrategies = (strategies: StrategyOverview[]) => {
    if (strategies.length === 0) return <p className="text-sm text-muted-foreground py-8 text-center">No strategies in this tab.</p>

    if (viewMode === 'grid') {
      return (
        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
          {strategies.map((s) => (
            <StrategyCard key={s.key} s={s} onToggle={() => handleToggle(s)} onClick={() => handleRowClick(s)} loading={mutLoading} />
          ))}
        </div>
      )
    }

    return (
      <DataTable
        columns={strategyColumns}
        data={strategies}
        storageKey="backtest-strategies-table"
        defaultPinned={['favorite', 'name']}
        defaultSorting={[{ id: 'favorite', desc: true }, { id: 'created_at', desc: true }]}
        columnLabels={COLUMN_LABELS}
        filterPlaceholder="Filter strategies..."
        onRowClick={(s) => handleRowClick(s)}
        showDownload
        downloadFilename="strategies"
        csvColumns={[
          { key: 'name', header: 'Strategy', accessor: (r) => r.name },
          { key: 'instruments', header: 'Instruments', accessor: (r) => r.default_instruments.join(', ') },
          { key: 'net_pnl', header: 'Net P&L', accessor: (r) => String(r.net_pnl ?? '') },
          { key: 'n_trades', header: 'Trades', accessor: (r) => String(r.n_trades ?? '') },
          { key: 'sharpe', header: 'Sharpe', accessor: (r) => r.sharpe != null ? r.sharpe.toFixed(2) : '' },
          { key: 'max_drawdown_pct', header: 'Max DD %', accessor: (r) => r.max_drawdown_pct != null ? r.max_drawdown_pct.toFixed(1) : '' },
          { key: 'xirr_pct', header: 'XIRR %', accessor: (r) => r.xirr_pct != null ? r.xirr_pct.toFixed(1) : '' },
          { key: 'return_on_margin_pct', header: 'RoM %', accessor: (r) => r.return_on_margin_pct != null ? r.return_on_margin_pct.toFixed(1) : '' },
          { key: 'fees_total', header: 'Fees', accessor: (r) => String(r.fees_total ?? '') },
          { key: 'capital', header: 'Capital', accessor: (r) => String(r.capital ?? '') },
          { key: 'created_at', header: 'Created', accessor: (r) => formatCreated(r.created_at) },
          { key: 'status', header: 'Status', accessor: (r) => r.is_active ? 'Active' : 'Inactive' },
        ]}
      />
    )
  }

  return (
    <div className="container mx-auto space-y-4 p-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">Backtests</h1>
        <Button onClick={() => navigate('/backtest/new')}>
          <Play className="h-3.5 w-3.5 mr-1.5" /> Run New Backtest
        </Button>
      </div>

      <Card>
        <CardContent className="p-4">
          <Tabs defaultValue="active">
            <div className="flex items-center justify-between mb-4">
              <TabsList className="h-9">
                <TabsTrigger value="active" className="text-xs px-4">Active <Badge variant="secondary" className="ml-1.5 text-[10px]">{active.length}</Badge></TabsTrigger>
                <TabsTrigger value="inactive" className="text-xs px-4">Inactive <Badge variant="secondary" className="ml-1.5 text-[10px]">{inactive.length}</Badge></TabsTrigger>
                <TabsTrigger value="all" className="text-xs px-4">All <Badge variant="secondary" className="ml-1.5 text-[10px]">{all.length}</Badge></TabsTrigger>
              </TabsList>
              <div className="flex items-center border rounded-md overflow-hidden">
                <button onClick={() => setViewMode('table')} className={`p-1.5 ${viewMode === 'table' ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:bg-muted/50'}`} title="Table view">
                  <List className="h-3.5 w-3.5" />
                </button>
                <button onClick={() => setViewMode('grid')} className={`p-1.5 ${viewMode === 'grid' ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:bg-muted/50'}`} title="Card view">
                  <LayoutGrid className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
            {isLoading ? <p className="text-sm text-muted-foreground py-8 text-center">Loading strategies...</p> : (
              <>
                <TabsContent value="active" className="mt-0">{renderStrategies(active)}</TabsContent>
                <TabsContent value="inactive" className="mt-0">{renderStrategies(inactive)}</TabsContent>
                <TabsContent value="all" className="mt-0">{renderStrategies(all)}</TabsContent>
              </>
            )}
          </Tabs>
        </CardContent>
      </Card>
    </div>
  )
}
