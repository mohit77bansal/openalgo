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
  strategy: string
  strategy_description?: string
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
  status: string
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
