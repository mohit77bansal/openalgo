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
}

/**
 * Run one backtest. This is a session/web route (not under /api/v1), so it
 * goes through webClient, which attaches the CSRF token for the POST.
 */
export async function runBacktest(req: BacktestRunRequest): Promise<BacktestRunResponse> {
  const { data } = await webClient.post<BacktestRunResponse>('/backtest/api/run', req)
  return data
}
