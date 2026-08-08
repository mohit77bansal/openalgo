/**
 * BacktestNew — configure and run a single backtest.
 *
 * Extracted from the original Backtest page so /backtest is a clean list
 * and /backtest/new is the dedicated form.
 */

import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import { type BacktestSource, runBacktest, getBacktestStrategies } from '@/api/backtest'
import type { StrategyOption } from '@/api/backtest'
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

export default function BacktestNew() {
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
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">New Backtest</h1>
          <p className="text-sm text-muted-foreground">
            Run the strategy over a symbol and read the equity curve, P&amp;L and costs. The demo
            source is synthetic, so it works without a broker login.
          </p>
        </div>
        <Button variant="outline" onClick={() => navigate('/backtest')}>
          Cancel
        </Button>
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
              <Label className="text-xs">Capital (&#8377;)</Label>
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
    </div>
  )
}
