import { webClient } from './client'

/** Where prices come from: 'demo' is a synthetic series, 'db' the local Historify store. */
export type BacktestSource = 'demo' | 'db'

/** One point on the equity curve — the shape PortfolioLineChart consumes. */
export interface BacktestCurvePoint {
  date: string
  value: number
}

/** The knobs the backtest engine takes for one run. */
export interface BacktestRunRequest {
  source: BacktestSource
  symbol: string
  exchange: string
  interval: string
  start: string
  end: string
  capital: number
  cost: string
  strategy?: string
}

export interface StrategyOption {
  key: string
  name: string
  description: string
}

export async function getBacktestStrategies(): Promise<StrategyOption[]> {
  const { data } = await webClient.get<{ status: string; strategies: StrategyOption[] }>(
    '/backtest/api/strategies',
  )
  return data.strategies
}

/** Headline numbers for one run. Null where the engine could not compute honestly. */
export interface BacktestMetrics {
  net_pnl: number | null
  sharpe: number | null
  max_drawdown_pct: number | null
  n_trades: number | null
  fees_total: number | null
}

/** One executed trade. Shape is engine-defined, kept permissive on purpose. */
export interface BacktestTrade {
  [key: string]: unknown
}

export interface BacktestRunResponse {
  status: 'success' | 'error'
  message?: string
  run_id?: number
  strategy: string
  strategy_description?: string
  strategy_source?: string
  start?: string
  end?: string
  symbol: string
  exchange: string
  interval: string
  source: string
  capital: number
  cost_model: string
  n_bars: number
  metrics: BacktestMetrics
  equity: BacktestCurvePoint[]
  trades: BacktestTrade[]
  ohlc?: { time: string; open: number; high: number; low: number; close: number }[]
  xirr_pct?: number | null
  margin_used?: number | null
  return_on_margin_pct?: number | null
  data_note?: string | null
  per_instrument?: { symbol: string; exchange: string; n_trades: number; net_pnl: number; pnl_pct: number; fees_total: number; sharpe: number | null; max_drawdown_pct: number | null; capital_allocated: number }[]
}

/**
 * Run one backtest. This is a session/web route (not under /api/v1), so it
 * goes through webClient, which attaches the CSRF token for the POST.
 */
export async function runBacktest(req: BacktestRunRequest): Promise<BacktestRunResponse> {
  const { data } = await webClient.post<BacktestRunResponse>('/backtest/api/run', req)
  return data
}

export interface BacktestRunSummary {
  id: number
  created_at: string
  strategy: string
  strategy_description: string | null
  symbol: string
  exchange: string
  interval: string
  source: string
  start_date: string | null
  end_date: string | null
  capital: number
  cost_model: string
  n_bars: number
  n_trades: number
  net_pnl: number
  fees_total: number
  sharpe: number | null
  max_drawdown_pct: number | null
  xirr_pct: number | null
  margin_used: number | null
  return_on_margin_pct: number | null
  is_favorite: boolean
  remarks: string | null
  strategy_source: string | null
  status: string
}

export async function toggleFavorite(runId: number): Promise<{ is_favorite: boolean }> {
  const { data } = await webClient.post(`/backtest/api/run/${runId}/favorite`)
  return data
}

export async function updateRemarks(runId: number, remarks: string): Promise<{ remarks: string }> {
  const { data } = await webClient.post(`/backtest/api/run/${runId}/remarks`, { remarks })
  return data
}

export async function getBacktestHistory(limit = 50): Promise<BacktestRunSummary[]> {
  const { data } = await webClient.get<{ status: string; runs: BacktestRunSummary[] }>(
    `/backtest/api/history?limit=${limit}`,
  )
  return data.runs
}

export async function getBacktestDetail(runId: number): Promise<BacktestRunResponse> {
  const { data } = await webClient.get<BacktestRunResponse & { status: string }>(
    `/backtest/api/run/${runId}`,
  )
  return data as BacktestRunResponse
}

// ---------------------------------------------------------------------------
// Monte Carlo simulation
// ---------------------------------------------------------------------------

export interface MonteCarloTradeStats {
  total_pnl: number
  avg_pnl_per_trade: number
  win_rate: number
  avg_win: number
  avg_loss: number
  profit_factor: number | null
}

export interface MonteCarloSimResults {
  final_equity_median: number
  final_equity_p5: number
  final_equity_p95: number
  final_equity_worst: number
  final_equity_best: number
  max_drawdown_median: number
  max_drawdown_p95: number
  probability_of_ruin: number
  probability_of_profit: number
}

export interface MonteCarloPercentileCurves {
  p5: number[]
  p25: number[]
  p50: number[]
  p75: number[]
  p95: number[]
}

export interface MonteCarloResult {
  status: string
  n_trades: number
  n_simulations: number
  trade_stats: MonteCarloTradeStats
  simulation_results: MonteCarloSimResults
  percentile_curves: MonteCarloPercentileCurves
}

export async function getMonteCarloSimulation(runId: number): Promise<MonteCarloResult> {
  const { data } = await webClient.get<MonteCarloResult>(
    `/backtest/api/run/${runId}/montecarlo`,
  )
  return data
}

// Strategy config (active/inactive)
export interface StrategyConfig {
  key: string
  name: string
  description: string
  source: string
  is_active: boolean
  default_instruments: string[]
  default_interval: string
  default_capital: number
  default_cost: string
  remarks: string
}

export async function getStrategyConfigs(): Promise<{ active: StrategyConfig[]; inactive: StrategyConfig[] }> {
  const { data } = await webClient.get<{ active: StrategyConfig[]; inactive: StrategyConfig[] }>('/backtest/api/strategy-configs')
  return data
}

export async function activateStrategy(key: string, instruments?: string[]): Promise<unknown> {
  const { data } = await webClient.post(`/backtest/api/strategy-configs/${key}/activate`, { instruments })
  return data
}

export async function deactivateStrategy(key: string): Promise<unknown> {
  const { data } = await webClient.post(`/backtest/api/strategy-configs/${key}/deactivate`)
  return data
}

// Strategy overview (config + latest backtest performance merged)
export interface StrategyOverview extends StrategyConfig {
  run_id: number | null
  net_pnl: number | null
  fees_total: number | null
  n_trades: number | null
  sharpe: number | null
  max_drawdown_pct: number | null
  xirr_pct: number | null
  return_on_margin_pct: number | null
  capital: number | null
  symbol: string | null
  interval: string | null
  n_bars: number | null
  created_at: string | null
}

export async function getStrategyOverview(): Promise<{ active: StrategyOverview[]; inactive: StrategyOverview[]; all: StrategyOverview[] }> {
  const { data } = await webClient.get<{ active: StrategyOverview[]; inactive: StrategyOverview[]; all: StrategyOverview[] }>('/backtest/api/strategy-overview')
  return data
}
