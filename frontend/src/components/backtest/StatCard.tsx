import { cn } from '@/lib/utils'

export function StatCard({
  label,
  value,
  sub,
  tone,
}: {
  label: string
  value: string
  sub?: string
  tone?: 'good' | 'bad'
}) {
  return (
    <div className={cn(
      'border-l-2 pl-3 py-1.5',
      tone === 'good' && 'border-l-emerald-500/60',
      tone === 'bad' && 'border-l-rose-500/60',
      !tone && 'border-l-border',
    )}>
      <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground leading-tight">{label}</div>
      <div
        className={cn(
          'text-base font-bold tabular-nums tracking-tight leading-tight mt-0.5',
          tone === 'good' && 'text-emerald-500',
          tone === 'bad' && 'text-rose-500'
        )}
      >
        {value}
      </div>
      {sub && <div className="text-[10px] text-muted-foreground leading-tight mt-0.5">{sub}</div>}
    </div>
  )
}
