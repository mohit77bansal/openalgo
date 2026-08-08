import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router'
import OrderBook from '@/pages/OrderBook'
import TradeBook from '@/pages/TradeBook'
import Positions from '@/pages/Positions'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'

const VALID_TABS = ['orderbook', 'tradebook', 'positions'] as const
type TabValue = (typeof VALID_TABS)[number]

const DEFAULT_TAB: TabValue = 'orderbook'

function isValidTab(value: string | null): value is TabValue {
  return value !== null && VALID_TABS.includes(value as TabValue)
}

export default function Orders() {
  const [searchParams, setSearchParams] = useSearchParams()
  const rawTab = searchParams.get('tab')
  const activeTab: TabValue = isValidTab(rawTab) ? rawTab : DEFAULT_TAB

  const handleTabChange = useCallback(
    (value: string) => {
      setSearchParams({ tab: value }, { replace: true })
    },
    [setSearchParams]
  )

  const tabItems = useMemo(
    () => [
      { value: 'orderbook' as const, label: 'Order Book' },
      { value: 'tradebook' as const, label: 'Trade Book' },
      { value: 'positions' as const, label: 'Positions' },
    ],
    []
  )

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Orders</h1>
        <p className="text-muted-foreground">
          View and manage your orders, trades, and positions
        </p>
      </div>

      <Tabs value={activeTab} onValueChange={handleTabChange}>
        <TabsList className="h-10">
          {tabItems.map((tab) => (
            <TabsTrigger key={tab.value} value={tab.value} className="min-w-[110px] text-sm">
              {tab.label}
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="orderbook" className="mt-6">
          <OrderBook />
        </TabsContent>

        <TabsContent value="tradebook" className="mt-6">
          <TradeBook />
        </TabsContent>

        <TabsContent value="positions" className="mt-6">
          <Positions />
        </TabsContent>
      </Tabs>
    </div>
  )
}
