import { BookOpen, LogOut, Menu, Moon, PanelLeftClose, PanelLeftOpen, Sun } from 'lucide-react'
import { useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router'
import { authApi } from '@/api/auth'
import { LogoutConfirmDialog } from '@/components/auth/LogoutConfirmDialog'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Separator } from '@/components/ui/separator'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { isActiveRoute, type NavGroup, type NavItem, sidebarGroups } from '@/config/navigation'
import { useProfileMenuItems } from '@/hooks/useProfileMenuItems'
import { cn } from '@/lib/utils'
import { useAuthStore } from '@/stores/authStore'
import { useSidebarStore } from '@/stores/sidebarStore'
import { useThemeStore } from '@/stores/themeStore'
import { showToast } from '@/utils/toast'

// ── Sidebar nav link (handles external vs. React routes) ──────────────

function SidebarNavLink({
  item,
  collapsed,
  isActive,
  onClick,
}: {
  item: NavItem
  collapsed: boolean
  isActive: boolean
  onClick?: () => void
}) {
  const cls = cn(
    'flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors',
    isActive
      ? 'bg-accent text-accent-foreground'
      : 'text-muted-foreground hover:bg-muted hover:text-foreground',
    collapsed && 'justify-center px-0'
  )

  const inner = (
    <>
      <item.icon className="h-4 w-4 shrink-0" />
      {!collapsed && <span className="truncate">{item.label}</span>}
    </>
  )

  const link = item.external ? (
    <a key={item.href} href={item.href} onClick={onClick} className={cls} title={item.label}>
      {inner}
    </a>
  ) : (
    <Link key={item.href} to={item.href} onClick={onClick} className={cls} title={item.label}>
      {inner}
    </Link>
  )

  if (collapsed) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>{link}</TooltipTrigger>
        <TooltipContent side="right">{item.label}</TooltipContent>
      </Tooltip>
    )
  }

  return link
}

// ── Nav group renderer ────────────────────────────────────────────────

function SidebarGroup({
  group,
  collapsed,
  pathname,
  onClick,
}: {
  group: NavGroup
  collapsed: boolean
  pathname: string
  onClick?: () => void
}) {
  return (
    <div className="flex flex-col gap-1">
      {collapsed ? (
        <Separator className="my-1" />
      ) : (
        <span className="px-3 py-1.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          {group.label}
        </span>
      )}
      {group.items.map((item) => (
        <SidebarNavLink
          key={item.href}
          item={item}
          collapsed={collapsed}
          isActive={isActiveRoute(pathname, item.href)}
          onClick={onClick}
        />
      ))}
    </div>
  )
}

// ── Mode toggle (Live / Analyze) ──────────────────────────────────────

function ModeToggle({ collapsed }: { collapsed: boolean }) {
  const { appMode, isTogglingMode, toggleAppMode } = useThemeStore()

  const handleToggle = async () => {
    const result = await toggleAppMode()
    if (result.success) {
      const newMode = useThemeStore.getState().appMode
      showToast.success(`Switched to ${newMode === 'live' ? 'Live' : 'Analyze'} mode`)
      if (newMode === 'analyzer') {
        setTimeout(() => {
          showToast.warning('Analyzer (Sandbox) mode is for testing purposes only', undefined, {
            duration: 10000,
          })
        }, 2000)
      }
    } else {
      showToast.error(result.message || 'Failed to toggle mode')
    }
  }

  const isLive = appMode === 'live'

  if (collapsed) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            onClick={handleToggle}
            disabled={isTogglingMode}
            className={cn(
              'flex items-center justify-center rounded-md p-2 text-xs font-medium transition-colors',
              isLive
                ? 'bg-green-500/10 text-green-600 dark:text-green-400'
                : 'bg-orange-500/10 text-orange-600 dark:text-orange-400'
            )}
            aria-label={`Switch to ${isLive ? 'Analyze' : 'Live'} mode`}
          >
            {isTogglingMode ? (
              <div className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
            ) : (
              <span className="relative flex h-3 w-3">
                <span
                  className={cn(
                    'absolute inline-flex h-full w-full rounded-full opacity-75',
                    isLive ? 'bg-green-500 animate-ping' : 'bg-orange-500'
                  )}
                />
                <span
                  className={cn(
                    'relative inline-flex h-3 w-3 rounded-full',
                    isLive ? 'bg-green-500' : 'bg-orange-500'
                  )}
                />
              </span>
            )}
          </button>
        </TooltipTrigger>
        <TooltipContent side="right">{isLive ? 'Live Mode' : 'Analyze Mode'}</TooltipContent>
      </Tooltip>
    )
  }

  return (
    <button
      type="button"
      onClick={handleToggle}
      disabled={isTogglingMode}
      className={cn(
        'flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition-colors w-full',
        isLive
          ? 'bg-green-500/10 text-green-600 dark:text-green-400 hover:bg-green-500/20'
          : 'bg-orange-500/10 text-orange-600 dark:text-orange-400 hover:bg-orange-500/20'
      )}
      aria-label={`Switch to ${isLive ? 'Analyze' : 'Live'} mode`}
    >
      {isTogglingMode ? (
        <div className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
      ) : (
        <span className="relative flex h-3 w-3 shrink-0">
          <span
            className={cn(
              'absolute inline-flex h-full w-full rounded-full opacity-75',
              isLive ? 'bg-green-500 animate-ping' : 'bg-orange-500'
            )}
          />
          <span
            className={cn(
              'relative inline-flex h-3 w-3 rounded-full',
              isLive ? 'bg-green-500' : 'bg-orange-500'
            )}
          />
        </span>
      )}
      <span className="truncate">{isLive ? 'Live Mode' : 'Analyze Mode'}</span>
    </button>
  )
}

// ── Shared sidebar content (used by both desktop + mobile sheet) ──────

function SidebarContent({
  collapsed,
  onNavigate,
}: {
  collapsed: boolean
  onNavigate?: () => void
}) {
  const location = useLocation()
  const navigate = useNavigate()
  const { mode, appMode, toggleMode } = useThemeStore()
  const { user } = useAuthStore()
  const { toggle } = useSidebarStore()
  const [showLogoutDialog, setShowLogoutDialog] = useState(false)

  // Profile menu filtered by broker capabilities
  const _filteredProfileMenuItems = useProfileMenuItems()

  const handleLogout = async () => {
    try {
      await authApi.logout()
      useAuthStore.getState().logout()
      navigate('/login')
      showToast.success('Logged out successfully')
    } catch {
      useAuthStore.getState().logout()
      navigate('/login')
    }
  }

  // Filter sidebar groups based on broker capabilities
  const filteredGroups = sidebarGroups.map((group) => {
    if (group.label !== 'Settings') return group
    return {
      ...group,
      items: group.items.filter((item) => {
        // Use the hook-filtered items to decide visibility
        return _filteredProfileMenuItems.some((pi) => pi.href === item.href)
      }),
    }
  })

  return (
    <div className="flex h-full flex-col">
      {/* ── Logo ─────────────────────────────────────────────── */}
      <div className={cn('flex items-center gap-2 px-3 py-4', collapsed && 'justify-center px-0')}>
        <Link to="/dashboard" className="flex items-center gap-2" onClick={onNavigate}>
          <img src="/logo.png" alt="OpenAlgo" className="h-8 w-8" />
          {!collapsed && <span className="font-semibold text-foreground">OpenAlgo</span>}
        </Link>
      </div>

      {/* ── Mode toggle ──────────────────────────────────────── */}
      <div className={cn('px-2 pb-2', collapsed && 'px-1')}>
        <ModeToggle collapsed={collapsed} />
      </div>

      <Separator />

      {/* ── Navigation groups (scrollable) ───────────────────── */}
      <nav className={cn('flex-1 overflow-y-auto px-2 py-2 space-y-3', collapsed && 'px-1')}>
        {filteredGroups.map((group) => (
          <SidebarGroup
            key={group.label}
            group={group}
            collapsed={collapsed}
            pathname={location.pathname}
            onClick={onNavigate}
          />
        ))}

        {/* Docs link */}
        <div className="flex flex-col gap-1">
          {collapsed ? (
            <Separator className="my-1" />
          ) : (
            <span className="px-3 py-1.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
              Help
            </span>
          )}
          {collapsed ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <a
                  href="https://docs.openalgo.in"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center justify-center rounded-md px-0 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                  title="Docs"
                >
                  <BookOpen className="h-4 w-4" />
                </a>
              </TooltipTrigger>
              <TooltipContent side="right">Docs</TooltipContent>
            </Tooltip>
          ) : (
            <a
              href="https://docs.openalgo.in"
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              <BookOpen className="h-4 w-4 shrink-0" />
              <span>Docs</span>
            </a>
          )}
        </div>
      </nav>

      <Separator />

      {/* ── Bottom controls ──────────────────────────────────── */}
      <div className={cn('flex flex-col gap-1 px-2 py-2', collapsed && 'px-1 items-center')}>
        {/* Theme toggle */}
        {collapsed ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8"
                onClick={toggleMode}
                disabled={appMode !== 'live'}
                aria-label={mode === 'light' ? 'Switch to dark mode' : 'Switch to light mode'}
              >
                {mode === 'light' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
              </Button>
            </TooltipTrigger>
            <TooltipContent side="right">
              {mode === 'light' ? 'Dark mode' : 'Light mode'}
            </TooltipContent>
          </Tooltip>
        ) : (
          <Button
            variant="ghost"
            className="w-full justify-start gap-3 px-3 text-sm font-medium text-muted-foreground"
            onClick={toggleMode}
            disabled={appMode !== 'live'}
            aria-label={mode === 'light' ? 'Switch to dark mode' : 'Switch to light mode'}
          >
            {mode === 'light' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            {mode === 'light' ? 'Dark mode' : 'Light mode'}
          </Button>
        )}

        {/* Collapse toggle (desktop only — hidden on mobile overlay) */}
        <div className="hidden md:block">
          {collapsed ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8"
                  onClick={toggle}
                  aria-label="Expand sidebar"
                >
                  <PanelLeftOpen className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="right">Expand sidebar</TooltipContent>
            </Tooltip>
          ) : (
            <Button
              variant="ghost"
              className="w-full justify-start gap-3 px-3 text-sm font-medium text-muted-foreground"
              onClick={toggle}
              aria-label="Collapse sidebar"
            >
              <PanelLeftClose className="h-4 w-4" />
              Collapse
            </Button>
          )}
        </div>

        <Separator />

        {/* User section */}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className={cn(
                'flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors hover:bg-muted w-full',
                collapsed && 'justify-center px-0'
              )}
              aria-label="User menu"
            >
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground text-xs font-medium">
                {user?.username?.[0]?.toUpperCase() || 'O'}
              </span>
              {!collapsed && (
                <span className="truncate text-foreground font-medium">
                  {user?.username || 'User'}
                </span>
              )}
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent side={collapsed ? 'right' : 'top'} align="start" className="w-48">
            {user?.broker && (
              <DropdownMenuItem disabled className="text-xs text-muted-foreground">
                Broker: {user.broker}
              </DropdownMenuItem>
            )}
            <DropdownMenuItem
              onClick={() => setShowLogoutDialog(true)}
              className="text-destructive focus:text-destructive cursor-pointer"
            >
              <LogOut className="h-4 w-4 mr-2" />
              Logout
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <LogoutConfirmDialog
        open={showLogoutDialog}
        onOpenChange={setShowLogoutDialog}
        onConfirm={handleLogout}
      />
    </div>
  )
}

// ── Desktop sidebar ───────────────────────────────────────────────────

function DesktopSidebar() {
  const { collapsed } = useSidebarStore()

  return (
    <aside
      className={cn(
        'hidden md:flex flex-col border-r bg-background transition-[width] duration-200 ease-in-out shrink-0',
        collapsed ? 'w-14' : 'w-60'
      )}
    >
      <SidebarContent collapsed={collapsed} />
    </aside>
  )
}

// ── Mobile sidebar (hamburger → sheet overlay) ────────────────────────

function MobileSidebarTrigger() {
  const [open, setOpen] = useState(false)

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="md:hidden fixed top-3 left-3 z-50 h-10 w-10 bg-background/80 backdrop-blur shadow-sm border"
          aria-label="Open navigation"
        >
          <Menu className="h-5 w-5" />
        </Button>
      </SheetTrigger>
      <SheetContent side="left" className="w-60 p-0">
        <SheetHeader className="sr-only">
          <SheetTitle>Navigation</SheetTitle>
          <SheetDescription>App navigation sidebar</SheetDescription>
        </SheetHeader>
        <SidebarContent collapsed={false} onNavigate={() => setOpen(false)} />
      </SheetContent>
    </Sheet>
  )
}

// ── Public export ─────────────────────────────────────────────────────

export function Sidebar() {
  return (
    <>
      <DesktopSidebar />
      <MobileSidebarTrigger />
    </>
  )
}
