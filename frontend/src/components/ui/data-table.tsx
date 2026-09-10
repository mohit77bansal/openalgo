/**
 * DataTable — reusable table component with all standard features.
 *
 * Features: sorting, global filter, column pinning (user-controllable),
 * column visibility toggle, column reorder, CSV download, localStorage
 * persistence. Built on TanStack Table v8.
 *
 * Usage:
 *   <DataTable columns={columns} data={data} storageKey="my-table" />
 */

import {
  type ColumnDef,
  type ColumnOrderState,
  type SortingState,
  type VisibilityState,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  useReactTable,
} from '@tanstack/react-table'
import { ChevronDown, ChevronUp, Download, RotateCcw, Search, Settings2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

/* ---------------------------------------------------------------------------
 * Persistence helpers
 * ------------------------------------------------------------------------ */

interface PersistedSettings {
  order: string[]
  visibility: VisibilityState
  pinned: string[]
  sorting: SortingState
}

/** Merge a saved column order with the current column set: drop columns that no
 *  longer exist, and splice in newly-added columns at their default position.
 *  Without this, a column added to a table AFTER a user saved their layout is
 *  silently absent from the saved order array and never renders. */
function reconcileOrder(savedOrder: string[], defaultOrder: string[]): string[] {
  const order = savedOrder.filter((id) => defaultOrder.includes(id))
  defaultOrder.forEach((id, idx) => {
    if (!order.includes(id)) order.splice(Math.min(idx, order.length), 0, id)
  })
  return order
}

/** Newly-added columns adopt their default pin state (the user never made a
 *  choice about them). New default-pinned columns go left of existing pins. */
function reconcilePinned(
  savedPinned: string[], newColumns: string[], defaultOrder: string[], defaultPinned: string[],
): string[] {
  const freshPins = defaultPinned.filter((id) => newColumns.includes(id))
  const keptPins = savedPinned.filter((id) => defaultOrder.includes(id) && !freshPins.includes(id))
  return [...freshPins, ...keptPins]
}

function loadSettings(key: string, defaultOrder: string[], defaultPinned: string[]): PersistedSettings {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return { order: defaultOrder, visibility: {}, pinned: defaultPinned, sorting: [] }
    const p = JSON.parse(raw) as Partial<PersistedSettings>
    const savedOrder = Array.isArray(p.order) ? p.order : defaultOrder
    const savedPinned = Array.isArray(p.pinned) ? p.pinned : defaultPinned
    const newColumns = defaultOrder.filter((id) => !savedOrder.includes(id))
    return {
      order: reconcileOrder(savedOrder, defaultOrder),
      visibility: p.visibility && typeof p.visibility === 'object' ? p.visibility : {},
      pinned: reconcilePinned(savedPinned, newColumns, defaultOrder, defaultPinned),
      sorting: Array.isArray(p.sorting) ? p.sorting : [],
    }
  } catch {
    return { order: defaultOrder, visibility: {}, pinned: defaultPinned, sorting: [] }
  }
}

function saveSettings(key: string, order: string[], visibility: VisibilityState, pinned: string[], sorting: SortingState): void {
  try { localStorage.setItem(key, JSON.stringify({ order, visibility, pinned, sorting })) } catch {}
}

/* ---------------------------------------------------------------------------
 * Column config dropdown
 * ------------------------------------------------------------------------ */

function ColumnConfig({
  columnOrder, columnVisibility, pinnedColumns, columnLabels,
  onOrderChange, onVisibilityChange, onPinnedChange, onReset,
}: {
  columnOrder: string[]; columnVisibility: VisibilityState; pinnedColumns: string[]
  columnLabels: Record<string, string>
  onOrderChange: (o: string[]) => void; onVisibilityChange: (v: VisibilityState) => void
  onPinnedChange: (p: string[]) => void; onReset: () => void
}) {
  const moveColumn = (colId: string, dir: -1 | 1) => {
    const idx = columnOrder.indexOf(colId)
    if (idx < 0) return
    const target = idx + dir
    if (target < 0 || target >= columnOrder.length) return
    if (pinnedColumns.includes(colId) || pinnedColumns.includes(columnOrder[target])) return
    const next = [...columnOrder]; const [r] = next.splice(idx, 1); next.splice(target, 0, r)
    onOrderChange(next)
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="h-8 text-xs gap-1.5">
          <Settings2 className="h-3.5 w-3.5" /> Columns
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56 max-h-80 overflow-y-auto">
        {pinnedColumns.length > 0 && (
          <>
            <DropdownMenuLabel className="text-[10px] uppercase tracking-wider text-muted-foreground">Pinned (click to unpin)</DropdownMenuLabel>
            {pinnedColumns.map((id) => (
              <DropdownMenuCheckboxItem key={id} checked className="text-xs"
                onCheckedChange={() => onPinnedChange(pinnedColumns.filter(p => p !== id))}
                onSelect={(e) => e.preventDefault()}>
                {columnLabels[id] ?? id}
              </DropdownMenuCheckboxItem>
            ))}
            <DropdownMenuSeparator />
          </>
        )}
        <DropdownMenuLabel className="text-[10px] uppercase tracking-wider text-muted-foreground">Columns</DropdownMenuLabel>
        {columnOrder.filter(id => !pinnedColumns.includes(id)).map((id, idx, arr) => {
          const visible = columnVisibility[id] ?? true
          return (
            <div key={id} className="flex items-center">
              <DropdownMenuCheckboxItem checked={visible}
                onCheckedChange={() => onVisibilityChange({ ...columnVisibility, [id]: !visible })}
                onSelect={(e) => e.preventDefault()} className="flex-1 text-xs">
                {columnLabels[id] ?? id}
              </DropdownMenuCheckboxItem>
              <button type="button" className="p-0.5 mr-1 text-muted-foreground/40 hover:text-blue-500" title="Pin"
                onClick={(e) => { e.stopPropagation(); onPinnedChange([...pinnedColumns, id]) }}>📌</button>
              <div className="flex flex-col mr-2">
                <button type="button" className="p-0.5 text-muted-foreground hover:text-foreground disabled:opacity-25" disabled={idx === 0}
                  onClick={(e) => { e.stopPropagation(); moveColumn(id, -1) }}><ChevronUp className="h-3 w-3" /></button>
                <button type="button" className="p-0.5 text-muted-foreground hover:text-foreground disabled:opacity-25" disabled={idx === arr.length - 1}
                  onClick={(e) => { e.stopPropagation(); moveColumn(id, 1) }}><ChevronDown className="h-3 w-3" /></button>
              </div>
            </div>
          )
        })}
        <DropdownMenuSeparator />
        <div className="p-1">
          <Button variant="ghost" size="sm" className="w-full text-xs h-7" onClick={onReset}>
            <RotateCcw className="mr-2 h-3.5 w-3.5" /> Reset to Defaults
          </Button>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/* ---------------------------------------------------------------------------
 * Pinning style helper
 * ------------------------------------------------------------------------ */

function getPinStyle(column: { getIsPinned: () => string | false; getStart: (pos: string) => number; getIsLastColumn: (pos: string) => boolean; getSize: () => number }): React.CSSProperties {
  const pinned = column.getIsPinned()
  if (!pinned) return {}
  return {
    left: `${column.getStart('left')}px`,
    position: 'sticky',
    zIndex: 2,
    width: column.getSize(),
    boxShadow: column.getIsLastColumn('left') ? '4px 0 8px -4px rgba(0,0,0,0.08)' : undefined,
  }
}

/* ---------------------------------------------------------------------------
 * DataTable component
 * ------------------------------------------------------------------------ */

interface DataTableProps<T> {
  columns: ColumnDef<T, unknown>[]
  data: T[]
  storageKey?: string
  defaultPinned?: string[]
  columnLabels?: Record<string, string>
  filterPlaceholder?: string
  onRowClick?: (row: T) => void
  title?: string
  showFilter?: boolean
  showColumnConfig?: boolean
  showDownload?: boolean
  downloadFilename?: string
  csvColumns?: { key: string; header: string; accessor: (row: T) => string }[]
  /** Initial sort applied when the user has no persisted sort yet. */
  defaultSorting?: SortingState
}

export function DataTable<T>({
  columns, data, storageKey, defaultPinned = [], columnLabels = {},
  filterPlaceholder = 'Filter...', onRowClick, title,
  showFilter = true, showColumnConfig = true, showDownload = true,
  downloadFilename = 'export', csvColumns, defaultSorting = [],
}: DataTableProps<T>) {
  const defaultOrder = useMemo(() => columns.map((c) => ('accessorKey' in c ? String(c.accessorKey) : c.id ?? '')).filter(Boolean), [columns])

  const [persisted] = useState(() => storageKey ? loadSettings(storageKey, defaultOrder, defaultPinned) : { order: defaultOrder, visibility: {} as VisibilityState, pinned: defaultPinned, sorting: [] as SortingState })
  const [sorting, setSorting] = useState<SortingState>(persisted.sorting.length ? persisted.sorting : defaultSorting)
  const [globalFilter, setGlobalFilter] = useState('')
  const [columnOrder, setColumnOrder] = useState<ColumnOrderState>(persisted.order)
  const [columnVisibility, setColumnVisibility] = useState<VisibilityState>(persisted.visibility)
  const [pinnedColumns, setPinnedColumns] = useState<string[]>(persisted.pinned)

  useEffect(() => {
    if (storageKey) saveSettings(storageKey, columnOrder, columnVisibility, pinnedColumns, sorting)
  }, [storageKey, columnOrder, columnVisibility, pinnedColumns, sorting])

  const table = useReactTable({
    data, columns,
    state: { sorting, globalFilter, columnOrder, columnVisibility, columnPinning: { left: pinnedColumns } },
    onSortingChange: setSorting,
    onGlobalFilterChange: setGlobalFilter,
    onColumnOrderChange: setColumnOrder,
    onColumnVisibilityChange: setColumnVisibility,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  })

  const handleDownload = useCallback(() => {
    if (!csvColumns) return
    const headers = csvColumns.map(c => c.header)
    const rows = table.getRowModel().rows.map(r => csvColumns.map(c => {
      const v = c.accessor(r.original)
      return v.includes(',') || v.includes('"') ? `"${v.replace(/"/g, '""')}"` : v
    }).join(','))
    const csv = [headers.join(','), ...rows].join('\n')
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a'); a.href = url; a.download = `${downloadFilename}_${new Date().toISOString().slice(0, 10)}.csv`; a.click()
    URL.revokeObjectURL(url)
  }, [table, csvColumns, downloadFilename])

  const handleReset = () => {
    setColumnOrder(defaultOrder); setColumnVisibility({}); setPinnedColumns(defaultPinned); setSorting([])
  }

  return (
    <div>
      {(title || showFilter || showColumnConfig || showDownload) && (
        <div className="flex items-center justify-between gap-3 mb-3">
          {title && <div className="text-base font-semibold">{title}</div>}
          <div className="flex items-center gap-2 ml-auto">
            {showFilter && (
              <div className="relative">
                <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
                <Input placeholder={filterPlaceholder} value={globalFilter} onChange={(e) => setGlobalFilter(e.target.value)}
                  className="h-8 w-48 text-xs pl-7" />
              </div>
            )}
            {showColumnConfig && (
              <ColumnConfig columnOrder={columnOrder} columnVisibility={columnVisibility} pinnedColumns={pinnedColumns}
                columnLabels={columnLabels} onOrderChange={setColumnOrder} onVisibilityChange={setColumnVisibility}
                onPinnedChange={setPinnedColumns} onReset={handleReset} />
            )}
            {showDownload && csvColumns && (
              <Button variant="outline" size="sm" className="h-8 text-xs" onClick={handleDownload}>
                <Download className="h-3.5 w-3.5 mr-1.5" /> CSV
              </Button>
            )}
          </div>
        </div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            {table.getHeaderGroups().map(hg => (
              <tr key={hg.id} className="border-b text-xs text-muted-foreground">
                {hg.headers.map(h => {
                  const pinned = h.column.getIsPinned()
                  const align = (h.column.columnDef.meta as Record<string, string> | undefined)?.align
                  return (
                    <th key={h.id} className={cn(
                      'pb-2 pr-3 cursor-pointer select-none whitespace-nowrap',
                      align === 'right' ? 'text-right' : 'text-left',
                      pinned && 'sticky bg-card z-20',
                    )}
                      style={pinned ? getPinStyle(h.column as never) : undefined}
                      onClick={h.column.getToggleSortingHandler()}>
                      {flexRender(h.column.columnDef.header, h.getContext())}
                      {h.column.getIsSorted() === 'asc' ? ' ▲' : h.column.getIsSorted() === 'desc' ? ' ▼' : null}
                    </th>
                  )
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map(row => (
              <tr key={row.id}
                className={cn('border-b border-border/50 last:border-0 even:bg-muted/20 hover:bg-accent/40 transition-colors', onRowClick && 'cursor-pointer')}
                onClick={onRowClick ? () => onRowClick(row.original) : undefined}>
                {row.getVisibleCells().map(cell => {
                  const pinned = cell.column.getIsPinned()
                  const align = (cell.column.columnDef.meta as Record<string, string> | undefined)?.align
                  return (
                    <td key={cell.id} className={cn(
                      'py-2 pr-3',
                      align === 'right' && 'text-right',
                      pinned && 'sticky bg-card z-10',
                    )}
                      style={pinned ? getPinStyle(cell.column as never) : undefined}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
