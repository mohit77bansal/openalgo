import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface SidebarStore {
  collapsed: boolean
  toggle: () => void
  setCollapsed: (collapsed: boolean) => void
}

export const useSidebarStore = create<SidebarStore>()(
  persist(
    (set, get) => ({
      collapsed: false,

      toggle: () => set({ collapsed: !get().collapsed }),

      setCollapsed: (collapsed) => set({ collapsed }),
    }),
    {
      name: 'openalgo-sidebar',
    }
  )
)
