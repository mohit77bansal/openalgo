/**
 * Backtest — configure one run of the event-driven engine and read the result.
 *
 * The result lives in useBacktestStore, set here right before we navigate to
 * the report page, so a long report does not sit permanently below the form.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import { type BacktestSource, runBacktest, getBacktestHistory } from '@/api/backtest'
import type { BacktestRunSummary } from '@/api/backtest'
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

  const [source, setSource] = useState<BacktestSource>('demo')
  const [symbol, setSymbol] = useState('NIFTY')
  const [exchange, setExchange] = useState('NSE')
  const [interval, setInterval] = useState('D')
  const [start, setStart] = useState(todayISO(1))
  const [end, setEnd] = useState(todayISO(0))
  const [capital, setCapital] = useState(1000000)
  const [cost, setCost] = useState('zerodha')

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
                  <SelectItem value="demo">Demo (synthetic)</SelectItem>
                  <SelectItem value="db">Historify (local)</SelectItem>
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

function BacktestHistory() {
  const { data: runs = [], isLoading } = useQuery({
    queryKey: ['backtest', 'history'],
    queryFn: () => getBacktestHistory(50),
    refetchOnWindowFocus: true,
  })

  if (isLoading) return <p className="text-sm text-muted-foreground">Loading history...</p>
  if (runs.length === 0) return <p className="text-sm text-muted-foreground">No backtests run yet. Run one above to see it here.</p>

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Backtest History</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th className="pb-2 pr-4">Time</th>
                <th className="pb-2 pr-4">Symbol</th>
                <th className="pb-2 pr-4">Source</th>
                <th className="pb-2 pr-4">Bars</th>
                <th className="pb-2 pr-4">Trades</th>
                <th className="pb-2 pr-4 text-right">Net P&L</th>
                <th className="pb-2 pr-4 text-right">Fees</th>
                <th className="pb-2 pr-4">Cost</th>
                <th className="pb-2">Status</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r: BacktestRunSummary) => (
                <tr key={r.id} className="border-b border-border/50 last:border-0">
                  <td className="py-2 pr-4 text-xs text-muted-foreground whitespace-nowrap">
                    {r.created_at ? new Date(r.created_at).toLocaleString('en-IN', { dateStyle: 'short', timeStyle: 'short' }) : '-'}
                  </td>
                  <td className="py-2 pr-4 font-medium">{r.symbol}/{r.exchange}</td>
                  <td className="py-2 pr-4">{r.source}</td>
                  <td className="py-2 pr-4 tabular-nums">{r.n_bars}</td>
                  <td className="py-2 pr-4 tabular-nums">{r.n_trades}</td>
                  <td className={`py-2 pr-4 text-right tabular-nums font-medium ${(r.net_pnl ?? 0) >= 0 ? 'text-emerald-500' : 'text-rose-500'}`}>
                    {r.net_pnl != null ? `₹${r.net_pnl.toLocaleString('en-IN', { maximumFractionDigits: 0 })}` : '-'}
                  </td>
                  <td className="py-2 pr-4 text-right tabular-nums">
                    {r.fees_total != null ? `₹${r.fees_total.toLocaleString('en-IN', { maximumFractionDigits: 0 })}` : '-'}
                  </td>
                  <td className="py-2 pr-4 text-xs">{r.cost_model}</td>
                  <td className="py-2">
                    <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${r.status === 'success' ? 'bg-emerald-500/10 text-emerald-500' : 'bg-rose-500/10 text-rose-500'}`}>
                      {r.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  )
}
