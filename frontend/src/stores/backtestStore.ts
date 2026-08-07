import { create } from 'zustand'
import type { BacktestRunRequest, BacktestRunResponse } from '@/api/backtest'

/**
 * Holds the most recent backtest result so the results sub-page can render it
 * without re-running the backtest. Not persisted: a stale result surviving a
 * reload would look current when the inputs behind it may have changed.
 */
interface BacktestStore {
  result: BacktestRunResponse | null
  request: BacktestRunRequest | null
  setResult: (result: BacktestRunResponse, request: BacktestRunRequest) => void
  clear: () => void
}

export const useBacktestStore = create<BacktestStore>((set) => ({
  result: null,
  request: null,
  setResult: (result, request) => set({ result, request }),
  clear: () => set({ result: null, request: null }),
}))
