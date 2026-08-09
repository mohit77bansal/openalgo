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
import { LayoutGrid, List, Play } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { getStrategyOverview, getBacktestDetail, activateStrategy, deactivateStrategy } from '@/api/backtest'
import type { StrategyOverview } from '@/api/backtest'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { DataTable } from '@/components/ui/data-table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { cn } from '@/lib/utils'
import { useBacktestStore } from '@/stores/backtestStore'
import { showToast } from '@/utils/toast'

const money = (v: number | null | undefined, dp = 0) =>
  v == null ? '-' : `₹${v.toLocaleString('en-IN', { maximumFractionDigits: dp })}`

const pct = (v: number | null | undefined, dp = 1) =>
  v == null ? '-' : `${v.toFixed(dp)}%`

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
  name: 'Strategy', instruments: 'Instruments', net_pnl: 'Net P&L', n_trades: 'Trades',
  sharpe: 'Sharpe', max_drawdown_pct: 'Max DD', xirr_pct: 'XIRR %', return_on_margin_pct: 'RoM %',
  fees_total: 'Fees', capital: 'Capital', source: 'Source', status: 'Status', action: 'Action',
}

export default function Backtest() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const setResult = useBacktestStore((s) => s.setResult)
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

  const active = data?.active ?? []
  const inactive = data?.inactive ?? []
  const all = data?.all ?? []
  const mutLoading = activateMut.isPending || deactivateMut.isPending

  const handleToggle = (s: StrategyOverview) => {
    if (s.is_active) deactivateMut.mutate(s.key)
    else activateMut.mutate(s.key)
  }

  const handleRowClick = async (s: StrategyOverview) => {
    if (!s.run_id) { showToast.info('No backtest results yet — activate this strategy first'); return }
    try {
      const detail = await getBacktestDetail(s.run_id)
      if (detail.equity?.length) {
        setResult(detail, { source: 'db' as const, symbol: detail.symbol, exchange: detail.exchange || 'NFO',
          interval: detail.interval || '15m', start: detail.start || '', end: detail.end || '',
          capital: detail.capital, cost: detail.cost_model })
        navigate('/backtest/results')
      } else {
        showToast.error('Could not load backtest details')
      }
    } catch { showToast.error('Failed to load backtest') }
  }

  const strategyColumns = useMemo<ColumnDef<StrategyOverview, unknown>[]>(() => [
    { accessorKey: 'name', header: 'Strategy', size: 200, cell: ({ row }) => (
      <div><div className="font-medium text-sm">{row.original.name}</div>
        <div className="text-xs text-muted-foreground mt-0.5 line-clamp-1 max-w-[200px]">{row.original.description}</div></div>
    )},
    { id: 'instruments', header: 'Instruments', cell: ({ row }) => row.original.default_instruments.length > 0
      ? <div className="flex flex-wrap gap-1">{row.original.default_instruments.map(i => <Badge key={i} variant="secondary" className="text-[10px] font-normal">{i.split('25')[0]}</Badge>)}</div>
      : <span className="text-xs text-muted-foreground">—</span>, enableSorting: false },
    { accessorKey: 'net_pnl', header: 'Net P&L', cell: ({ getValue }) => {
      const v = getValue<number | null>(); return <span className={cn('text-right tabular-nums text-xs font-medium block', v != null && v >= 0 ? 'text-emerald-500' : v != null ? 'text-rose-500' : '')}>{v != null ? money(v) : '-'}</span>
    }, sortingFn: 'basic' },
    { accessorKey: 'n_trades', header: 'Trades', cell: ({ getValue }) => <span className="text-right tabular-nums text-xs block">{getValue<number | null>() ?? '-'}</span> },
    { accessorKey: 'sharpe', header: 'Sharpe', cell: ({ getValue }) => {
      const v = getValue<number | null>(); return <span className={cn('text-right tabular-nums text-xs font-medium block', v != null && v >= 1.5 ? 'text-emerald-500' : v != null && v < 0 ? 'text-rose-500' : '')}>{v != null ? v.toFixed(2) : '-'}</span>
    }, sortingFn: 'basic' },
    { accessorKey: 'max_drawdown_pct', header: 'Max DD', cell: ({ getValue }) => {
      const v = getValue<number | null>(); return <span className={cn('text-right tabular-nums text-xs block', v != null && v < -10 ? 'text-rose-500' : '')}>{v != null ? pct(v) : '-'}</span>
    }, sortingFn: 'basic' },
    { accessorKey: 'xirr_pct', header: 'XIRR %', cell: ({ getValue }) => {
      const v = getValue<number | null>(); return <span className={cn('text-right tabular-nums text-xs block', v != null && v >= 0 ? 'text-emerald-500' : v != null ? 'text-rose-500' : '')}>{v != null ? pct(v) : '-'}</span>
    }, sortingFn: 'basic' },
    { accessorKey: 'return_on_margin_pct', header: 'RoM %', cell: ({ getValue }) => {
      const v = getValue<number | null>(); return <span className={cn('text-right tabular-nums text-xs block', v != null && v >= 0 ? 'text-emerald-500' : v != null ? 'text-rose-500' : '')}>{v != null ? pct(v) : '-'}</span>
    }, sortingFn: 'basic' },
    { accessorKey: 'fees_total', header: 'Fees', cell: ({ getValue }) => <span className="text-right tabular-nums text-xs block">{getValue<number | null>() != null ? money(getValue<number>()) : '-'}</span> },
    { accessorKey: 'capital', header: 'Capital', cell: ({ getValue }) => <span className="text-right tabular-nums text-xs block">{getValue<number | null>() != null ? money(getValue<number>()) : '-'}</span> },
    { id: 'status', header: 'Status', cell: ({ row }) => row.original.is_active
      ? <Badge className="bg-emerald-500/10 text-emerald-500 border-emerald-500/30 text-[10px]">Active</Badge>
      : <Badge variant="outline" className="text-muted-foreground text-[10px]">Inactive</Badge> },
    { id: 'action', header: '', cell: ({ row }) => (
      <Button variant="outline" size="sm"
        className={cn("h-7 text-xs", row.original.is_active ? "text-rose-500 border-rose-500/30 hover:bg-rose-500/10" : "text-emerald-500 border-emerald-500/30 hover:bg-emerald-500/10")}
        onClick={(e) => { e.stopPropagation(); handleToggle(row.original) }} disabled={mutLoading}>
        {row.original.is_active ? 'Deactivate' : 'Activate'}
      </Button>
    ), enableSorting: false },
  ], [mutLoading])

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
        defaultPinned={['name']}
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
