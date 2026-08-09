/**
 * Backtests — strategy management page.
 *
 * Single page with:
 * 1. Active / Inactive tabs to filter strategies
 * 2. Table / Grid toggle to change layout
 * 3. Activate / Deactivate toggle per strategy
 */

import { type ColumnDef } from '@tanstack/react-table'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { LayoutGrid, List, Play } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { getStrategyConfigs, activateStrategy, deactivateStrategy } from '@/api/backtest'
import type { StrategyConfig } from '@/api/backtest'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { DataTable } from '@/components/ui/data-table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { cn } from '@/lib/utils'
import { showToast } from '@/utils/toast'

/* ---------------------------------------------------------------------------
 * Grid card for a strategy
 * ------------------------------------------------------------------------ */

function StrategyCard({ s, onToggle, loading }: { s: StrategyConfig; onToggle: () => void; loading: boolean }) {
  return (
    <div className={cn('rounded-lg border p-4 transition-colors', s.is_active ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-border')}>
      <div className="flex items-start justify-between mb-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-sm">{s.name}</span>
            {s.is_active && <Badge className="bg-emerald-500/10 text-emerald-500 border-emerald-500/30 text-[10px]">Active</Badge>}
          </div>
          <p className="text-xs text-muted-foreground mt-1 line-clamp-2">{s.description}</p>
        </div>
      </div>
      {s.default_instruments.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-2">
          {s.default_instruments.map((inst) => (
            <Badge key={inst} variant="secondary" className="text-[10px] font-normal">{inst.split('25')[0]}</Badge>
          ))}
        </div>
      )}
      {s.source && <p className="text-[10px] text-muted-foreground/60 mt-2">{s.source.split('|')[0].trim()}</p>}
      <div className="mt-3 pt-2 border-t">
        <Button
          variant="outline" size="sm" className={cn("h-7 text-xs w-full", s.is_active ? "text-rose-500 border-rose-500/30 hover:bg-rose-500/10" : "text-emerald-500 border-emerald-500/30 hover:bg-emerald-500/10")}
          onClick={onToggle} disabled={loading}
        >
          {loading ? 'Running...' : s.is_active ? 'Deactivate' : 'Activate'}
        </Button>
      </div>
    </div>
  )
}

/* ---------------------------------------------------------------------------
 * Main page
 * ------------------------------------------------------------------------ */

export default function Backtest() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [viewMode, setViewMode] = useState<'table' | 'grid'>('table')

  const { data, isLoading } = useQuery({
    queryKey: ['strategy-configs'],
    queryFn: getStrategyConfigs,
    staleTime: 30_000,
  })

  const activateMut = useMutation({
    mutationFn: (key: string) => activateStrategy(key),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['strategy-configs'] })
      showToast.success('Strategy activated — backtests running')
    },
  })

  const deactivateMut = useMutation({
    mutationFn: (key: string) => deactivateStrategy(key),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['strategy-configs'] })
      showToast.success('Strategy deactivated')
    },
  })

  const active = data?.active ?? []
  const inactive = data?.inactive ?? []
  const mutLoading = activateMut.isPending || deactivateMut.isPending

  const handleToggle = (s: StrategyConfig) => {
    if (s.is_active) deactivateMut.mutate(s.key)
    else activateMut.mutate(s.key)
  }

  const strategyColumns = useMemo<ColumnDef<StrategyConfig, unknown>[]>(() => [
    { accessorKey: 'name', header: 'Strategy', cell: ({ row }) => (
      <div>
        <div className="font-medium text-sm">{row.original.name}</div>
        <div className="text-xs text-muted-foreground mt-0.5 line-clamp-1">{row.original.description}</div>
      </div>
    )},
    { id: 'instruments', header: 'Instruments', cell: ({ row }) => row.original.default_instruments.length > 0 ? (
      <div className="flex flex-wrap gap-1">{row.original.default_instruments.map(i => <Badge key={i} variant="secondary" className="text-[10px] font-normal">{i.split('25')[0]}</Badge>)}</div>
    ) : <span className="text-xs text-muted-foreground">—</span>, enableSorting: false },
    { accessorKey: 'source', header: 'Source', cell: ({ getValue }) => <span className="text-xs text-muted-foreground truncate block max-w-[150px]" title={getValue<string>() || ''}>{(getValue<string>() || '—').split('|')[0].trim()}</span> },
    { id: 'status', header: 'Status', cell: ({ row }) => row.original.is_active
      ? <Badge className="bg-emerald-500/10 text-emerald-500 border-emerald-500/30 text-[10px]">Active</Badge>
      : <Badge variant="outline" className="text-muted-foreground text-[10px]">Inactive</Badge>,
      sortingFn: (a, b) => (a.original.is_active ? 1 : 0) - (b.original.is_active ? 1 : 0),
    },
    { id: 'action', header: 'Action', cell: ({ row }) => (
      <Button variant="outline" size="sm"
        className={cn("h-7 text-xs", row.original.is_active ? "text-rose-500 border-rose-500/30 hover:bg-rose-500/10" : "text-emerald-500 border-emerald-500/30 hover:bg-emerald-500/10")}
        onClick={(e) => { e.stopPropagation(); handleToggle(row.original) }} disabled={mutLoading}>
        {row.original.is_active ? 'Deactivate' : 'Activate'}
      </Button>
    ), enableSorting: false },
  ], [mutLoading])

  const strategyColumnLabels: Record<string, string> = { name: 'Strategy', instruments: 'Instruments', source: 'Source', status: 'Status', action: 'Action' }

  const renderStrategies = (strategies: StrategyConfig[]) => {
    if (strategies.length === 0) {
      return <p className="text-sm text-muted-foreground py-8 text-center">No strategies in this tab.</p>
    }

    if (viewMode === 'grid') {
      return (
        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
          {strategies.map((s) => (
            <StrategyCard key={s.key} s={s} onToggle={() => handleToggle(s)} loading={mutLoading} />
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
        columnLabels={strategyColumnLabels}
        filterPlaceholder="Filter strategies..."
        showDownload={false}
      />
    )
  }

  return (
    <div className="container mx-auto space-y-4 p-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">Backtests</h1>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => navigate('/backtest/new')}>
            <Play className="h-3.5 w-3.5 mr-1.5" />
            Run New Backtest
          </Button>
          <Button variant="outline" size="sm" onClick={() => navigate('/backtest/results')}>
            View History
          </Button>
        </div>
      </div>

      <Card>
        <CardContent className="p-4">
          <div className="flex items-center justify-between mb-4">
            <Tabs defaultValue="active" className="flex-1">
              <div className="flex items-center justify-between">
                <TabsList className="h-9">
                  <TabsTrigger value="active" className="text-xs px-4">
                    Active <Badge variant="secondary" className="ml-1.5 text-[10px]">{active.length}</Badge>
                  </TabsTrigger>
                  <TabsTrigger value="inactive" className="text-xs px-4">
                    Inactive <Badge variant="secondary" className="ml-1.5 text-[10px]">{inactive.length}</Badge>
                  </TabsTrigger>
                  <TabsTrigger value="all" className="text-xs px-4">
                    All <Badge variant="secondary" className="ml-1.5 text-[10px]">{active.length + inactive.length}</Badge>
                  </TabsTrigger>
                </TabsList>

                <div className="flex items-center border rounded-md overflow-hidden">
                  <button onClick={() => setViewMode('table')} className={`p-1.5 ${viewMode === 'table' ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:bg-muted/50'}`} title="Table view">
                    <List className="h-3.5 w-3.5" />
                  </button>
                  <button onClick={() => setViewMode('grid')} className={`p-1.5 ${viewMode === 'grid' ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:bg-muted/50'}`} title="Grid view">
                    <LayoutGrid className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>

              {isLoading ? (
                <p className="text-sm text-muted-foreground py-8 text-center">Loading strategies...</p>
              ) : (
                <>
                  <TabsContent value="active" className="mt-4">
                    {activateMut.isPending && <p className="text-xs text-muted-foreground animate-pulse mb-2">Running backtests on activated strategy...</p>}
                    {renderStrategies(active)}
                  </TabsContent>
                  <TabsContent value="inactive" className="mt-4">
                    {renderStrategies(inactive)}
                  </TabsContent>
                  <TabsContent value="all" className="mt-4">
                    {renderStrategies([...active, ...inactive])}
                  </TabsContent>
                </>
              )}
            </Tabs>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
