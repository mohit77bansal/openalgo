import { useState } from 'react'
import {
  Activity,
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  CircleDot,
  Eye,
  Pause,
  Play,
  Plus,
  Power,
  ShieldAlert,
  Square,
  Zap,
} from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { showToast } from '@/utils/toast'
import {
  createAccount,
  createStrategy,
  emergencyStop,
  fetchAccounts,
  fetchStrategyLogs,
  fetchStrategies,
  pauseStrategy,
  resumeStrategy,
  startStrategy,
  stopStrategy,
} from '@/api/live'
import type { CreateAccountParams, CreateStrategyParams, LiveStrategy, StrategyLog } from '@/api/live'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const BROKER_OPTIONS = ['angelone', 'upstox', 'fyers', 'zerodha', 'dhan', 'samco'] as const

const EXCHANGE_OPTIONS = ['NSE', 'BSE', 'NFO', 'BFO', 'MCX', 'CDS'] as const

const INTERVAL_OPTIONS = ['1m', '3m', '5m', '15m', '30m', '1h', '1d'] as const

const POLL_INTERVAL = 5_000

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface StatusConfig {
  readonly label: string
  readonly dotClass: string
  readonly badgeClass: string
}

function getStatusConfig(status: LiveStrategy['status']): StatusConfig {
  switch (status) {
    case 'RUNNING':
      return {
        label: 'Running',
        dotClass: 'bg-emerald-500 animate-pulse',
        badgeClass: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-500',
      }
    case 'PAUSED':
      return {
        label: 'Paused',
        dotClass: 'bg-amber-500',
        badgeClass: 'border-amber-500/30 bg-amber-500/10 text-amber-500',
      }
    case 'STOPPED':
      return {
        label: 'Stopped',
        dotClass: 'bg-zinc-500',
        badgeClass: 'border-zinc-500/30 bg-zinc-500/10 text-zinc-400',
      }
    case 'ERROR':
      return {
        label: 'Error',
        dotClass: 'bg-rose-500',
        badgeClass: 'border-rose-500/30 bg-rose-500/10 text-rose-500',
      }
  }
}

function formatPnl(value: number): string {
  const prefix = value >= 0 ? '+' : ''
  return `${prefix}${value.toLocaleString('en-IN', { style: 'currency', currency: 'INR' })}`
}

function formatTimestamp(ts: string | null): string {
  if (!ts) return '--'
  return new Date(ts).toLocaleString('en-IN', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function Stat({
  label,
  value,
  tone,
}: {
  readonly label: string
  readonly value: string
  readonly tone?: 'good' | 'bad' | 'neutral'
}) {
  return (
    <div className="space-y-0.5">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p
        className={`text-sm font-semibold tabular-nums ${
          tone === 'good'
            ? 'text-emerald-500'
            : tone === 'bad'
              ? 'text-rose-500'
              : ''
        }`}
      >
        {value}
      </p>
    </div>
  )
}

function StatusBadge({ status }: { readonly status: LiveStrategy['status'] }) {
  const config = getStatusConfig(status)
  return (
    <Badge variant="outline" className={`gap-1.5 ${config.badgeClass}`}>
      <span className={`inline-block h-2 w-2 rounded-full ${config.dotClass}`} />
      {config.label}
    </Badge>
  )
}

// ---------------------------------------------------------------------------
// Logs Sheet
// ---------------------------------------------------------------------------

function LogsSheet({
  strategyId,
  strategyName,
  open,
  onOpenChange,
}: {
  readonly strategyId: number
  readonly strategyName: string
  readonly open: boolean
  readonly onOpenChange: (open: boolean) => void
}) {
  const { data, isLoading } = useQuery({
    queryKey: ['live', 'logs', strategyId],
    queryFn: () => fetchStrategyLogs(strategyId),
    enabled: open,
    refetchInterval: open ? POLL_INTERVAL : false,
  })

  const logs: readonly StrategyLog[] = data?.logs ?? []

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Activity - {strategyName}</SheetTitle>
          <SheetDescription>Every evaluation, signal and order — newest first</SheetDescription>
        </SheetHeader>
        <div className="mt-4 space-y-1.5 overflow-y-auto max-h-[calc(100vh-10rem)]">
          {isLoading && (
            <p className="text-sm text-muted-foreground py-8 text-center">Loading logs...</p>
          )}
          {!isLoading && logs.length === 0 && (
            <p className="text-sm text-muted-foreground py-8 text-center">No activity yet</p>
          )}
          {logs.map((log, i) => {
            const d = parseDetails(log.details_json)
            const time = new Date(log.timestamp).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
            const date = new Date(log.timestamp).toLocaleDateString('en-IN', { day: '2-digit', month: 'short' })

            if (log.action === 'EVAL') {
              return (
                <div key={`${log.timestamp}-${i}`} className="flex items-start gap-2 px-2 py-1 text-xs border-l-2 border-border/50">
                  <span className="text-muted-foreground tabular-nums shrink-0 w-24">{date} {time}</span>
                  {d?.price != null && <span className="tabular-nums font-medium shrink-0">{Number(d.price).toLocaleString('en-IN')}</span>}
                  <span className="text-muted-foreground min-w-0">{d?.reason ?? 'evaluated'}</span>
                </div>
              )
            }

            const isOrder = log.action === 'ORDER_PLACED' || log.action === 'ORDER_FILLED'
            const isError = log.action === 'ERROR' || log.action === 'RISK_TRIGGERED'
            return (
              <div
                key={`${log.timestamp}-${i}`}
                className={
                  'rounded-md border px-3 py-2 text-sm space-y-1 ' +
                  (isOrder ? 'border-emerald-500/40 bg-emerald-500/5' : isError ? 'border-rose-500/40 bg-rose-500/5' : '')
                }
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium">{log.action}</span>
                  <span className="text-xs text-muted-foreground">{date} {time}</span>
                </div>
                {d?.reason && <p className="text-xs text-muted-foreground">{d.reason}</p>}
                {!d?.reason && log.details_json && (
                  <pre className="text-xs text-muted-foreground whitespace-pre-wrap break-all">
                    {log.details_json}
                  </pre>
                )}
                {log.pnl_at_action !== 0 && (
                  <p className="text-xs tabular-nums">
                    P&L:{' '}
                    <span className={log.pnl_at_action >= 0 ? 'text-emerald-500' : 'text-rose-500'}>
                      {formatPnl(log.pnl_at_action)}
                    </span>
                  </p>
                )}
              </div>
            )
          })}
        </div>
      </SheetContent>
    </Sheet>
  )
}

// ---------------------------------------------------------------------------
// Live heartbeat — what the strategy saw on its last evaluation
// ---------------------------------------------------------------------------

interface EvalDetails {
  price?: number | null
  or_high?: number | null
  or_low?: number | null
  bars_collected?: number
  reason?: string
  action?: string | null
}

function parseDetails(raw: string | null | undefined): EvalDetails | null {
  if (!raw) return null
  try { return JSON.parse(raw) as EvalDetails } catch { return null }
}

function LiveHeartbeat({ strategyId, isRunning }: { readonly strategyId: number; readonly isRunning: boolean }) {
  const { data } = useQuery({
    queryKey: ['live', 'heartbeat', strategyId],
    queryFn: () => fetchStrategyLogs(strategyId),
    enabled: isRunning,
    refetchInterval: isRunning ? 15_000 : false,
  })

  if (!isRunning) return null

  const logs: readonly StrategyLog[] = data?.logs ?? []
  const lastEval = logs.find((l) => l.action === 'EVAL')

  if (!lastEval) {
    return (
      <div className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">
        Waiting for first evaluation... (polls every strategy interval)
      </div>
    )
  }

  const d = parseDetails(lastEval.details_json)
  const ts = new Date(lastEval.timestamp).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })

  return (
    <div className="rounded-md border bg-muted/30 px-3 py-2 space-y-1">
      <div className="flex items-center justify-between text-xs">
        <span className="flex items-center gap-1.5 font-medium">
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
            <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
          </span>
          Live — last checked {ts}
        </span>
        {d?.price != null && <span className="tabular-nums font-medium">{Number(d.price).toLocaleString('en-IN')}</span>}
      </div>
      {d?.reason && <p className="text-xs text-muted-foreground">{d.reason}</p>}
      {(d?.or_high != null || d?.or_low != null) && (
        <div className="flex gap-3 text-[10px] text-muted-foreground tabular-nums">
          {d.or_low != null && <span>OR low: {Number(d.or_low).toFixed(0)}</span>}
          {d.or_high != null && <span>OR high: {Number(d.or_high).toFixed(0)}</span>}
          {d.bars_collected != null && <span>{d.bars_collected} bars</span>}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Strategy Card
// ---------------------------------------------------------------------------

function StrategyCard({ strategy }: { readonly strategy: LiveStrategy }) {
  const queryClient = useQueryClient()
  const [logsOpen, setLogsOpen] = useState(false)
  const [stopDialogOpen, setStopDialogOpen] = useState(false)

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['live', 'strategies'] })

  const startMut = useMutation({
    mutationFn: () => startStrategy(strategy.id),
    onSuccess: () => {
      showToast.success(`Started ${strategy.strategy_name}`)
      invalidate()
    },
    onError: () => showToast.error(`Failed to start ${strategy.strategy_name}`),
  })

  const pauseMut = useMutation({
    mutationFn: () => pauseStrategy(strategy.id),
    onSuccess: () => {
      showToast.success(`Paused ${strategy.strategy_name}`)
      invalidate()
    },
    onError: () => showToast.error(`Failed to pause ${strategy.strategy_name}`),
  })

  const resumeMut = useMutation({
    mutationFn: () => resumeStrategy(strategy.id),
    onSuccess: () => {
      showToast.success(`Resumed ${strategy.strategy_name}`)
      invalidate()
    },
    onError: () => showToast.error(`Failed to resume ${strategy.strategy_name}`),
  })

  const stopMut = useMutation({
    mutationFn: () => stopStrategy(strategy.id),
    onSuccess: () => {
      showToast.success(`Stopped ${strategy.strategy_name}`)
      setStopDialogOpen(false)
      invalidate()
    },
    onError: () => showToast.error(`Failed to stop ${strategy.strategy_name}`),
  })

  const canStart = strategy.status === 'PAUSED' || strategy.status === 'STOPPED'
  const canPause = strategy.status === 'RUNNING'
  const canResume = strategy.status === 'PAUSED'
  const canStop = strategy.status === 'RUNNING' || strategy.status === 'PAUSED'
  const pnlTone: 'good' | 'bad' | 'neutral' =
    strategy.total_pnl > 0 ? 'good' : strategy.total_pnl < 0 ? 'bad' : 'neutral'

  return (
    <>
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-start justify-between gap-2">
            <div className="space-y-1 min-w-0">
              <CardTitle className="flex items-center gap-2 text-base">
                <Zap className="h-4 w-4 shrink-0 text-amber-500" />
                <span className="truncate">{strategy.strategy_name}</span>
              </CardTitle>
              <p className="text-xs text-muted-foreground">
                {strategy.symbol} / {strategy.exchange} &middot; {strategy.interval} &middot;{' '}
                {strategy.account_name}
              </p>
            </div>
            <StatusBadge status={strategy.status} />
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Metrics row */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
            <Stat label="Total P&L" value={formatPnl(strategy.total_pnl)} tone={pnlTone} />
            <Stat label="Trades" value={String(strategy.total_trades)} />
            <Stat label="Capital" value={`${(strategy.capital_allocated / 100_000).toFixed(1)}L`} />
            <Stat label="Lots" value={String(strategy.lots)} />
            <Stat label="Started" value={formatTimestamp(strategy.started_at)} />
          </div>

          {/* Live heartbeat: what the strategy saw on the last poll */}
          <LiveHeartbeat strategyId={strategy.id} isRunning={strategy.status === 'RUNNING'} />

          {strategy.remarks && (
            <p className="text-xs text-muted-foreground italic">{strategy.remarks}</p>
          )}

          {/* Action buttons */}
          <div className="flex flex-wrap gap-2">
            {canStart && !canResume && (
              <Button
                size="sm"
                variant="outline"
                className="border-emerald-500/30 text-emerald-500 hover:bg-emerald-500/10"
                disabled={startMut.isPending}
                onClick={() => startMut.mutate()}
              >
                <Play className="mr-1.5 h-3.5 w-3.5" />
                Start
              </Button>
            )}
            {canResume && (
              <Button
                size="sm"
                variant="outline"
                className="border-emerald-500/30 text-emerald-500 hover:bg-emerald-500/10"
                disabled={resumeMut.isPending}
                onClick={() => resumeMut.mutate()}
              >
                <Play className="mr-1.5 h-3.5 w-3.5" />
                Resume
              </Button>
            )}
            {canPause && (
              <Button
                size="sm"
                variant="outline"
                className="border-amber-500/30 text-amber-500 hover:bg-amber-500/10"
                disabled={pauseMut.isPending}
                onClick={() => pauseMut.mutate()}
              >
                <Pause className="mr-1.5 h-3.5 w-3.5" />
                Pause
              </Button>
            )}
            {canStop && (
              <Dialog open={stopDialogOpen} onOpenChange={setStopDialogOpen}>
                <DialogTrigger asChild>
                  <Button
                    size="sm"
                    variant="outline"
                    className="border-rose-500/30 text-rose-500 hover:bg-rose-500/10"
                  >
                    <Square className="mr-1.5 h-3.5 w-3.5" />
                    Stop
                  </Button>
                </DialogTrigger>
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>Stop Strategy</DialogTitle>
                    <DialogDescription>
                      This will stop <strong>{strategy.strategy_name}</strong> and close all
                      positions. This cannot be undone.
                    </DialogDescription>
                  </DialogHeader>
                  <DialogFooter>
                    <DialogClose asChild>
                      <Button variant="outline">Cancel</Button>
                    </DialogClose>
                    <Button
                      variant="destructive"
                      disabled={stopMut.isPending}
                      onClick={() => stopMut.mutate()}
                    >
                      {stopMut.isPending ? 'Stopping...' : 'Confirm Stop'}
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            )}
            <Button size="sm" variant="ghost" onClick={() => setLogsOpen(true)}>
              <Eye className="mr-1.5 h-3.5 w-3.5" />
              Logs
            </Button>
          </div>
        </CardContent>
      </Card>

      <LogsSheet
        strategyId={strategy.id}
        strategyName={strategy.strategy_name}
        open={logsOpen}
        onOpenChange={setLogsOpen}
      />
    </>
  )
}

// ---------------------------------------------------------------------------
// Accounts Section
// ---------------------------------------------------------------------------

function AccountsSection() {
  const [open, setOpen] = useState(false)
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState<CreateAccountParams>({
    account_name: '',
    broker: '',
    client_code: '',
  })

  const queryClient = useQueryClient()

  const { data } = useQuery({
    queryKey: ['live', 'accounts'],
    queryFn: fetchAccounts,
    staleTime: 30_000,
  })

  const accounts = data?.accounts ?? []

  const createMut = useMutation({
    mutationFn: () => createAccount(form),
    onSuccess: () => {
      showToast.success('Account connected')
      setForm({ account_name: '', broker: '', client_code: '' })
      setShowForm(false)
      queryClient.invalidateQueries({ queryKey: ['live', 'accounts'] })
    },
    onError: () => showToast.error('Failed to connect account'),
  })

  const canSubmit = form.account_name.trim() && form.broker && form.client_code.trim()

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <Card>
        <CollapsibleTrigger asChild>
          <CardHeader className="cursor-pointer select-none">
            <div className="flex items-center justify-between">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Activity className="h-4 w-4" />
                Broker Accounts ({accounts.length})
              </CardTitle>
              {open ? (
                <ChevronUp className="h-4 w-4 text-muted-foreground" />
              ) : (
                <ChevronDown className="h-4 w-4 text-muted-foreground" />
              )}
            </div>
          </CardHeader>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <CardContent className="space-y-3 pt-0">
            {accounts.length === 0 && !showForm && (
              <p className="text-sm text-muted-foreground">No accounts connected yet.</p>
            )}

            {accounts.map((acc) => (
              <div
                key={acc.id}
                className="flex items-center gap-3 rounded-md border px-3 py-2 text-sm"
              >
                <span
                  className={`h-2 w-2 rounded-full ${acc.is_active ? 'bg-emerald-500' : 'bg-zinc-500'}`}
                />
                <span className="font-medium">{acc.account_name}</span>
                <Badge variant="outline" className="text-xs">
                  {acc.broker}
                </Badge>
                <span className="text-muted-foreground ml-auto text-xs">{acc.client_code}</span>
              </div>
            ))}

            {showForm && (
              <div className="space-y-3 rounded-md border p-3">
                <div className="grid gap-3 sm:grid-cols-3">
                  <div className="space-y-1">
                    <Label htmlFor="acc-name">Account Name</Label>
                    <Input
                      id="acc-name"
                      placeholder="My AngelOne"
                      value={form.account_name}
                      onChange={(e) =>
                        setForm({ ...form, account_name: e.target.value })
                      }
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="acc-broker">Broker</Label>
                    <Select
                      value={form.broker}
                      onValueChange={(v) => setForm({ ...form, broker: v })}
                    >
                      <SelectTrigger id="acc-broker">
                        <SelectValue placeholder="Select broker" />
                      </SelectTrigger>
                      <SelectContent>
                        {BROKER_OPTIONS.map((b) => (
                          <SelectItem key={b} value={b}>
                            {b.charAt(0).toUpperCase() + b.slice(1)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="acc-code">Client Code</Label>
                    <Input
                      id="acc-code"
                      placeholder="A12345"
                      value={form.client_code}
                      onChange={(e) =>
                        setForm({ ...form, client_code: e.target.value })
                      }
                    />
                  </div>
                </div>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    disabled={!canSubmit || createMut.isPending}
                    onClick={() => createMut.mutate()}
                  >
                    {createMut.isPending ? 'Connecting...' : 'Connect'}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setShowForm(false)}
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            )}

            {!showForm && (
              <Button size="sm" variant="outline" onClick={() => setShowForm(true)}>
                <Plus className="mr-1.5 h-3.5 w-3.5" />
                Connect Account
              </Button>
            )}
          </CardContent>
        </CollapsibleContent>
      </Card>
    </Collapsible>
  )
}

// ---------------------------------------------------------------------------
// Add Strategy Dialog
// ---------------------------------------------------------------------------

function AddStrategyDialog({
  open,
  onOpenChange,
}: {
  readonly open: boolean
  readonly onOpenChange: (open: boolean) => void
}) {
  const queryClient = useQueryClient()

  const { data: accountsData } = useQuery({
    queryKey: ['live', 'accounts'],
    queryFn: fetchAccounts,
    staleTime: 30_000,
  })

  const accounts = accountsData?.accounts ?? []

  const [form, setForm] = useState<CreateStrategyParams>({
    strategy_key: '',
    account_name: '',
    symbol: '',
    exchange: 'NSE',
    interval: '5m',
    lots: 1,
    capital_allocated: 100_000,
  })

  const createMut = useMutation({
    mutationFn: () => createStrategy(form),
    onSuccess: () => {
      showToast.success('Strategy created')
      onOpenChange(false)
      setForm({
        strategy_key: '',
        account_name: '',
        symbol: '',
        exchange: 'NSE',
        interval: '5m',
        lots: 1,
        capital_allocated: 100_000,
      })
      queryClient.invalidateQueries({ queryKey: ['live', 'strategies'] })
    },
    onError: () => showToast.error('Failed to create strategy'),
  })

  const canSubmit =
    form.strategy_key.trim() &&
    form.account_name.trim() &&
    form.symbol.trim() &&
    form.lots > 0 &&
    form.capital_allocated > 0

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Add Live Strategy</DialogTitle>
          <DialogDescription>
            Deploy a strategy from the backtest registry to a connected broker account.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4 py-2">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="strat-key">Strategy</Label>
              <Input
                id="strat-key"
                placeholder="nifty_straddle"
                value={form.strategy_key}
                onChange={(e) => setForm({ ...form, strategy_key: e.target.value })}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="strat-account">Account</Label>
              <Select
                value={form.account_name}
                onValueChange={(v) => setForm({ ...form, account_name: v })}
              >
                <SelectTrigger id="strat-account">
                  <SelectValue placeholder="Select account" />
                </SelectTrigger>
                <SelectContent>
                  {accounts.map((a) => (
                    <SelectItem key={a.id} value={a.account_name}>
                      {a.account_name} ({a.broker})
                    </SelectItem>
                  ))}
                  {accounts.length === 0 && (
                    <SelectItem value="__none" disabled>
                      No accounts - connect one first
                    </SelectItem>
                  )}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-3">
            <div className="space-y-1">
              <Label htmlFor="strat-symbol">Symbol</Label>
              <Input
                id="strat-symbol"
                placeholder="NIFTY"
                value={form.symbol}
                onChange={(e) => setForm({ ...form, symbol: e.target.value })}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="strat-exchange">Exchange</Label>
              <Select
                value={form.exchange}
                onValueChange={(v) => setForm({ ...form, exchange: v })}
              >
                <SelectTrigger id="strat-exchange">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {EXCHANGE_OPTIONS.map((ex) => (
                    <SelectItem key={ex} value={ex}>
                      {ex}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="strat-interval">Interval</Label>
              <Select
                value={form.interval}
                onValueChange={(v) => setForm({ ...form, interval: v })}
              >
                <SelectTrigger id="strat-interval">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {INTERVAL_OPTIONS.map((iv) => (
                    <SelectItem key={iv} value={iv}>
                      {iv}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="strat-lots">Lots</Label>
              <Input
                id="strat-lots"
                type="number"
                min={1}
                value={form.lots}
                onChange={(e) =>
                  setForm({ ...form, lots: Number.parseInt(e.target.value) || 1 })
                }
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="strat-capital">Capital Allocated</Label>
              <Input
                id="strat-capital"
                type="number"
                min={0}
                value={form.capital_allocated}
                onChange={(e) =>
                  setForm({
                    ...form,
                    capital_allocated: Number.parseInt(e.target.value) || 0,
                  })
                }
              />
            </div>
          </div>
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">Cancel</Button>
          </DialogClose>
          <Button disabled={!canSubmit || createMut.isPending} onClick={() => createMut.mutate()}>
            {createMut.isPending ? 'Creating...' : 'Create Strategy'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ---------------------------------------------------------------------------
// Risk Monitor
// ---------------------------------------------------------------------------

function RiskMonitor({
  strategies,
}: {
  readonly strategies: readonly LiveStrategy[]
}) {
  const running = strategies.filter((s) => s.status === 'RUNNING')
  const dailyPnl = strategies.reduce((sum, s) => sum + s.total_pnl, 0)
  const openPositions = running.length
  const hasErrors = strategies.some((s) => s.status === 'ERROR')

  const pnlTone: 'good' | 'bad' | 'neutral' =
    dailyPnl > 0 ? 'good' : dailyPnl < 0 ? 'bad' : 'neutral'

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-sm">
          <ShieldAlert className="h-4 w-4" />
          Risk Monitor
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Stat
            label="Risk Status"
            value={hasErrors ? 'ALERT' : running.length > 0 ? 'ACTIVE' : 'IDLE'}
            tone={hasErrors ? 'bad' : running.length > 0 ? 'good' : 'neutral'}
          />
          <Stat label="Aggregate P&L" value={formatPnl(dailyPnl)} tone={pnlTone} />
          <Stat label="Active Strategies" value={String(openPositions)} />
          <Stat
            label="Kill Switch"
            value={running.length > 0 ? 'Armed' : 'Standby'}
            tone={running.length > 0 ? 'good' : 'neutral'}
          />
        </div>
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

export default function LiveStrategies() {
  const [addDialogOpen, setAddDialogOpen] = useState(false)

  const queryClient = useQueryClient()

  const { data, isLoading } = useQuery({
    queryKey: ['live', 'strategies'],
    queryFn: fetchStrategies,
    refetchInterval: POLL_INTERVAL,
  })

  const strategies: readonly LiveStrategy[] = data?.strategies ?? []

  const running = strategies.filter((s) => s.status === 'RUNNING').length
  const paused = strategies.filter((s) => s.status === 'PAUSED').length
  const stopped = strategies.filter(
    (s) => s.status === 'STOPPED' || s.status === 'ERROR',
  ).length

  const emergencyMut = useMutation({
    mutationFn: emergencyStop,
    onSuccess: (res) => {
      showToast.success(`Emergency stop: ${res.stopped_count} strategies stopped`)
      queryClient.invalidateQueries({ queryKey: ['live', 'strategies'] })
    },
    onError: () => showToast.error('Emergency stop failed'),
  })

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Zap className="h-6 w-6 text-amber-500" />
            Live Strategies
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {running > 0 && (
              <span className="text-emerald-500 font-medium">{running} running</span>
            )}
            {running > 0 && (paused > 0 || stopped > 0) && ' / '}
            {paused > 0 && (
              <span className="text-amber-500 font-medium">{paused} paused</span>
            )}
            {paused > 0 && stopped > 0 && ' / '}
            {stopped > 0 && (
              <span className="text-muted-foreground">{stopped} stopped</span>
            )}
            {strategies.length === 0 && 'No strategies deployed yet'}
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => setAddDialogOpen(true)}>
            <Plus className="mr-1.5 h-4 w-4" />
            Add Strategy
          </Button>
          <Dialog>
            <DialogTrigger asChild>
              <Button
                variant="destructive"
                className="bg-rose-600 hover:bg-rose-700 shadow-lg shadow-rose-600/20"
                disabled={running === 0}
              >
                <Power className="mr-1.5 h-4 w-4" />
                Emergency Stop
              </Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle className="flex items-center gap-2 text-rose-500">
                  <AlertTriangle className="h-5 w-5" />
                  Emergency Stop All
                </DialogTitle>
                <DialogDescription>
                  This will immediately stop <strong>all running strategies</strong> and close all
                  open positions. This action cannot be undone.
                </DialogDescription>
              </DialogHeader>
              <DialogFooter>
                <DialogClose asChild>
                  <Button variant="outline">Cancel</Button>
                </DialogClose>
                <Button
                  variant="destructive"
                  disabled={emergencyMut.isPending}
                  onClick={() => emergencyMut.mutate()}
                >
                  {emergencyMut.isPending ? 'Stopping...' : 'Stop Everything'}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      </div>

      {/* Accounts */}
      <AccountsSection />

      {/* Strategies */}
      {isLoading && (
        <div className="flex items-center justify-center py-12">
          <p className="text-sm text-muted-foreground">Loading strategies...</p>
        </div>
      )}

      {!isLoading && strategies.length === 0 && (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-12 text-center">
            <CircleDot className="h-10 w-10 text-muted-foreground/40 mb-3" />
            <p className="text-sm font-medium">No live strategies</p>
            <p className="text-xs text-muted-foreground mt-1">
              Click "Add Strategy" to deploy your first live strategy.
            </p>
          </CardContent>
        </Card>
      )}

      <div className="space-y-4">
        {strategies.map((s) => (
          <StrategyCard key={s.id} strategy={s} />
        ))}
      </div>

      {/* Risk Monitor */}
      {strategies.length > 0 && <RiskMonitor strategies={strategies} />}

      {/* Add Strategy Dialog */}
      <AddStrategyDialog open={addDialogOpen} onOpenChange={setAddDialogOpen} />
    </div>
  )
}
