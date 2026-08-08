import { Navigate, Outlet } from 'react-router'
import { SocketProvider } from '@/components/socket/SocketProvider'
import { useAuthStore } from '@/stores/authStore'
import { MobileBottomNav } from './MobileBottomNav'
import { Sidebar } from './Sidebar'

export function Layout() {
  const { isAuthenticated } = useAuthStore()

  // AuthSync has already synced Flask session with Zustand store
  // So we just need to check the Zustand store state
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />
  }

  // Broker connection is optional — users can access backtest, strategies,
  // and other non-trading pages without a live broker session.

  return (
    <SocketProvider>
      <div className="flex h-screen bg-background">
        <Sidebar />
        <main className="flex-1 overflow-auto p-6 pb-24 md:pb-6">
          <Outlet />
        </main>
        <MobileBottomNav />
      </div>
    </SocketProvider>
  )
}

export function PublicLayout() {
  return (
    <div className="min-h-screen bg-background">
      <Outlet />
    </div>
  )
}
