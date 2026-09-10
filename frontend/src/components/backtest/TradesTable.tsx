import { useMemo } from 'react'
import { type ColumnDef } from '@tanstack/react-table'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { DataTable } from '@/components/ui/data-table'
import { cn } from '@/lib/utils'

const money = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined
    ? '-'
    : `₹${v.toLocaleString('en-IN', { maximumFractionDigits: dp })}`

interface ParsedTrade {
  idx: number
  instrument: string
  side: string
  entryTime: string
  entryPrice: number
  exitTime: string
  exitPrice: number
  qty: number
  pnl: number
  fees: number
  sign: number
}

const fmtTs = (ts: string) => {
  if (ts === '-') return '-'
  try { return new Date(ts).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false }) } catch { return ts.slice(0, 16) }
}

const R = { align: 'right' as const }

const tradeColumns: ColumnDef<ParsedTrade, unknown>[] = [
  { accessorKey: 'idx', header: '#', size: 50, cell: ({ getValue }) => <span className="text-muted-foreground">{getValue<number>()}</span> },
  { accessorKey: 'instrument', header: 'Instrument', size: 180, cell: ({ getValue }) => <span className="font-medium text-xs">{getValue<string>()}</span> },
  { accessorKey: 'side', header: 'Side', size: 60, cell: ({ row }) => <span className={cn('font-medium', row.original.sign > 0 ? 'text-emerald-500' : 'text-rose-500')}>{row.original.side}</span> },
  { accessorKey: 'entryTime', header: 'Entry Time', size: 140, cell: ({ getValue }) => <span className="text-muted-foreground whitespace-nowrap">{fmtTs(getValue<string>())}</span> },
  { accessorKey: 'entryPrice', header: 'Entry Price', meta: R, size: 100, cell: ({ getValue }) => <span className="tabular-nums">{getValue<number>().toFixed(2)}</span> },
  { accessorKey: 'exitTime', header: 'Exit Time', size: 140, cell: ({ getValue }) => <span className="text-muted-foreground whitespace-nowrap">{fmtTs(getValue<string>())}</span> },
  { accessorKey: 'exitPrice', header: 'Exit Price', meta: R, size: 100, cell: ({ getValue }) => <span className="tabular-nums">{getValue<number>().toFixed(2)}</span> },
  { accessorKey: 'qty', header: 'Qty', meta: R, size: 60, cell: ({ getValue }) => <span className="tabular-nums">{getValue<number>()}</span> },
  { accessorKey: 'pnl', header: 'P&L', meta: R, size: 120, cell: ({ row }) => <span className={cn('tabular-nums font-medium', row.original.pnl >= 0 ? 'text-emerald-500' : 'text-rose-500')}>{money(row.original.pnl)}</span>, sortingFn: 'basic' },
  { accessorKey: 'fees', header: 'Fees', meta: R, size: 100, cell: ({ getValue }) => <span className="tabular-nums">{money(getValue<number>())}</span> },
]

const COLUMN_LABELS: Record<string, string> = {
  idx: '#', instrument: 'Instrument', side: 'Side',
  entryTime: 'Entry Time', entryPrice: 'Entry Price',
  exitTime: 'Exit Time', exitPrice: 'Exit Price',
  qty: 'Qty', pnl: 'P&L', fees: 'Fees',
}

export function TradesTable({ trades }: { trades: Record<string, unknown>[] }) {
  const data = useMemo<ParsedTrade[]>(() => trades.map((t, i) => {
    const ef = (t.entry_fill ?? {}) as Record<string, unknown>
    const xf = (t.exit_fill ?? {}) as Record<string, unknown>
    const inst = (t.instrument ?? ef.instrument ?? {}) as Record<string, unknown>
    const entryPrice = Number(ef.price ?? t.entry_price ?? 0)
    const exitPrice = Number(xf.price ?? t.exit_price ?? 0)
    const qty = Number(ef.quantity ?? xf.quantity ?? t.quantity ?? t.qty ?? 0)
    const entryFees = Number(ef.fees ?? 0)
    const exitFees = Number(xf.fees ?? 0)
    const dir = String(t.direction ?? t.side ?? '-')
    const sign = dir === 'BUY' || dir === 'LONG' ? 1 : -1
    // Compose the real option contract name: the instrument stores the
    // underlying as `symbol` with strike/type/expiry separate, so a raw
    // `symbol` reads "NIFTY". Build "NIFTY 24600 CE (11 Aug)" for options.
    const itype = String(inst.instrument_type ?? '')
    const baseSym = String(inst.symbol ?? t.instrument_id ?? '-')
    let instrumentLabel = baseSym
    if ((itype === 'CE' || itype === 'PE') && inst.strike != null) {
      const strike = Number(inst.strike)
      const strikeStr = Number.isInteger(strike) ? String(strike) : String(strike)
      let exp = ''
      if (inst.expiry) {
        const d = new Date(String(inst.expiry))
        if (!Number.isNaN(d.getTime())) {
          exp = ` ${d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' })}`
        }
      }
      instrumentLabel = `${baseSym} ${strikeStr} ${itype}${exp}`
    }
    return {
      idx: i + 1,
      instrument: instrumentLabel,
      side: dir,
      entryTime: String(ef.ts ?? ef.timestamp ?? t.entry_time ?? t.entry_ts ?? '-'),
      entryPrice,
      exitTime: String(xf.ts ?? xf.timestamp ?? t.exit_time ?? t.exit_ts ?? '-'),
      exitPrice,
      qty,
      pnl: Number(t.pnl ?? t.realized_pnl ?? ((exitPrice - entryPrice) * qty * sign - entryFees - exitFees)),
      fees: entryFees + exitFees,
      sign,
    }
  }), [trades])

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Trades ({data.length})</CardTitle>
      </CardHeader>
      <CardContent>
        <DataTable
          columns={tradeColumns}
          data={data}
          storageKey="backtest-trades-table"
          defaultPinned={['idx', 'instrument', 'side']}
          columnLabels={COLUMN_LABELS}
          filterPlaceholder="Filter trades..."
          showDownload
          downloadFilename="trades"
          csvColumns={[
            { key: 'idx', header: '#', accessor: (r) => String(r.idx) },
            { key: 'instrument', header: 'Instrument', accessor: (r) => r.instrument },
            { key: 'side', header: 'Side', accessor: (r) => r.side },
            { key: 'entryTime', header: 'Entry Time', accessor: (r) => r.entryTime },
            { key: 'entryPrice', header: 'Entry Price', accessor: (r) => r.entryPrice.toFixed(2) },
            { key: 'exitTime', header: 'Exit Time', accessor: (r) => r.exitTime },
            { key: 'exitPrice', header: 'Exit Price', accessor: (r) => r.exitPrice.toFixed(2) },
            { key: 'qty', header: 'Qty', accessor: (r) => String(r.qty) },
            { key: 'pnl', header: 'P&L', accessor: (r) => r.pnl.toFixed(2) },
            { key: 'fees', header: 'Fees', accessor: (r) => r.fees.toFixed(2) },
          ]}
        />
      </CardContent>
    </Card>
  )
}
