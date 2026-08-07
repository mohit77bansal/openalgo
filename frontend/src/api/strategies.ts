import { webClient } from './client'

export interface StrategyInfo {
  name: string
  description: string
  parameters: Record<string, string>
  risk_profile: Record<string, string>
}

export interface SpreadSignal {
  direction: string
  trigger_price: number
  buy_strike: number
  sell_strike: number
  buy_symbol: string
  sell_symbol: string
  option_type: string
  spread_width: number
  estimated_debit: number
  max_loss_per_lot: number
  max_profit_per_lot: number
  rr_ratio: number
  or_high: number
  or_low: number
  spot_at_signal: number
  expiry: string
}

export interface SignalResponse {
  status: string
  signal: SpreadSignal | null
  message?: string
}

export interface ExecuteResponse {
  status: string
  direction?: string
  buy_symbol?: string
  sell_symbol?: string
  lots?: number
  quantity?: number
  max_loss?: number
  max_profit?: number
  rr_ratio?: number
  message?: string
}

export async function listStrategies(): Promise<{ strategies: StrategyInfo[] }> {
  const { data } = await webClient.get('/strategies/api/list')
  return data
}

export async function computeSignal(params: {
  api_key: string
  or_high: number
  or_low: number
  current_spot: number
  nearest_expiry: string
}): Promise<SignalResponse> {
  const { data } = await webClient.post('/strategies/api/signal', params)
  return data
}

export async function executeStrategy(params: {
  api_key: string
  or_high: number
  or_low: number
  current_spot: number
  nearest_expiry: string
  lots?: number
}): Promise<ExecuteResponse> {
  const { data } = await webClient.post('/strategies/api/execute', params)
  return data
}
