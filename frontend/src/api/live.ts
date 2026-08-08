import { webClient } from './client'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface LiveAccount {
  readonly id: number
  readonly account_name: string
  readonly broker: string
  readonly client_code: string
  readonly is_active: boolean
}

export interface LiveStrategy {
  readonly id: number
  readonly strategy_key: string
  readonly strategy_name: string
  readonly account_name: string
  readonly symbol: string
  readonly exchange: string
  readonly interval: string
  readonly lots: number
  readonly capital_allocated: number
  readonly status: 'RUNNING' | 'PAUSED' | 'STOPPED' | 'ERROR'
  readonly total_pnl: number
  readonly total_trades: number
  readonly started_at: string | null
  readonly paused_at: string | null
  readonly remarks: string | null
}

export interface StrategyPosition {
  readonly symbol: string
  readonly exchange: string
  readonly qty: number
  readonly avg_price: number
  readonly ltp: number
  readonly pnl: number
}

export interface RiskMetrics {
  readonly max_drawdown: number
  readonly sharpe_ratio: number
  readonly win_rate: number
  readonly avg_profit: number
  readonly avg_loss: number
}

export interface StrategyStatusDetail {
  readonly status: string
  readonly strategy: LiveStrategy
  readonly positions: readonly StrategyPosition[]
  readonly pnl: number
  readonly risk_metrics: RiskMetrics
}

export interface StrategyLog {
  readonly timestamp: string
  readonly action: string
  readonly details_json: string
  readonly pnl_at_action: number
}

export interface CreateAccountParams {
  readonly account_name: string
  readonly broker: string
  readonly client_code: string
}

export interface CreateStrategyParams {
  readonly strategy_key: string
  readonly account_name: string
  readonly symbol: string
  readonly exchange: string
  readonly interval: string
  readonly lots: number
  readonly capital_allocated: number
}

// ---------------------------------------------------------------------------
// Accounts
// ---------------------------------------------------------------------------

export async function fetchAccounts(): Promise<{ accounts: LiveAccount[] }> {
  const { data } = await webClient.get('/live/accounts')
  return data
}

export async function createAccount(
  params: CreateAccountParams,
): Promise<{ status: string; account: LiveAccount }> {
  const { data } = await webClient.post('/live/accounts', params)
  return data
}

// ---------------------------------------------------------------------------
// Strategies
// ---------------------------------------------------------------------------

export async function fetchStrategies(): Promise<{ strategies: LiveStrategy[] }> {
  const { data } = await webClient.get('/live/strategies')
  return data
}

export async function createStrategy(
  params: CreateStrategyParams,
): Promise<{ status: string; strategy: LiveStrategy }> {
  const { data } = await webClient.post('/live/strategies', params)
  return data
}

export async function startStrategy(id: number): Promise<{ status: string; message: string }> {
  const { data } = await webClient.post(`/live/strategies/${id}/start`)
  return data
}

export async function pauseStrategy(id: number): Promise<{ status: string; message: string }> {
  const { data } = await webClient.post(`/live/strategies/${id}/pause`)
  return data
}

export async function resumeStrategy(id: number): Promise<{ status: string; message: string }> {
  const { data } = await webClient.post(`/live/strategies/${id}/resume`)
  return data
}

export async function stopStrategy(id: number): Promise<{ status: string; message: string }> {
  const { data } = await webClient.post(`/live/strategies/${id}/stop`)
  return data
}

export async function fetchStrategyStatus(id: number): Promise<StrategyStatusDetail> {
  const { data } = await webClient.get(`/live/strategies/${id}/status`)
  return data
}

export async function fetchStrategyLogs(id: number): Promise<{ logs: StrategyLog[] }> {
  const { data } = await webClient.get(`/live/strategies/${id}/logs`)
  return data
}

// ---------------------------------------------------------------------------
// Emergency
// ---------------------------------------------------------------------------

export async function emergencyStop(): Promise<{ status: string; stopped_count: number }> {
  const { data } = await webClient.post('/live/emergency-stop')
  return data
}
