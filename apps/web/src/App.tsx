import { useEffect, useState, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  LayoutDashboard,
  Play,
  Clock3,
  Puzzle,
  Brain,
  Radio,
  Send,
  Settings,
  FileText,
  ScrollText,
  Menu,
  LogOut,
  ChevronLeft,
  ChevronRight,
  Bird,
  PanelLeft,
  Sparkles,
  ShieldCheck,
} from 'lucide-react'
import { api, openSSE, getToken, setToken, setOnUnauthorized } from './api'
import type { SSEState } from './api'
import {
  Dashboard,
  RunsPage,
  RunDetailPage,
  ApprovalsPage,
  ContextPage,
  MemoryPage,
  SourcesPage,
  AgentsPage,
  TelegramPage,
  SettingsPage,
  AuditPage,
  ReportsPage,
  CommandCenter,
  LoginPage,
} from './pages'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { Sheet, SheetContent, SheetTrigger } from '@/components/ui/sheet'
import { cn } from '@/lib/utils'

type PageKey = 'dashboard' | 'runs' | 'run-detail' | 'approvals' | 'context' | 'memory' | 'sources' | 'agents' | 'telegram' | 'settings' | 'audit' | 'reports'
type NavKey = Exclude<PageKey, 'run-detail'>

const NAV: [NavKey, string, React.ElementType][] = [
  ['dashboard', 'Dashboard', LayoutDashboard],
  ['runs', 'Runs', Play],
  ['approvals', 'Approvals', Clock3],
  ['context', 'Context', Puzzle],
  ['memory', 'Memory', Brain],
  ['sources', 'Sources', Radio],
  ['agents', 'Trust Center', ShieldCheck],

  ['telegram', 'Telegram', Send],
  ['settings', 'Settings', Settings],
  ['reports', 'Reports', FileText],
  ['audit', 'Audit', ScrollText],
]

function SSEDot({ state }: { state: SSEState }) {
  const color =
    state === 'open' ? 'bg-emerald-500' : state === 'connecting' ? 'bg-amber-500' : state === 'error' ? 'bg-red-500' : 'bg-zinc-400'
  const glow = state === 'open' ? 'shadow-[0_0_10px_theme(colors.emerald.500),0_0_20px_theme(colors.emerald.500/0.4)]' : ''
  return (
    <span className="relative flex h-2.5 w-2.5 shrink-0">
      {state === 'open' && (
        <>
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />
          <span className="absolute inline-flex h-full w-full animate-pulse rounded-full bg-emerald-400 opacity-20 blur-[2px]" />
        </>
      )}
      <span className={cn('relative inline-flex h-2.5 w-2.5 rounded-full ring-2 ring-white/20 dark:ring-white/10', color, glow)} />
    </span>
  )
}

function SSEBadge({ state, lastId }: { state: SSEState; lastId: string }) {
  const label = state === 'open' ? 'live' : state === 'connecting' ? 'connecting' : state === 'error' ? 'reconnecting' : 'offline'
  const variant: 'default' | 'secondary' | 'destructive' | 'outline' =
    state === 'open' ? 'default' : state === 'error' ? 'destructive' : 'secondary'
  return (
    <div
      className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/70 px-3 py-1.5 text-xs font-medium shadow-sm backdrop-blur-xl dark:bg-white/[0.06] dark:border-white/[0.06]"
      title={lastId ? `Last-Event-ID: ${lastId}` : undefined}
    >
      <SSEDot state={state} />
      <span className="capitalize tracking-tight font-semibold">{label}</span>
      {lastId && <span className="hidden text-[10px] font-mono text-muted-foreground sm:inline">#{lastId.slice(0, 8)}</span>}
      <Badge variant={variant} className="ml-1 hidden h-5 px-1.5 text-[10px] font-bold uppercase tracking-widest sm:inline-flex">
        SSE
      </Badge>
    </div>
  )
}

function NavButton({
  active,
  collapsed,
  icon: Icon,
  label,
  onClick,
}: {
  active: boolean
  collapsed: boolean
  icon: React.ElementType
  label: string
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      title={collapsed ? label : undefined}
      className={cn(
        'group relative flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-all duration-200 ease-[cubic-bezier(0.32,0.72,0,1)]',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500/20',
        active
          ? 'bg-gradient-to-br from-violet-600 to-indigo-600 text-white shadow-[0_4px_14px_rgba(99,102,241,0.35),0_1px_3px_rgba(0,0,0,0.1)] hover:shadow-[0_6px_20px_rgba(99,102,241,0.4)] hover:from-violet-600 hover:to-indigo-700'
          : 'text-sidebar-foreground/75 hover:bg-white/60 hover:text-sidebar-foreground hover:shadow-sm hover:backdrop-blur-sm dark:hover:bg-white/[0.06] dark:hover:text-sidebar-foreground',
        collapsed && 'justify-center px-2.5',
        'active:scale-[0.98]'
      )}
    >
      {/* active indicator glow */}
      {active && <span className="absolute inset-0 rounded-xl bg-gradient-to-br from-white/12 to-transparent pointer-events-none" />}
      <Icon className={cn('h-[18px] w-[18px] shrink-0 transition-transform duration-200 group-hover:scale-105', active ? 'text-white' : 'text-muted-foreground group-hover:text-foreground')} />
      {!collapsed && <span className="truncate tracking-tight text-[13.5px]">{label}</span>}
      {!collapsed && active && <motion.span layoutId="nav-dot" className="ml-auto h-1.5 w-1.5 rounded-full bg-white/80 shadow-sm" />}
    </button>
  )
}
export default function App() {
  const [tab, setTab] = useState<NavKey>('dashboard')
  const [runId, setRunId] = useState<string>('')
  const [live, setLive] = useState<Record<string, unknown> | null>(null)
  const [sseState, setSseState] = useState<SSEState>('connecting')
  const [lastId, setLastId] = useState('')
  const [toast, setToast] = useState('')
  const [session, setSession] = useState<{ email: string; role: string } | null>(null)
  const [authLoading, setAuthLoading] = useState(true)
  const [mobileOpen, setMobileOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(false)
  const liveRef = useRef(live)

  useEffect(() => {
    liveRef.current = live
  }, [live])

  useEffect(() => {
    setOnUnauthorized(() => {
      setToken('')
      setSession(null)
    })
    if (!getToken()) {
      setAuthLoading(false)
      return
    }
    api<{ username: string; role: string }>('/v1/auth/me')
      .then((u) => setSession({ email: u.username, role: u.role }))
      .catch(() => {
        setToken('')
        setSession(null)
      })
      .finally(() => setAuthLoading(false))
  }, [])

  useEffect(() => {
    if (!session) return
    const stop = openSSE(
      (e, id) => {
        setLive(e as Record<string, unknown>)
        if (id) setLastId(id)
      },
      (s) => setSseState(s)
    )
    return stop
  }, [session])

  function logout() {
    setToken('')
    setSession(null)
    setTab('dashboard')
  }

  // auto-dismiss toast
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(''), 3000)
    return () => clearTimeout(t)
  }, [toast])

  if (authLoading)
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="flex flex-col items-center gap-3">
          <img src="/logo.png?v=5" alt="LUMI" className="h-20 w-20 object-contain animate-pulse drop-shadow-[0_4px_14px_rgba(99,102,241,0.3)]" />
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-violet-500 shadow-[0_0_8px_theme(colors.violet.500)]" />
            loading…
          </div>
        </motion.div>
      </div>
    )

  if (!session) {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4, ease: [0.32,0.72,0,1] }} className="w-full max-w-[420px]">
          <div className="mb-6 flex flex-col items-center gap-3 text-center">
            <img src="/logo.png?v=5" alt="LUMI" className="h-28 w-28 object-contain drop-shadow-[0_8px_24px_rgba(99,102,241,0.25)]" />
            <div>
              <div className="bg-gradient-to-r from-violet-600 to-indigo-600 bg-clip-text text-base font-bold tracking-tight text-transparent">LUMI OBSERVATORY</div>
              <div className="text-xs font-medium tracking-widest text-muted-foreground uppercase">Observation & orchestration</div>
            </div>
          </div>
          <Card className="shadow-[0_16px_40px_rgba(0,0,0,0.1),0_4px_12px_rgba(0,0,0,0.06)] border-white/20 dark:border-white/5">
            <CardContent className="pt-6">
              <LoginPage onLogin={(u) => setSession(u)} />
            </CardContent>
          </Card>
        </motion.div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-background text-foreground antialiased">
      {/* Topbar — glass */}
      <header className="sticky top-0 z-40 flex h-[60px] items-center gap-3 border-b border-white/10 bg-white/65 px-3 backdrop-blur-2xl supports-[backdrop-filter]:bg-white/45 shadow-[0_1px_3px_rgba(0,0,0,0.04),0_4px_12px_rgba(0,0,0,0.03)] dark:bg-zinc-900/55 dark:border-white/[0.06] dark:shadow-[0_1px_3px_rgba(0,0,0,0.2)] lg:px-4">
        {/* subtle top highlight */}
        <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-violet-500/20 to-transparent dark:via-violet-400/15" />
        {/* mobile menu */}
        <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
          <SheetTrigger asChild>
            <Button variant="ghost" size="icon" className="shrink-0 rounded-xl md:hidden" aria-label="Open menu">
              <Menu className="h-5 w-5" />
            </Button>
          </SheetTrigger>
          <SheetContent side="left" className="w-[300px] p-0 border-white/10">
            <div className="flex h-full flex-col">
              <div className="flex h-[60px] items-center gap-3 border-b border-white/10 px-4 bg-white/30 dark:bg-white/[0.02]">
                <img src="/logo.png?v=5" alt="LUMI" className="h-10 w-10 object-contain" />
                <div>
                  <div className="bg-gradient-to-r from-violet-600 to-indigo-600 bg-clip-text text-sm font-bold leading-none tracking-tight text-transparent">LUMI</div>
                  <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">Observatory</div>
                </div>
              </div>
              <div className="p-3">
                <SSEBadge state={sseState} lastId={lastId} />
              </div>
              <nav className="flex-1 space-y-1 overflow-y-auto px-3 py-2">
                {NAV.map(([k, label, Icon]) => (
                  <NavButton
                    key={k}
                    active={tab === k}
                    collapsed={false}
                    icon={Icon}
                    label={label}
                    onClick={() => {
                      setTab(k)
                      setMobileOpen(false)
                    }}
                  />
                ))}
              </nav>
              <div className="border-t border-white/10 p-4 bg-white/20 dark:bg-white/[0.02]">
                <div className="mb-2 truncate text-xs font-semibold tracking-tight">{session.email}</div>
                <Badge variant="violet" className="mb-3 text-[10px] uppercase tracking-widest">
                  {session.role}
                </Badge>
                <Button variant="outline" size="sm" className="w-full justify-start gap-2 rounded-xl" onClick={logout}>
                  <LogOut className="h-4 w-4" /> Sign out
                </Button>
              </div>
            </div>
          </SheetContent>
        </Sheet>

        {/* brand — gradient text */}
        <div className="flex items-center gap-3">
          <img src="/logo.png?v=5" alt="LUMI" className="hidden h-9 w-9 object-contain md:block" />
          <div className="hidden md:block">
            <div className="bg-gradient-to-r from-violet-600 via-indigo-600 to-violet-700 bg-clip-text text-[15px] font-extrabold leading-none tracking-tight text-transparent">LUMI</div>
            <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground flex items-center gap-1">Observatory <Sparkles className="h-2.5 w-2.5 text-violet-500/70" /></div>
          </div>
          <div className="flex items-center gap-1.5 md:hidden">
            <img src="/logo.png?v=5" alt="LUMI" className="h-8 w-8 object-contain" />
            <span className="bg-gradient-to-r from-violet-600 to-indigo-600 bg-clip-text text-sm font-bold tracking-tight text-transparent">LUMI</span>
          </div>
        </div>

        <div className="hidden items-center gap-2 lg:flex">
          <span className="mx-2 h-5 w-px bg-border/60 dark:bg-white/10" />
          <SSEBadge state={sseState} lastId={lastId} />
        </div>

        <div className="flex flex-1 items-center justify-end gap-2">
          {/* desktop SSE compact */}
          <div className="hidden items-center gap-2 rounded-full border border-white/10 bg-white/60 px-3 py-1.5 backdrop-blur-xl dark:bg-white/[0.04] sm:flex lg:hidden">
            <SSEDot state={sseState} />
            <span className="text-xs font-semibold capitalize text-muted-foreground">{sseState === 'open' ? 'live' : sseState}</span>
          </div>

          <div className="hidden items-center gap-3 sm:flex">
            <div className="hidden flex-col items-end leading-none md:flex">
              <span className="max-w-[160px] truncate text-xs font-semibold tracking-tight">{session.email}</span>
              <span className="text-[10px] font-medium uppercase tracking-widest text-muted-foreground">{session.role}</span>
            </div>
            <Badge variant="violet" className="hidden text-[10px] uppercase tracking-widest shadow-sm md:inline-flex">
              {session.role}
            </Badge>
          </div>

          <Button variant="ghost" size="sm" className="hidden gap-1.5 rounded-xl md:inline-flex" onClick={logout}>
            <LogOut className="h-4 w-4" /> Sign out
          </Button>
          <Button variant="ghost" size="icon" className="rounded-xl md:hidden" onClick={logout} aria-label="Sign out">
            <LogOut className="h-4 w-4" />
          </Button>

          <Button
            variant="ghost"
            size="icon"
            className="hidden h-8 w-8 rounded-xl border border-black/[0.04] bg-white/40 backdrop-blur-sm hover:bg-white/70 dark:bg-white/[0.04] dark:border-white/5 dark:hover:bg-white/10 md:inline-flex"
            onClick={() => setCollapsed((v) => !v)}
            aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            {collapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronLeft className="h-4 w-4" />}
          </Button>
        </div>
      </header>

      <div className="flex">
        {/* Sidebar desktop — glass */}
        <aside
          className={cn(
            'sticky top-[60px] hidden h-[calc(100vh-60px)] shrink-0 flex-col border-r border-white/10 bg-white/65 backdrop-blur-2xl supports-[backdrop-filter]:bg-white/40 text-sidebar-foreground shadow-[4px_0_24px_rgba(0,0,0,0.03)] md:flex dark:bg-zinc-900/40 dark:border-white/[0.06] dark:shadow-[4px_0_24px_rgba(0,0,0,0.12)]',
            'transition-all duration-300 ease-[cubic-bezier(0.32,0.72,0,1)]',
            collapsed ? 'w-[72px]' : 'w-[252px]'
          )}
        >
          <div className={cn('flex h-11 items-center border-b border-black/[0.04] dark:border-white/[0.05] px-2 bg-white/20 dark:bg-white/[0.015]', collapsed ? 'justify-center' : 'justify-between gap-2 px-3')}>
            {!collapsed && (
              <span className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-[0.1em] text-muted-foreground">
                <PanelLeft className="h-3.5 w-3.5" /> Menu
              </span>
            )}
            {!collapsed && (
              <Badge variant="outline" className="h-5 rounded-full px-1.5 text-[10px] bg-white/70 dark:bg-white/[0.04]">
                {NAV.length}
              </Badge>
            )}
          </div>

          <nav className="flex-1 space-y-1.5 overflow-y-auto p-3">
            {NAV.map(([k, label, Icon]) => (
              <NavButton
                key={k}
                active={tab === k}
                collapsed={collapsed}
                icon={Icon}
                label={label}
                onClick={() => setTab(k)}
              />
            ))}
          </nav>

          <div className={cn('border-t border-black/[0.04] dark:border-white/[0.05] p-3 bg-gradient-to-br from-violet-50/50 to-indigo-50/30 dark:from-violet-950/10 dark:to-indigo-950/5', collapsed && 'p-2')}>
            {!collapsed ? (
              <div className="space-y-2.5">
                <div className="flex items-center gap-2 rounded-xl border border-white/50 bg-white/60 px-2.5 py-2 backdrop-blur-sm dark:bg-white/[0.04] dark:border-white/5">
                  <SSEDot state={sseState} />
                  <span className="text-xs font-semibold capitalize tracking-tight">SSE {sseState}</span>
                  <span className={cn('ml-auto h-1.5 w-1.5 rounded-full', sseState==='open'?'bg-emerald-500 shadow-[0_0_6px_theme(colors.emerald.500)]':'bg-zinc-300')} />
                </div>
                <div className="truncate text-[11px] font-medium tracking-tight text-muted-foreground px-1" title={session.email}>
                  {session.email}
                </div>
              </div>
            ) : (
              <div className="flex justify-center rounded-xl bg-white/50 p-2 dark:bg-white/[0.04]">
                <SSEDot state={sseState} />
              </div>
            )}
          </div>
        </aside>

        {/* Main — page transition wrapper */}
        <main className="min-w-0 flex-1">
          <div className="mx-auto max-w-[1240px] p-4 md:p-6 lg:p-8">
            {/* toast premium */}
            <AnimatePresence>
              {toast && (
                <motion.div
                  initial={{ opacity: 0, y: -12, scale: 0.96 }}
                  animate={{ opacity: 1, y: 0, scale: 1 }}
                  exit={{ opacity: 0, y: -8, scale: 0.98 }}
                  transition={{ duration: 0.22, ease: [0.32,0.72,0,1] }}
                  onClick={() => setToast('')}
                  className="fixed right-4 top-[4.5rem] z-50 cursor-pointer rounded-2xl border border-emerald-200/50 bg-white/85 px-4 py-3 text-sm font-medium text-emerald-800 shadow-[0_8px_32px_rgba(16,185,129,0.18),0_2px_8px_rgba(0,0,0,0.06)] backdrop-blur-xl dark:border-emerald-800/30 dark:bg-emerald-950/70 dark:text-emerald-100"
                >
                  <span className="flex items-center gap-2"><span className="h-1.5 w-1.5 rounded-full bg-emerald-500 shadow-[0_0_8px_theme(colors.emerald.500)] animate-pulse" />{toast}</span>
                </motion.div>
              )}
            </AnimatePresence>

            {/* page content with fade+slide */}
            <AnimatePresence mode="wait">
              <motion.div
                key={tab + runId}
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -6 }}
                transition={{ duration: 0.28, ease: [0.32,0.72,0,1] }}
                className="space-y-6"
              >
              {tab === 'dashboard' && (
                <Card className="overflow-hidden border-violet-200/40 bg-gradient-to-br from-violet-50/80 via-white to-indigo-50/40 dark:border-violet-900/20 dark:from-violet-950/20 dark:via-zinc-900/40 dark:to-indigo-950/10 shadow-[0_8px_32px_rgba(99,102,241,0.08)]">
                  <div className="absolute inset-0 bg-gradient-to-br from-violet-500/[0.03] via-transparent to-indigo-500/[0.03] pointer-events-none" />
                  <CardContent className="relative p-5 md:p-6">
                    <CommandCenter
                      onCreated={(id: string) => {
                        setRunId(id)
                        setToast(`Run created: ${id.slice(0, 8)}`)
                        setTab('runs')
                      }}
                    />
                  </CardContent>
                </Card>
              )}

              {tab === 'dashboard' && (
                <Dashboard live={live} sseState={sseState} lastId={lastId} onOpen={(k: string) => setTab(k as NavKey)} onOpenRun={(id: string) => { setRunId(id); setTab('runs') }} />
              )}

              {tab === 'runs' &&
                (runId ? (
                  <RunsWithDetail runId={runId} onBack={() => setRunId('')} onOpenDetail={(id) => setRunId(id)} />
                ) : (
                  <RunsPage onOpenDetail={(id) => setRunId(id)} />
                ))}

              {tab === 'approvals' && <ApprovalsPage />}
              {tab === 'context' && <ContextPage initialRunId={runId} />}
              {tab === 'memory' && <MemoryPage />}
              {tab === 'sources' && <SourcesPage />}
              {tab === 'agents' && <AgentsPage />}
              {tab === 'telegram' && <TelegramPage />}
              {tab === 'settings' && <SettingsPage />}
              {tab === 'reports' && <ReportsPage />}
              {tab === 'audit' && <AuditPage />}

              {tab !== 'dashboard' && (
                <Card className="border-dashed border-violet-200/40 bg-white/40 backdrop-blur-sm dark:bg-white/[0.02] dark:border-white/10 hover:bg-white/60 dark:hover:bg-white/[0.04] transition-colors">
                  <CardContent className="p-4">
                    <CommandCenter
                      compact
                      onCreated={(id) => {
                        setRunId(id)
                        setToast(`Run created: ${id.slice(0, 8)}`)
                        setTab('runs')
                      }}
                    />
                  </CardContent>
                </Card>
              )}
              </motion.div>
            </AnimatePresence>
          </div>
        </main>
      </div>
    </div>
  )
}

function RunsWithDetail({ runId, onBack, onOpenDetail }: { runId: string; onBack: () => void; onOpenDetail: (id: string) => void }) {
  const [showDetail, setShowDetail] = useState(true)
  useEffect(() => {
    setShowDetail(true)
  }, [runId])
  if (showDetail) return <RunDetailPage runId={runId} onBack={() => { setShowDetail(false); onBack() }} />
  return <RunsPage onOpenDetail={(id) => { onOpenDetail(id); setShowDetail(true) }} />
}
