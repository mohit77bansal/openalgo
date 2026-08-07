import { FlaskConical, Play, TrendingDown, TrendingUp } from 'lucide-react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuthStore } from '@/stores/authStore'
import { showToast } from '@/utils/toast'
import { listStrategies, computeSignal, executeStrategy } from '@/api/strategies'
import type { SpreadSignal } from '@/api/strategies'
import { useState } from 'react'

function Stat({ label, value, tone }: { label: string; value: string; tone?: 'good' | 'bad' | 'neutral' }) {
  return (
    <div className="space-y-1">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={`text-lg font-semibold tabular-nums ${tone === 'good' ? 'text-emerald-500' : tone === 'bad' ? 'text-rose-500' : ''}`}>
        {value}
      </p>
    </div>
  )
}

export default function AutoStrategies() {
  const { apiKey } = useAuthStore()
  const [orHigh, setOrHigh] = useState('')
  const [orLow, setOrLow] = useState('')
  const [spot, setSpot] = useState('')
  const [expiry, setExpiry] = useState('25AUG26')
  const [lots, setLots] = useState('1')
  const [signal, setSignal] = useState<SpreadSignal | null>(null)

  const { data: strategiesData } = useQuery({
    queryKey: ['strategies', 'list'],
    queryFn: listStrategies,
    staleTime: 5 * 60_000,
  })

  const signalMutation = useMutation({
    mutationFn: () => computeSignal({
      api_key: apiKey ?? '',
      or_high: Number.parseFloat(orHigh),
      or_low: Number.parseFloat(orLow),
      current_spot: Number.parseFloat(spot),
      nearest_expiry: expiry,
    }),
    onSuccess: (data) => {
      if (data.signal) {
        setSignal(data.signal)
        showToast.success(`${data.signal.direction} signal detected — R:R ${data.signal.rr_ratio.toFixed(1)}:1`)
      } else {
        setSignal(null)
        showToast.info(data.message || 'No valid signal')
      }
    },
    onError: () => showToast.error('Failed to compute signal'),
  })

  const executeMutation = useMutation({
    mutationFn: () => executeStrategy({
      api_key: apiKey ?? '',
      or_high: Number.parseFloat(orHigh),
      or_low: Number.parseFloat(orLow),
      current_spot: Number.parseFloat(spot),
      nearest_expiry: expiry,
      lots: Number.parseInt(lots) || 1,
    }),
    onSuccess: (data) => {
      if (data.status === 'success') {
        showToast.success(`Executed ${data.direction} spread — ${data.lots} lot(s), max loss ₹${data.max_loss?.toLocaleString()}`)
      } else {
        showToast.error(data.message || 'Execution failed')
      }
    },
    onError: () => showToast.error('Execution failed'),
  })

  const strategy = strategiesData?.strategies?.[0]

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Automated Strategies</h1>
        <p className="text-sm text-muted-foreground">Defined risk-reward strategies with paper trading</p>
      </div>

      {strategy && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <FlaskConical className="h-5 w-5" />
              {strategy.name}
              <Badge variant="outline" className="ml-auto">2:1 R:R</Badge>
            </CardTitle>
            <p className="text-sm text-muted-foreground">{strategy.description}</p>
          </CardHeader>
          <CardContent>
            <div className="grid gap-3 md:grid-cols-4">
              <Stat label="Target R:R" value={strategy.parameters.target_rr} tone="good" />
              <Stat label="Win rate needed" value={strategy.risk_profile.win_rate_needed_to_breakeven} />
              <Stat label="Typical win rate" value={strategy.risk_profile.typical_win_rate} tone="good" />
              <Stat label="OR Window" value={strategy.parameters.or_window} />
            </div>
            <div className="mt-3 grid gap-3 md:grid-cols-4">
              <Stat label="Entry cutoff" value={strategy.parameters.entry_window_end} />
              <Stat label="Exit time" value={strategy.parameters.exit_time} />
              <Stat label="Min OR range" value={strategy.parameters.min_or_range} />
              <Stat label="Max OR range" value={strategy.parameters.max_or_range} />
            </div>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Compute Signal</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 md:grid-cols-4">
            <div className="space-y-1">
              <Label htmlFor="or-high">OR High</Label>
              <Input id="or-high" type="number" placeholder="24650" value={orHigh} onChange={e => setOrHigh(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="or-low">OR Low</Label>
              <Input id="or-low" type="number" placeholder="24580" value={orLow} onChange={e => setOrLow(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="spot">Current Spot</Label>
              <Input id="spot" type="number" placeholder="24680" value={spot} onChange={e => setSpot(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="expiry">Nearest Expiry</Label>
              <Input id="expiry" placeholder="25AUG26" value={expiry} onChange={e => setExpiry(e.target.value)} />
            </div>
          </div>
          <div className="flex gap-3">
            <Button onClick={() => signalMutation.mutate()} disabled={signalMutation.isPending || !orHigh || !orLow || !spot}>
              Compute Signal
            </Button>
            <div className="space-y-1">
              <Label htmlFor="lots" className="sr-only">Lots</Label>
              <Input id="lots" type="number" className="w-20" min={1} max={10} value={lots} onChange={e => setLots(e.target.value)} />
            </div>
            <Button
              variant="default"
              onClick={() => executeMutation.mutate()}
              disabled={executeMutation.isPending || !signal || !orHigh || !orLow || !spot}
            >
              <Play className="mr-1.5 h-4 w-4" />
              Execute (Paper)
            </Button>
          </div>
        </CardContent>
      </Card>

      {signal && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              {signal.direction === 'BULL'
                ? <TrendingUp className="h-5 w-5 text-emerald-500" />
                : <TrendingDown className="h-5 w-5 text-rose-500" />
              }
              {signal.direction} Signal — {signal.option_type} Debit Spread
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid gap-3 md:grid-cols-4">
              <Stat label="Buy" value={signal.buy_symbol} />
              <Stat label="Sell" value={signal.sell_symbol} />
              <Stat label="Spread width" value={`${signal.spread_width} pts`} />
              <Stat label="Est. debit" value={`₹${signal.estimated_debit}/unit`} />
            </div>
            <div className="mt-3 grid gap-3 md:grid-cols-4">
              <Stat label="Max loss / lot" value={`₹${signal.max_loss_per_lot.toLocaleString()}`} tone="bad" />
              <Stat label="Max profit / lot" value={`₹${signal.max_profit_per_lot.toLocaleString()}`} tone="good" />
              <Stat label="R:R ratio" value={`${signal.rr_ratio.toFixed(1)}:1`} tone="good" />
              <Stat label="Trigger" value={`${signal.trigger_price.toFixed(1)} (${signal.direction === 'BULL' ? 'OR High' : 'OR Low'})`} />
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
