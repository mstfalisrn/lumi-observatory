import { useEffect, useState, useCallback } from 'react'
import { motion } from 'framer-motion'
import { api, errMsg, setToken } from './api'
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Activity,
  CheckCircle2,
  Clock,
  Layers,
  TrendingUp,
  Zap,
  AlertCircle,
  RefreshCw,
  Search,
  ChevronLeft,
  ChevronRight,
  Inbox,
  Sparkles,
  BarChart3,
  Shield,
  FileSearch,
  Play,
  Puzzle,
  Brain,
  Radio,
  Satellite,
  Send,
  Settings,
  FileText,
  ScrollText,
} from 'lucide-react'

// ---------- types ----------
type Report = {
  id: string
  report_type: string
  subject: string
  confidence: number
  created_at: string
  summary: string
  body?: unknown
}
type Run = {
  id: string
  status: string
  iteration: number
  created_at: string
  error?: string | null
  token_used?: number
  cost_used?: number
  worker_id?: string | null
  control_request?: string | null
}
type RunEvent = {
  seq: number
  event_type: string
  payload: Record<string, unknown> & { segments?: Segment[] }
  ts: string
}
type Segment = {
  segment_type: string
  token_count: number
  confidence: number
  included_reason: string
  preview?: string
  content_preview?: string
  contains_untrusted_input?: boolean
  _event?: string
  _seq?: number
}
type Approval = {
  id: string
  action_class: string
  target: string
  status: string
  impact_summary: string
  expires_at: string | null
}
type MemoryItem = {
  id: string
  content: string
  status: string
  confidence: number
  source: string
  category?: string | null
  expires_at?: string | null
  created_at: string
}
type SourceItem = {
  id: string
  name: string
  source_type: string
  reference: string
  is_enabled: boolean
  last_accessed_at: string | null
  last_observed_at: string | null
  last_content_hash: string | null
  error_series: { at: string; code: string }[]
  backoff_until: string | null
}
type SourceObservation = {
  id: string
  observed_at: string
  seq: number
  change_type: string
  content_hash: string
  change: { metadata?: Record<string, unknown>; untrusted?: boolean }
}
type DigestSchedule = {
  id: string
  name: string
  interval_minutes: number
  source_ids: string[]
  minimum_tier: string
  is_enabled: boolean
  delivery_mode: string
  last_generated_at: string | null
}
type DigestPreview = {
  source_changes: unknown[]
  risk_items: unknown[]
  delivery_mode: string
}
type SourceMonitorStatus = {
  monitor_enabled: boolean
  memory_candidates_enabled: boolean
  max_per_tick: number
  allowed_http_hosts_configured: number
}
type AgentSkill = { id: string; title: string; description: string; execution_mode: string; allowed_tools: string[]; required_scope: string[] }
type TrustSummary = { tiers: Record<string, number>; monitoring_enabled: boolean; risk_alerts_enabled: boolean; recent: { room: string; seq: number; nick: string; tier: string; score: number; reason: string; evaluated_at: string }[] }
type LoginResponse = { token: string; email: string; role: string }
type TaskCreateResponse = { run_id?: string; runId?: string; id?: string }

// ---------- helpers ----------
function useFetch<T>(path: string, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)
  const [tick, setTick] = useState(0)
  const reload = useCallback(()=> setTick(x=>x+1), [])
  const key = path
  useEffect(() => {
    if (!path) { setLoading(false); setData(null); setErr(''); return }
    let on = true
    setLoading(true); setErr('')
    api<T>(path).then(d => { if(on){ setData(d); setLoading(false) }})
      .catch(e => { if(on){ setErr(errMsg(e)); setLoading(false) }})
    return () => { on=false }
  }, [key, tick, ...deps])
  return { data, err, loading, reload }
}
function statusVariant(s: string): 'default' | 'secondary' | 'destructive' | 'outline' | 'success' | 'warning' | 'violet' {
  const v = s.toUpperCase()
  if (['COMPLETED','ACTIVE','APPROVED','AUTO_APPROVED'].includes(v)) return 'success'
  if (['FAILED','REJECTED','EXPIRED','CANCELLED'].includes(v)) return 'destructive'
  if (['QUEUED','EXECUTING','CONTEXT_BUILDING','PLANNING','POLICY_CHECK','VERIFYING','PERSISTING','PENDING','PAUSED','WAITING_APPROVAL'].includes(v)) return 'violet'
  return 'outline'
}
function runStatusVariant(s: string): 'default' | 'secondary' | 'destructive' | 'outline' | 'success' | 'warning' | 'violet' {
  const u = s.toUpperCase()
  if (u === 'COMPLETED') return 'success'
  if (u === 'FAILED' || u === 'CANCELLED') return 'destructive'
  if (u === 'EXECUTING' || u === 'QUEUED') return 'violet'
  if (u === 'PAUSED') return 'warning'
  return 'outline'
}

function Skeleton({ className = '' }: { className?: string }) {
  return <div className={'shimmer rounded-xl ' + className} />
}
function Loading(){
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {[0,1,2,3].map(i=> (
          <Card key={i} className="overflow-hidden"><CardContent className="p-4 space-y-3"><Skeleton className="h-4 w-24" /><Skeleton className="h-8 w-16" /><Skeleton className="h-3 w-full" /></CardContent></Card>
        ))}
      </div>
      <Skeleton className="h-6 w-40" />
      <div className="space-y-3">
        {[0,1,2].map(i=> <Skeleton key={i} className="h-14 w-full rounded-xl" />)}
      </div>
    </div>
  )
}
function TableSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-3">
      <Skeleton className="h-10 w-full rounded-xl" />
      {Array.from({ length: rows }).map((_, i) => <Skeleton key={i} className="h-14 w-full rounded-xl" />)}
    </div>
  )
}
function Err({msg, onRetry}:{msg:string, onRetry?:()=>void}){
  return (
    <Card className="border-red-200/50 bg-gradient-to-br from-red-50/80 to-rose-50/40 dark:border-red-900/30 dark:from-red-950/20 dark:to-zinc-900">
      <CardContent className="flex items-center justify-between gap-3 p-4">
        <span className="flex items-center gap-2.5 text-sm font-medium text-red-700 dark:text-red-300"><span className="flex h-7 w-7 items-center justify-center rounded-full bg-red-100 dark:bg-red-900/40"><AlertCircle className="h-4 w-4" /></span> {msg}</span>
        {onRetry && <Button variant="outline" size="sm" className="rounded-xl border-red-200 hover:bg-red-50 dark:border-red-800 dark:hover:bg-red-900/20" onClick={onRetry}><RefreshCw className="h-3.5 w-3.5" /> Retry</Button>}
      </CardContent>
    </Card>
  )
}
function PremiumEmpty({ title, msg, icon: Icon = Inbox, action }: { title?: string; msg: string; icon?: React.ElementType; action?: React.ReactNode }) {
  return (
    <Card className="border-dashed border-violet-200/40 bg-gradient-to-br from-white/60 via-violet-50/20 to-indigo-50/20 dark:border-white/10 dark:from-white/[0.02] dark:via-violet-950/5 dark:to-indigo-950/5">
      <CardContent className="flex flex-col items-center justify-center gap-4 py-10 text-center">
        <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-violet-100 to-indigo-100 text-violet-600 shadow-sm ring-1 ring-violet-200/50 dark:from-violet-900/30 dark:to-indigo-900/20 dark:text-violet-300 dark:ring-violet-800/30">
          <Icon className="h-7 w-7" />
        </div>
        {title && <div className="text-sm font-semibold tracking-tight">{title}</div>}
        <div className="max-w-sm text-sm leading-relaxed text-muted-foreground">{msg}</div>
        {action && <div className="pt-1">{action}</div>}
      </CardContent>
    </Card>
  )
}
function Empty({msg}:{msg:string}){ return <PremiumEmpty msg={msg} /> }

// sparkline placeholder
function Sparkline({ variant = 'violet' }: { variant?: 'violet' | 'emerald' | 'amber' | 'indigo' }) {
  const colors: Record<string, string> = {
    violet: '#8b5cf6',
    emerald: '#10b981',
    amber: '#f59e0b',
    indigo: '#6366f1',
  }
  const id = `grad-${variant}-${Math.random().toString(36).slice(2,6)}`
  return (
    <svg viewBox="0 0 60 20" className="h-6 w-full" preserveAspectRatio="none" aria-hidden>
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={colors[variant]} stopOpacity={0.35} />
          <stop offset="100%" stopColor={colors[variant]} stopOpacity={0} />
        </linearGradient>
      </defs>
      <path d="M0 14 C 6 12, 10 16, 16 10 S 24 4, 30 8 S 38 14, 44 9 S 52 2, 60 6 L 60 20 L 0 20 Z" fill={`url(#${id})`} />
      <path d="M0 14 C 6 12, 10 16, 16 10 S 24 4, 30 8 S 38 14, 44 9 S 52 2, 60 6" fill="none" stroke={colors[variant]} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" opacity={0.9} />
    </svg>
  )
}

// ---------- Login ----------
export function LoginPage({ onLogin }: { onLogin:(u:{email:string, role:string}) => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  async function submit() {
    if (!email.trim() || !password) { setErr('email and password required'); return }
    setBusy(true); setErr('')
    try {
      const r = await api<LoginResponse>('/v1/auth/login', { method:'POST', body: JSON.stringify({ email, password }) })
      setToken(r.token)
      onLogin({ email: r.email, role: r.role })
    } catch(e){ setErr(errMsg(e)) } finally { setBusy(false) }
  }
  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-bold tracking-tight flex items-center gap-2"><span className="h-1.5 w-1.5 rounded-full bg-violet-600" />Sign in</h2>
        <p className="text-sm text-muted-foreground">Local authentication — sign in.</p>
      </div>
      <div className="flex flex-col gap-3">
        <Input placeholder="email" value={email} onChange={e=>setEmail(e.target.value)} autoComplete="username" />
        <Input placeholder="password" type="password" value={password} onChange={e=>setPassword(e.target.value)} autoComplete="current-password" onKeyDown={e=> e.key==='Enter' && submit()} />
        <Button onClick={submit} disabled={busy || !email.trim() || !password} className="w-full rounded-xl h-10 font-semibold">{busy ? 'signing in…' : 'Sign in'}</Button>
      </div>
      {err && <div className="flex items-center gap-2 rounded-xl border border-red-200/50 bg-red-50/80 px-3 py-2.5 text-sm font-medium text-red-700 dark:border-red-900/30 dark:bg-red-950/20 dark:text-red-300"><AlertCircle className="h-4 w-4" /> {err}</div>}
    </div>
  )
}

// ---------- Command Center ----------
export function CommandCenter({ onCreated, compact }: { onCreated?:(runId:string)=>void, compact?:boolean }) {
  const [prompt, setPrompt] = useState('')
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [ok, setOk] = useState('')
  const [skillId, setSkillId] = useState('')
  const [target, setTarget] = useState('')
  const skills = useFetch<AgentSkill[]>('/v1/skills')
  const selectedSkill = skills.data?.find(skill => skill.id === skillId)
  function scopedTask() {
    if (!skillId) return {}
    const scope: Record<string, unknown> = { kind: 'skill', skill_id: skillId }
    if (target.trim()) {
      if (skillId === 'approved-http-observation') scope.allowed_urls = [target.trim()]
      if (skillId === 'github-observation') scope.github_repos = [target.trim()]
      if (skillId === 'technocore-read-observation') scope.allowed_rooms = [target.trim()]
    }
    return scope
  }
  async function submit() {
    if (!prompt.trim()) { setErr('prompt is required'); return }
    setBusy(true); setErr(''); setOk('')
    try {
      const r = await api<TaskCreateResponse>('/v1/tasks', { method:'POST', body: JSON.stringify({ title: title||prompt.slice(0,60), prompt, scope: scopedTask() }) })
      const id = r.run_id || r.runId || r.id || ''
      setOk(`Run queued: ${String(id).slice(0,8)}`)
      setPrompt(''); setTitle('')
      onCreated?.(String(id))
    } catch(e){ setErr(errMsg(e)) } finally { setBusy(false) }
  }
  return (
    <div>
      <div className="mb-3 flex items-center gap-2">
        <div className={compact ? "flex h-7 w-7 items-center justify-center rounded-lg bg-gradient-to-br from-violet-600 to-indigo-600 text-white shadow-sm" : "flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-violet-600 to-indigo-600 text-white shadow-[0_4px_14px_rgba(99,102,241,0.3)]"}>
          <Sparkles className={compact ? "h-3.5 w-3.5" : "h-4 w-4"} />
        </div>
        <h3 className="text-sm font-bold tracking-tight">{compact ? 'Command' : 'Command Center'}</h3>
        {!compact && <Badge variant="violet" className="text-[10px]">Live SSE</Badge>}
        {!compact && <span className="ml-auto hidden items-center gap-1 text-xs text-muted-foreground sm:flex"><span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse shadow-[0_0_6px_theme(colors.emerald.500)]" /> ready</span>}
      </div>
      {!compact && <p className="mb-3 text-sm text-muted-foreground leading-relaxed">Enter a prompt to create a run. Follow it live via SSE.</p>}
      <div className="flex flex-wrap gap-2">
        <Input placeholder="title (optional)" value={title} onChange={e=>setTitle(e.target.value)} className="flex-none sm:w-[200px]" />
        <Input placeholder="prompt — what should it do?" value={prompt} onChange={e=>setPrompt(e.target.value)} className="min-w-[220px] flex-1" onKeyDown={e=> e.key==='Enter' && submit()} />
        <Button onClick={submit} disabled={busy || !prompt.trim() || (!!selectedSkill && selectedSkill.required_scope.length > 0 && !target.trim())} className="rounded-xl px-5 font-semibold">{busy?'sending…':'▶ Run'}</Button>
      </div>
      {!compact && <div className="mt-2 grid gap-2 sm:grid-cols-[minmax(190px,0.8fr)_minmax(240px,1.2fr)]"><select value={skillId} onChange={event=>{setSkillId(event.target.value); setTarget('')}} className="h-10 rounded-xl border border-input bg-white/60 px-3 text-sm dark:bg-white/[0.04]"><option value="">General safe task</option>{skills.data?.filter(skill => skill.execution_mode === 'task').map(skill => <option key={skill.id} value={skill.id}>{skill.title}</option>)}</select>{selectedSkill && selectedSkill.required_scope.length > 0 ? <Input value={target} onChange={event=>setTarget(event.target.value)} placeholder={skillId === 'github-observation' ? 'owner/repository' : skillId === 'technocore-read-observation' ? 'configured room name' : 'exact approved HTTPS URL'} /> : <div className="flex items-center rounded-xl border border-dashed border-violet-200/70 px-3 text-xs text-muted-foreground dark:border-violet-900/40">{selectedSkill ? 'This skill has no operator target.' : 'Choose a bounded skill to add explicit scope.'}</div>}</div>}
      {selectedSkill && !compact && <div className="mt-2 text-xs text-muted-foreground">{selectedSkill.description} Tool scope: {selectedSkill.allowed_tools.join(', ') || 'scheduled only'}.</div>}
      {err && <motion.div initial={{ opacity:0, y:4 }} animate={{opacity:1, y:0}} className="mt-3 flex items-center gap-2 rounded-xl border border-red-200/50 bg-red-50 px-3 py-2 text-sm font-medium text-red-700 dark:border-red-900/30 dark:bg-red-950/20 dark:text-red-300">⚠ {err}</motion.div>}
      {ok && <motion.div initial={{ opacity:0, y:4 }} animate={{opacity:1, y:0}} className="mt-3 flex items-center gap-2 rounded-xl border border-emerald-200/50 bg-emerald-50 px-3 py-2 text-sm font-medium text-emerald-700 dark:border-emerald-900/30 dark:bg-emerald-950/20 dark:text-emerald-300">✓ {ok}</motion.div>}
    </div>
  )
}

// ---------- Dashboard ----------
type DashboardProps = {
  live: Record<string, unknown> | null
  sseState: string
  lastId: string
  onOpen: (k:string)=>void
  onOpenRun?: (id:string)=>void
}
export function Dashboard({ live, sseState, lastId, onOpen, onOpenRun }: DashboardProps) {
  const { data: runs, err: e1, loading: l1, reload: r1 } = useFetch<Run[]>('/v1/runs')
  const { data: approvals, err: e2, loading: l2, reload: r2 } = useFetch<Approval[]>('/v1/approvals')
  const { data: health } = useFetch<{status:string, time?:string}>('/health/live')
  const pending = approvals?.filter((a)=>a.status==='PENDING').length ?? 0
  const running = runs?.filter((r)=>['EXECUTING','QUEUED'].includes(r.status)).length ?? 0
  const total = runs?.length ?? 0
  const completed = runs?.filter(r=> r.status==='COMPLETED').length ?? 0
  const successPct = total ? ((completed/total)*100).toFixed(1) : '—'
  const avgTokens = (() => {
    if (!runs?.length) return '—'
    const withTok = runs.filter(r=> typeof r.token_used === 'number' && r.token_used>0)
    if (!withTok.length) return '—'
    const sum = withTok.reduce((a,b)=> a + (b.token_used||0), 0)
    return Math.round(sum/withTok.length).toString()
  })()
  // KPI card pulse for SSE
  const sseColor = sseState==='open' ? 'bg-emerald-500' : sseState==='connecting' ? 'bg-amber-500' : sseState==='error' ? 'bg-red-500' : 'bg-zinc-400'

  if (l1 || l2) {
    if (!runs && !approvals) return <Loading/>
  }

  const kpiCards = [
    { title: 'Total Runs', icon: Layers, value: total, sub: `${running} active · ${completed} completed`, accent: 'from-violet-600 to-indigo-600', light: 'from-violet-50 to-indigo-50', spark: 'violet' as const, action: ()=>onOpen('runs'), actionLabel: "View runs →" },
    { title: 'Success %', icon: TrendingUp, value: `${successPct}%`, sub: `${completed}/${total} completed`, accent: 'from-emerald-500 to-teal-600', light: 'from-emerald-50 to-teal-50', spark: 'emerald' as const },
    { title: 'Queue depth', icon: Activity, value: running, sub: 'QUEUED + EXECUTING', accent: 'from-amber-500 to-orange-500', light: 'from-amber-50 to-orange-50', spark: 'amber' as const, badge: running ? 'active jobs' : 'idle' },
    { title: 'Avg. tokens', icon: Zap, value: avgTokens, sub: 'avg per run', accent: 'from-indigo-600 to-violet-600', light: 'from-indigo-50 to-violet-50', spark: 'indigo' as const, extra: health?.status ? `health: ${health.status}` : 'checking health…' },
  ]

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold tracking-tight flex items-center gap-2.5"><span className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-violet-600 to-indigo-600 text-white shadow-md"><BarChart3 className="h-4 w-4" /></span> Dashboard</h1>
        <div className="flex items-center gap-2">
          <span className="relative flex h-2.5 w-2.5">
            {sseState==='open' && <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />}
            <span className={'relative inline-flex h-2.5 w-2.5 rounded-full ring-2 ring-white/20 ' + sseColor + (sseState==='open' ? ' shadow-[0_0_10px_theme(colors.emerald.500)]' : '')} />
          </span>
          <Badge variant={sseState==='open' ? 'success' : sseState==='error' ? 'destructive' : 'secondary'} className="capitalize rounded-full">{sseState}</Badge>
          <span className="hidden text-xs font-mono text-muted-foreground sm:inline">#{lastId?.slice(0,8) || '—'}</span>
        </div>
      </div>

      {/* KPI cards — premium glass + gradient icon + sparkline */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {kpiCards.map((k, i) => (
          <motion.div
            key={k.title}
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: i * 0.06, duration: 0.35, ease: [0.32,0.72,0,1] }}
            whileHover={{ y: -3 }}
            className="group"
          >
            <Card className="relative overflow-hidden h-full hover:shadow-[0_12px_32px_rgba(0,0,0,0.08)] dark:hover:shadow-[0_12px_32px_rgba(0,0,0,0.3)]">
              <div className={`absolute inset-0 bg-gradient-to-br ${k.light} opacity-60 dark:opacity-[0.04]`} />
              <div className="absolute -right-6 -top-6 h-20 w-20 rounded-full bg-gradient-to-br from-white/40 to-transparent blur-2xl dark:from-white/[0.03]" />
              <CardHeader className="relative pb-2">
                <CardDescription className="flex items-center gap-2 text-xs font-semibold tracking-wide uppercase">
                  <span className={`flex h-7 w-7 items-center justify-center rounded-xl bg-gradient-to-br ${k.accent} text-white shadow-sm ring-1 ring-white/15`}>
                    <k.icon className="h-3.5 w-3.5" />
                  </span>
                  {k.title}
                </CardDescription>
                <CardTitle className="text-[28px] font-bold tracking-tight leading-none mt-2">{l1 ? <Skeleton className="h-7 w-14" /> : k.value}</CardTitle>
              </CardHeader>
              <CardContent className="relative pt-0 space-y-2.5">
                <p className="text-xs font-medium text-muted-foreground">{k.sub}</p>
                <div className="opacity-80 group-hover:opacity-100 transition-opacity">
                  <Sparkline variant={k.spark} />
                </div>
                {k.action && (
                  <Button variant="ghost" size="sm" className="h-7 rounded-full px-3 text-xs font-semibold -ml-1 group-hover:bg-violet-50 group-hover:text-violet-700 dark:group-hover:bg-violet-950/40 dark:group-hover:text-violet-300" onClick={k.action}>{k.actionLabel}</Button>
                )}
                {k.badge && <Badge variant={running ? 'warning' : 'secondary'} className="text-[11px] rounded-full mt-1">{k.badge}</Badge>}
                {k.extra && <span className="flex items-center gap-1 text-xs text-muted-foreground"><Clock className="h-3 w-3" /> {k.extra}</span>}
                {k.title === 'Success %' && !l1 && (
                  <div className="h-1.5 w-full overflow-hidden rounded-full bg-black/5 dark:bg-white/10 mt-1">
                    <motion.div initial={{ width: 0 }} animate={{ width: total ? `${(completed/total)*100}%` : '0%' }} transition={{ duration: 0.8, ease: [0.32,0.72,0,1], delay: 0.3 }} className="h-full bg-gradient-to-r from-emerald-500 to-teal-500 rounded-full" />
                  </div>
                )}
              </CardContent>
            </Card>
          </motion.div>
        ))}
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <motion.div initial={{ opacity:0, y: 8 }} animate={{ opacity:1, y:0 }} transition={{ delay: 0.25, duration: 0.35 }} className="lg:col-span-2">
        <Card className="overflow-hidden">
          <div className="absolute inset-0 bg-gradient-to-br from-violet-500/[0.02] to-transparent pointer-events-none" />
          <CardHeader className="relative pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="text-sm flex items-center gap-2"><span className="h-6 w-6 rounded-lg bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 flex items-center justify-center"><Activity className="h-3.5 w-3.5" /></span> Latest Runs — timeline</CardTitle>
              <Button variant="ghost" size="sm" className="h-7 rounded-full text-xs font-semibold" onClick={()=>onOpen('runs')}>View all</Button>
            </div>
            <CardDescription>Last 5 runs · synced via live SSE</CardDescription>
          </CardHeader>
          <CardContent className="relative">
            {(e1||e2) && <div className="mb-3"><Err msg={(e1||e2) as string} onRetry={()=>{r1();r2()}} /></div>}
            {l1 ? <TableSkeleton rows={5} /> : !runs?.length ? <PremiumEmpty title="No runs yet" msg="Create your first run from Command Center and follow it live." icon={Sparkles} action={<Button size="sm" className="rounded-xl" onClick={()=>onOpen('runs')}>Create run</Button>} /> : (
              <div className="relative">
                <div className="absolute bottom-0 left-[11px] top-2 w-px bg-gradient-to-b from-violet-200 via-border to-transparent dark:from-violet-800/30" />
                <div className="space-y-3">
                  {runs!.slice(0,5).map((r, idx)=> (
                    <motion.div
                      key={r.id}
                      initial={{ opacity:0, x: -8 }}
                      animate={{ opacity:1, x: 0 }}
                      transition={{ delay: 0.3 + idx*0.06 }}
                      onClick={()=>onOpenRun?.(r.id)}
                      className="group relative flex cursor-pointer items-center gap-3 rounded-2xl border bg-card/60 p-3 pl-7 backdrop-blur-sm transition-all duration-200 hover:bg-white hover:shadow-[0_4px_16px_rgba(0,0,0,0.06)] hover:border-violet-200/50 hover:-translate-y-0.5 dark:hover:bg-white/[0.04] dark:hover:border-white/10"
                    >
                      <span className="absolute left-0 top-1/2 h-3 w-3 -translate-y-1/2 rounded-full border-2 border-background bg-gradient-to-br from-violet-600 to-indigo-600 shadow-md ring-2 ring-violet-100 dark:ring-violet-900/40" style={{ left: '5px' }} />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="truncate font-mono text-xs font-semibold tracking-tight">{r.id.slice(0,8)}</span>
                          <Badge variant={runStatusVariant(r.status)} className="text-[10px] rounded-full">{r.status}</Badge>
                          <span className="text-xs font-medium text-muted-foreground">iter {r.iteration}</span>
                        </div>
                        <div className="text-xs text-muted-foreground truncate">{r.created_at?.slice(0,19)} {r.error && <span className="text-red-600 dark:text-red-400">· {r.error.slice(0,60)}</span>}</div>
                      </div>
                      <ChevronRight className="h-4 w-4 text-muted-foreground group-hover:text-violet-600 group-hover:translate-x-0.5 transition-all" />
                    </motion.div>
                  ))}
                </div>
              </div>
            )}
            {/* SSE live pulse detail */}
            <div className="mt-4 flex items-center gap-2.5 rounded-2xl border bg-gradient-to-br from-zinc-50 to-white px-3.5 py-2.5 text-xs shadow-sm dark:from-zinc-900/50 dark:to-zinc-900/20 border-black/[0.04] dark:border-white/5">
              <motion.span animate={sseState==='open' ? { scale:[1,1.25,1]} : {}} transition={{ repeat: Infinity, duration: 1.4 }} className={'h-2 w-2 rounded-full shadow-sm ' + sseColor + (sseState==='open'?' shadow-[0_0_8px_theme(colors.emerald.500)]':'')} />
              <span className="font-semibold tracking-wide text-muted-foreground">SSE</span>
              <Badge variant="outline" className="h-5 rounded-full text-[10px] bg-white dark:bg-white/5">{sseState}</Badge>
              <span className="truncate font-mono text-[11px] text-muted-foreground hidden sm:inline">{live ? `last event: ${String((live as any).event_type || (live as any).seq || JSON.stringify(live).slice(0,60))}` : 'waiting'}</span>
              <span className="ml-auto hidden h-1.5 w-12 rounded-full bg-gradient-to-r from-emerald-400 to-teal-400 opacity-20 sm:block" />
            </div>
          </CardContent>
        </Card>
        </motion.div>

        <motion.div initial={{ opacity:0, y: 8 }} animate={{ opacity:1, y:0 }} transition={{ delay: 0.32, duration: 0.35 }}>
        <Card className="h-full">
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm"><span className="h-7 w-7 rounded-xl bg-gradient-to-br from-amber-400 to-orange-500 flex items-center justify-center text-white shadow-sm"><CheckCircle2 className="h-4 w-4" /></span> Approvals</CardTitle>
            <CardDescription>{pending} pending · quick view</CardDescription>
          </CardHeader>
          <CardContent>
            {l2 ? <div className="space-y-2"><Skeleton className="h-14 w-full rounded-xl"/><Skeleton className="h-14 w-full rounded-xl"/></div> : !approvals?.length ? <PremiumEmpty msg="No pending approvals — all clear." icon={Shield} /> : (
              <div className="space-y-2.5">
                {approvals!.slice(0,4).map(a=> (
                  <div key={a.id} className="group rounded-2xl border bg-white/40 p-3.5 backdrop-blur-sm hover:bg-white hover:shadow-md hover:border-violet-200/40 transition-all duration-200 dark:bg-white/[0.02] dark:hover:bg-white/[0.04] dark:hover:border-white/10">
                    <div className="flex items-center gap-2">
                      <Badge variant={statusVariant(a.status)} className="text-[10px] rounded-full">{a.status}</Badge>
                      <span className="truncate text-sm font-semibold tracking-tight">{a.action_class}</span>
                    </div>
                    <div className="truncate text-xs text-muted-foreground mt-1">{a.target.slice(0,80)}</div>
                  </div>
                ))}
                <Button variant="outline" size="sm" className="w-full rounded-xl font-semibold mt-1" onClick={()=>onOpen('approvals')}>Go to Approvals {pending>0 && <Badge variant="destructive" className="ml-2 h-5 rounded-full text-[10px]">{pending}</Badge>}</Button>
              </div>
            )}
            <div className="mt-4 rounded-2xl border bg-gradient-to-br from-violet-50/60 to-indigo-50/40 p-3.5 dark:from-violet-950/10 dark:to-indigo-950/10 border-violet-100/50 dark:border-violet-900/20">
              <div className="text-xs font-bold tracking-wide flex items-center gap-1.5"><span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" /> Health</div>
              <div className="text-sm font-semibold mt-1">{health ? `✓ ${health.status}` : '...'}</div>
              <div className="text-xs font-mono text-muted-foreground">{health?.time?.slice(0,19) || ''}</div>
            </div>
          </CardContent>
        </Card>
        </motion.div>
      </div>
    </div>
  )
}

// ---------- Runs ----------
export function RunsPage({ onOpenDetail }: { onOpenDetail?:(id:string)=>void }) {
  const [limit, setLimit] = useState(20)
  const [offset, setOffset] = useState(0)
  const [statusFilter, setStatusFilter] = useState('ALL')
  const { data, err, loading, reload } = useFetch<Run[]>(`/v1/runs?limit=${limit}&offset=${offset}`, [limit, offset])
  const [q, setQ] = useState('')
  const filtered = (() => {
    let out = data || []
    if (q) out = out.filter(r=> r.id.includes(q) || r.status.toLowerCase().includes(q.toLowerCase()))
    if (statusFilter!=='ALL') out = out.filter(r=> r.status.toUpperCase()===statusFilter)
    return out
  })()
  const hasNext = (data?.length ?? 0) >= limit
  const hasPrev = offset > 0
  function goNext(){ if(hasNext) setOffset(o=>o+limit) }
  function goPrev(){ setOffset(o=>Math.max(0, o-limit)) }
  function onLimitChange(v:number){ setLimit(v); setOffset(0) }

  const statuses = ['ALL','QUEUED','EXECUTING','COMPLETED','FAILED','CANCELLED','PAUSED']

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold tracking-tight flex items-center gap-2.5"><span className="h-8 w-8 rounded-xl bg-gradient-to-br from-violet-600 to-indigo-600 flex items-center justify-center text-white shadow-md"><Play className="h-4 w-4 fill-white/20" /></span> Runs</h1>
        <Badge variant="outline" className="rounded-full bg-white/60 backdrop-blur-sm dark:bg-white/[0.04] font-mono text-xs">offset {offset} · limit {limit}{data ? ` · ${data.length} records` : ''}</Badge>
      </div>

      <Card className="overflow-hidden">
        <CardContent className="p-3 sm:p-4">
          <div className="flex flex-wrap items-center gap-2.5">
            <div className="relative flex-1 min-w-[180px]">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input placeholder="filter (id/status)" value={q} onChange={e=>setQ(e.target.value)} className="pl-9 rounded-xl" />
            </div>
            <select value={statusFilter} onChange={e=>setStatusFilter(e.target.value)} className="h-10 rounded-xl border border-input bg-white/60 backdrop-blur-sm px-3 text-sm font-medium shadow-sm hover:border-violet-200 focus:border-violet-300 focus:ring-2 focus:ring-violet-500/20 outline-none transition-all dark:bg-white/[0.04] dark:hover:border-white/10">
              {statuses.map(s=> <option key={s} value={s}>{s==='ALL' ? 'All statuses' : s}</option>)}
            </select>
            <Button variant="outline" size="sm" className="rounded-xl" onClick={()=>reload()}><RefreshCw className="h-4 w-4" /> Refresh</Button>
            <select value={String(limit)} onChange={e=>onLimitChange(parseInt(e.target.value))} className="h-10 rounded-xl border border-input bg-white/60 backdrop-blur-sm px-2.5 text-sm font-medium shadow-sm hover:border-violet-200 focus:border-violet-300 focus:ring-2 focus:ring-violet-500/20 outline-none transition-all dark:bg-white/[0.04]">
              <option value="10">10</option><option value="20">20</option><option value="50">50</option>
            </select>
          </div>
        </CardContent>
      </Card>

      {loading ? <TableSkeleton rows={5} /> : err ? <Err msg={err} onRetry={reload}/> : !filtered?.length ? <PremiumEmpty title="No runs found" msg={data?.length? 'No runs match the filter — try clearing it.':'No runs yet — create your first run with Command Center.'} icon={FileSearch} action={data?.length ? <Button size="sm" variant="outline" className="rounded-xl" onClick={()=>{setQ(''); setStatusFilter('ALL')}}>Clear filter</Button> : undefined} /> : (
        <Card className="overflow-hidden p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b bg-gradient-to-r from-zinc-50/80 to-white dark:from-zinc-900/40 dark:to-zinc-900/10 text-xs uppercase tracking-widest text-muted-foreground">
                <tr><th className="px-4 py-3 text-left font-semibold">ID</th><th className="px-3 py-3 text-left font-semibold">Status</th><th className="px-3 py-3 text-left font-semibold">Iter</th><th className="px-3 py-3 text-left font-semibold">Error</th><th className="px-3 py-3 text-right"></th></tr>
              </thead>
              <tbody>
                {filtered!.map((r, idx) => (
                  <motion.tr
                    key={r.id}
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: idx * 0.03, duration: 0.25 }}
                    className="border-b last:border-0 hover:bg-violet-50/40 dark:hover:bg-white/[0.03] transition-colors group even:bg-zinc-50/40 dark:even:bg-white/[0.015]"
                  >
                    <td className="px-4 py-3.5 font-mono text-xs font-semibold tracking-tight" title={r.id}><span className="inline-flex items-center gap-2"><span className="h-1.5 w-1.5 rounded-full bg-violet-500 opacity-60 group-hover:opacity-100 transition-opacity" />{r.id.slice(0,8)}</span></td>
                    <td className="px-3 py-3.5"><Badge variant={runStatusVariant(r.status)} className="text-[10px] rounded-full shadow-sm">{r.status}</Badge></td>
                    <td className="px-3 py-3.5"><span className="inline-flex h-6 min-w-6 items-center justify-center rounded-full bg-zinc-100 px-2 text-xs font-semibold dark:bg-white/10">{r.iteration}</span></td>
                    <td className="max-w-[260px] truncate px-3 py-3.5 text-xs font-medium text-red-600 dark:text-red-400">{r.error||<span className="text-muted-foreground/50">—</span>}</td>
                    <td className="px-3 py-3.5 text-right"><Button size="sm" variant="outline" className="h-7 rounded-full text-xs font-semibold group-hover:border-violet-200 group-hover:bg-violet-50 dark:group-hover:border-violet-800/30 dark:group-hover:bg-violet-950/20" onClick={()=>onOpenDetail?.(r.id)}>Details <ChevronRight className="h-3 w-3" /></Button></td>
                  </motion.tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex items-center justify-between border-t bg-zinc-50/50 p-3 dark:bg-white/[0.02]">
            <span className="text-xs font-medium text-muted-foreground">{filtered.length} shown{data && statusFilter!=='ALL' ? ` (filtered)`:''}</span>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" className="rounded-xl" disabled={!hasPrev} onClick={goPrev}><ChevronLeft className="h-4 w-4" /> Back</Button>
              <Button variant="outline" size="sm" className="rounded-xl" disabled={!hasNext} onClick={goNext}>Next <ChevronRight className="h-4 w-4" /></Button>
            </div>
          </div>
        </Card>
      )}
    </div>
  )
}

const RUN_STEPS = ['QUEUED','EXECUTING','VERIFYING','COMPLETED'] as const
function Stepper({ status }: { status: string }) {
  const upper = status.toUpperCase()
  const idx = RUN_STEPS.indexOf(upper as any)
  const isTerminalFail = ['FAILED','CANCELLED'].includes(upper)
  const activeIdx = idx >=0 ? idx : (upper==='FAILED' || upper==='CANCELLED' ? 1 : 0)
  return (
    <div className="flex items-center gap-1 sm:gap-2 flex-wrap">
      {RUN_STEPS.map((s,i)=> {
        const done = i < activeIdx
        const active = i===activeIdx && !isTerminalFail
        const fail = isTerminalFail && i===activeIdx
        return (
          <div key={s} className="flex items-center gap-1 sm:gap-2">
            <div className={'flex h-8 w-8 items-center justify-center rounded-full text-xs font-bold shadow-sm ring-1 transition-all duration-300 ' + (done ? 'bg-gradient-to-br from-emerald-500 to-teal-600 text-white ring-emerald-200 dark:ring-emerald-800' : active ? 'bg-gradient-to-br from-violet-600 to-indigo-600 text-white ring-violet-200 dark:ring-violet-800 shadow-[0_4px_12px_rgba(99,102,241,0.3)]' : fail ? 'bg-gradient-to-br from-red-500 to-rose-600 text-white ring-red-200' : 'bg-zinc-100 text-zinc-500 ring-zinc-200 dark:bg-white/5 dark:text-zinc-400 dark:ring-white/10')}>
              {done ? '✓' : i+1}
            </div>
            <span className={'hidden text-xs font-semibold tracking-wide sm:inline ' + (active ? 'text-foreground' : done ? 'text-emerald-700 dark:text-emerald-300' : 'text-muted-foreground')}>{s}</span>
            {i < RUN_STEPS.length-1 && <div className={'h-0.5 w-6 sm:w-10 rounded-full transition-colors ' + (done ? 'bg-emerald-500' : 'bg-zinc-200 dark:bg-white/10')} />}
          </div>
        )
      })}
      {isTerminalFail && <Badge variant="destructive" className="ml-2 rounded-full text-[10px]">{upper}</Badge>}
    </div>
  )
}

export function RunDetailPage({ runId, onBack }: { runId:string, onBack:()=>void }) {
  const { data: run, reload: reloadRun } = useFetch<Run>(`/v1/runs/${runId}`, [runId])
  const { data, err, loading, reload } = useFetch<RunEvent[]>(`/v1/runs/${runId}/events`, [runId])
  const [filter, setFilter] = useState('')
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState('')
  const [tab, setTab] = useState<'overview'|'events'|'timeline'>('overview')
  const evs = !filter ? data : data?.filter((e)=> e.event_type.toLowerCase().includes(filter.toLowerCase()))
  const active = run && ['EXECUTING','QUEUED'].includes(run.status)
  const terminal = run && ['FAILED','COMPLETED','CANCELLED'].includes(run.status)
  async function control(action: string) {
    setBusy(action); setMsg('')
    try { await api(`/v1/runs/${runId}/control`, { method:'POST', body: JSON.stringify({ action }) }); setMsg(`✓ ${action} sent`); reloadRun() }
    catch(e){ setMsg('⚠ '+errMsg(e)) } finally { setBusy('') }
  }
  async function retry() {
    setBusy('retry'); setMsg('')
    const idem = typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? `retry:${runId}:${(crypto as Crypto).randomUUID()}`
      : `retry:${runId}:${Date.now()}`
    try { await api(`/v1/runs/${runId}/retry`, { method:'POST', body: '{}', headers: { 'Idempotency-Key': idem } }); setMsg('✓ requeued'); reloadRun() }
    catch(e){ setMsg('⚠ '+errMsg(e)) } finally { setBusy('') }
  }
  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="sm" className="rounded-xl gap-1.5" onClick={onBack}><ChevronLeft className="h-4 w-4" /> Back</Button>
        <h2 className="truncate text-lg font-bold tracking-tight flex items-center gap-2"><span className="h-7 w-7 rounded-xl bg-gradient-to-br from-violet-600 to-indigo-600 flex items-center justify-center text-white shadow-sm"><FileSearch className="h-3.5 w-3.5" /></span> Run: {runId.slice(0,8)} <span className="hidden font-mono text-xs font-normal text-muted-foreground sm:inline bg-zinc-100 px-2 py-1 rounded-full dark:bg-white/10">{runId.slice(0,8)}…</span></h2>
        {run && <Badge variant={runStatusVariant(run.status)} className="rounded-full">{run.status}</Badge>}
      </div>

      {run && (
        <Card className="overflow-hidden">
          <div className="absolute inset-0 bg-gradient-to-br from-violet-500/[0.03] to-indigo-500/[0.02] pointer-events-none" />
          <CardContent className="relative space-y-4 p-5">
            <Stepper status={run.status} />
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <Badge variant="outline" className="font-mono rounded-full bg-white dark:bg-white/5">{run.id.slice(0,8)}</Badge>
              <span className="inline-flex items-center gap-1 rounded-full bg-zinc-100 px-2.5 py-1 text-xs font-medium dark:bg-white/10">iter {run.iteration}</span>
              <span className="inline-flex items-center gap-1 rounded-full bg-zinc-100 px-2.5 py-1 text-xs font-medium dark:bg-white/10">token {run.token_used ?? 0}</span>
              <span className="inline-flex items-center gap-1 rounded-full bg-zinc-100 px-2.5 py-1 text-xs font-medium dark:bg-white/10">cost ${run.cost_used ?? 0}</span>
              <span className="text-muted-foreground font-mono text-xs">worker {run.worker_id?.slice(0,8) || '—'}</span>
              {run.control_request && <Badge variant="violet" className="rounded-full">control: {run.control_request}</Badge>}
            </div>
            {run.error && <div className="rounded-xl border border-red-200/50 bg-red-50 px-4 py-3 text-sm font-medium text-red-700 dark:border-red-900/30 dark:bg-red-950/20 dark:text-red-300">⚠ {run.error}</div>}
            <div className="flex flex-wrap gap-2">
              {active && <>
                <Button size="sm" variant="outline" className="rounded-xl" disabled={!!busy} onClick={()=>control('pause')}>{busy==='pause'?'...':'⏸ Pause'}</Button>
                <Button size="sm" variant="outline" className="rounded-xl" disabled={!!busy} onClick={()=>control('resume')}>{busy==='resume'?'...':'▶️ Resume'}</Button>
                <Button size="sm" variant="destructive" className="rounded-xl" disabled={!!busy} onClick={()=>control('stop')}>{busy==='stop'?'...':'⏹ Stop'}</Button>
              </>}
              {terminal && <Button size="sm" className="rounded-xl" disabled={!!busy} onClick={retry}>{busy==='retry'?'...':'🔄 Run again'}</Button>}
            </div>
          </CardContent>
        </Card>
      )}
      {msg && <motion.div initial={{ opacity:0, y:4 }} animate={{ opacity:1, y:0 }} className={'rounded-xl border px-4 py-2.5 text-sm font-medium ' + (msg.startsWith('⚠') ? 'border-red-200/50 bg-red-50 text-red-700 dark:border-red-900/30 dark:bg-red-950/20 dark:text-red-300' : 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900/30 dark:bg-emerald-950/20 dark:text-emerald-300')}>{msg}</motion.div>}

      {/* tabs premium */}
      <div className="flex gap-1 rounded-2xl border bg-zinc-100/70 p-1.5 backdrop-blur-sm dark:bg-white/[0.04] dark:border-white/5">
        {(['overview','events','timeline'] as const).map(t=> (
          <button key={t} onClick={()=>setTab(t)} className={'flex-1 rounded-xl px-3 py-2 text-sm font-semibold capitalize transition-all duration-200 ' + (tab===t ? 'bg-white shadow-sm text-foreground ring-1 ring-black/5 dark:bg-white/10 dark:ring-white/5' : 'text-muted-foreground hover:text-foreground hover:bg-white/40 dark:hover:bg-white/[0.03]')}>
            {t==='overview' ? 'Overview' : t==='events' ? `Events (${evs?.length ?? 0})` : 'Timeline'}
          </button>
        ))}
      </div>

      <Card>
        <CardContent className="p-4 sm:p-5">
          <div className="mb-4 flex gap-2">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input placeholder="filter by event type" value={filter} onChange={e=>setFilter(e.target.value)} className="pl-9 rounded-xl" />
            </div>
            <Button variant="outline" size="sm" className="rounded-xl" onClick={reload}><RefreshCw className="h-4 w-4" /> Refresh</Button>
          </div>

          {tab==='overview' && (
            <div className="space-y-3">
              {loading ? <TableSkeleton/> : err ? <Err msg={err} onRetry={reload}/> : !evs?.length ? <PremiumEmpty msg="no events" icon={Inbox} /> : (
                <div className="space-y-3">
                  <div className="text-xs font-medium text-muted-foreground">{evs.length} event</div>
                  {evs.slice(0,5).map((e,i)=> (
                    <motion.div initial={{ opacity:0, y:6 }} animate={{ opacity:1, y:0 }} transition={{ delay: i*0.04 }} className="group rounded-2xl border bg-white/50 p-4 backdrop-blur-sm hover:bg-white hover:shadow-md transition-all dark:bg-white/[0.02] dark:hover:bg-white/[0.04]" key={i}>
                      <div className="flex items-center gap-2"><Badge variant="violet" className="text-[10px] rounded-full">{e.event_type}</Badge> <span className="text-xs font-mono text-muted-foreground">seq {e.seq}</span> <span className="text-xs text-muted-foreground">{e.ts?.slice(0,19)}</span></div>
                      {e.payload && <pre className="mt-3 max-h-40 overflow-auto rounded-xl bg-zinc-950 text-zinc-100 p-3 text-xs leading-relaxed font-mono dark:bg-black/40">{JSON.stringify(e.payload, null, 2).slice(0,1200)}</pre>}
                    </motion.div>
                  ))}
                  {evs.length>5 && <p className="text-center text-xs font-medium text-muted-foreground py-2">… and {evs.length-5} more — go to Events tab</p>}
                </div>
              )}
            </div>
          )}

          {tab==='events' && (
            loading ? <TableSkeleton/> : err ? <Err msg={err} onRetry={reload}/> : !evs?.length ? <PremiumEmpty msg="no events" /> : (
              <div className="space-y-3">
                <div className="text-xs font-medium text-muted-foreground">{evs.length} event</div>
                {evs.map((e,i)=> (
                  <motion.div initial={{ opacity:0, y:6 }} animate={{ opacity:1, y:0 }} transition={{ delay: i*0.02 }} className="group rounded-2xl border bg-white/50 p-4 backdrop-blur-sm hover:bg-white hover:shadow-md transition-all dark:bg-white/[0.02] dark:hover:bg-white/[0.04]" key={i}>
                    <div className="flex items-center gap-2"><Badge variant="outline" className="text-[10px] rounded-full bg-white dark:bg-white/5">{e.event_type}</Badge> <Badge variant="secondary" className="text-[10px] rounded-full">seq {e.seq}</Badge> <span className="text-xs text-muted-foreground font-mono">{e.ts?.slice(0,19)}</span></div>
                    {e.payload && <pre className="mt-3 max-h-48 overflow-auto rounded-xl bg-zinc-950 text-zinc-100 p-3 text-xs leading-relaxed font-mono dark:bg-black/40">{JSON.stringify(e.payload, null, 2).slice(0,2000)}</pre>}
                  </motion.div>
                ))}
              </div>
            )
          )}

          {tab==='timeline' && (
            loading ? <TableSkeleton/> : !evs?.length ? <PremiumEmpty msg="no events" /> : (
              <div className="relative pl-6">
                <div className="absolute bottom-0 left-[9px] top-2 w-px bg-gradient-to-b from-violet-200 via-zinc-200 to-transparent dark:from-violet-800/30 dark:via-white/10" />
                <div className="space-y-4">
                  {evs.map((e,i)=> (
                    <motion.div key={i} initial={{ opacity:0, x:-8 }} animate={{ opacity:1, x:0 }} transition={{ delay: i*0.03 }} className="relative">
                      <span className="absolute -left-6 top-1 h-3 w-3 rounded-full border-2 border-background bg-gradient-to-br from-violet-600 to-indigo-600 shadow-sm ring-2 ring-violet-100 dark:ring-violet-900/30" />
                      <div className="rounded-2xl border bg-white/60 p-4 backdrop-blur-sm hover:shadow-md hover:bg-white transition-all dark:bg-white/[0.03] dark:hover:bg-white/[0.05]">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-bold tracking-tight">{e.event_type}</span>
                          <Badge variant="outline" className="text-[10px] rounded-full bg-white dark:bg-white/5 font-mono">#{e.seq}</Badge>
                          <span className="text-xs text-muted-foreground font-mono">{e.ts?.slice(0,19)}</span>
                        </div>
                        {e.payload && Object.keys(e.payload).length>0 && (
                          <details className="mt-3"><summary className="cursor-pointer text-xs font-semibold text-muted-foreground hover:text-foreground">payload</summary><pre className="mt-2 max-h-40 overflow-auto rounded-xl bg-zinc-950 text-zinc-100 p-3 text-xs font-mono leading-relaxed dark:bg-black/40">{JSON.stringify(e.payload, null, 2).slice(0,1500)}</pre></details>
                        )}
                      </div>
                    </motion.div>
                  ))}
                </div>
              </div>
            )
          )}
        </CardContent>
      </Card>
    </div>
  )
}

// ---------- Approvals ----------
export function ApprovalsPage() {
  const { data, err, loading, reload } = useFetch<Approval[]>('/v1/approvals')
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState('')
  const [err2, setErr2] = useState('')
  async function decide(id: string, decision: string) {
    setBusy(id+decision); setErr2(''); setMsg('')
    try { await api(`/v1/approvals/${id}/decision`, { method: 'POST', body: JSON.stringify({ decision, approval_id: id }) }); setMsg('decision saved ✓'); reload() }
    catch(e){ setErr2(errMsg(e)) } finally { setBusy('') }
  }
  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between"><h1 className="text-xl font-bold tracking-tight flex items-center gap-2.5"><span className="h-8 w-8 rounded-xl bg-gradient-to-br from-amber-500 to-orange-500 flex items-center justify-center text-white shadow-md"><Clock className="h-4 w-4" /></span> Approvals</h1><Button variant="outline" size="sm" className="rounded-xl" onClick={reload}><RefreshCw className="h-4 w-4" /> Refresh</Button></div>
      {loading ? <TableSkeleton/> : err ? <Err msg={err} onRetry={reload}/> : !data?.length ? <PremiumEmpty title="No approvals pending" msg="There are no pending approvals right now. New actions will appear here." icon={Shield} /> : data.map((a, idx) => (
        <motion.div key={a.id} initial={{ opacity:0, y:8 }} animate={{ opacity:1, y:0 }} transition={{ delay: idx*0.04 }}>
        <Card className="group hover:shadow-[0_8px_24px_rgba(0,0,0,0.06)] hover:-translate-y-0.5 transition-all duration-200">
          <CardContent className="p-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2"><Badge variant={statusVariant(a.status)} className="rounded-full">{a.status}</Badge><span className="font-bold tracking-tight">{a.action_class}</span><span className="text-muted-foreground truncate">— {a.target.slice(0,80)}</span></div>
                <div className="mt-1.5 text-sm text-muted-foreground leading-relaxed">impact: {a.impact_summary}</div>
                <div className="text-xs font-mono text-muted-foreground mt-1">expires: {a.expires_at?.slice(0,19) || '—'}</div>
              </div>
              {a.status === 'PENDING' && (
                <div className="flex gap-2">
                  <Button size="sm" className="rounded-xl" disabled={!!busy} onClick={() => decide(a.id, 'approve')}>{busy===a.id+'approve'?'...':'✅ Approve'}</Button>
                  <Button size="sm" variant="destructive" className="rounded-xl" disabled={!!busy} onClick={() => decide(a.id, 'reject')}>{busy===a.id+'reject'?'...':'❌ Reject'}</Button>
                </div>
              )}
            </div>
          </CardContent>
        </Card>
        </motion.div>
      ))}
      {msg && <motion.div initial={{ opacity:0, y:4 }} animate={{opacity:1,y:0}} className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm font-medium text-emerald-700 dark:border-emerald-900/30 dark:bg-emerald-950/20 dark:text-emerald-300">{msg}</motion.div>}
      {err2 && <motion.div initial={{ opacity:0, y:4 }} animate={{opacity:1,y:0}} className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-medium text-red-700 dark:border-red-900/30 dark:bg-red-950/20 dark:text-red-300">⚠ {err2}</motion.div>}
    </div>
  )
}

// ---------- Context Inspector ----------
export function ContextPage({ initialRunId }: { initialRunId?:string }) {
  const [runId, setRunId] = useState(initialRunId || '')
  useEffect(()=>{ if(initialRunId) setRunId(initialRunId)},[initialRunId])
  const { data: runs } = useFetch<Run[]>('/v1/runs?limit=20')
  const path = runId ? `/v1/runs/${runId}/events` : ''
  const { data, err, loading, reload } = useFetch<RunEvent[]>(path, [runId])
  const segments: Segment[] = (data||[]).flatMap((e)=> (e.payload?.segments as Segment[] | undefined ||[]).map((s)=> ({...s, _event:e.event_type, _seq:e.seq})))
  return (
    <div className="space-y-5">
      <h1 className="text-xl font-bold tracking-tight flex items-center gap-2.5"><span className="h-8 w-8 rounded-xl bg-gradient-to-br from-violet-600 to-indigo-600 flex items-center justify-center text-white shadow-md"><Puzzle className="h-4 w-4" /></span> Context Inspector</h1>
      <Card>
        <CardContent className="flex flex-wrap gap-2 p-4">
          {runs && <select value={runId} onChange={e=>setRunId(e.target.value)} className="h-10 rounded-xl border border-input bg-white/60 backdrop-blur-sm px-3 text-sm font-medium shadow-sm hover:border-violet-200 focus:border-violet-300 focus:ring-2 focus:ring-violet-500/20 outline-none transition-all dark:bg-white/[0.04]"><option value="">select run…</option>{runs.map((r)=> <option key={r.id} value={r.id}>{r.id.slice(0,8)} · {r.status}</option>)}</select>}
          <Input placeholder="run_id" value={runId} onChange={e => setRunId(e.target.value)} className="min-w-[260px] flex-1 rounded-xl" />
          <Button variant="outline" size="sm" className="rounded-xl" onClick={reload} disabled={!runId}><RefreshCw className="h-4 w-4" /> Load</Button>
        </CardContent>
      </Card>
      {!runId ? <PremiumEmpty title="Select run" msg="Select a run to view context segment metadata." icon={FileSearch} /> : loading ? <TableSkeleton/> : err ? <Err msg={err} onRetry={reload}/> : !segments.length ? <PremiumEmpty msg="No segments for this run (segments expected in event payload)." icon={Inbox} /> : (
        <div className="space-y-3">
          <div className="text-xs font-medium text-muted-foreground">{segments.length} segment · {data?.length} event</div>
          {segments.map((s, j) => (
            <motion.div key={j} initial={{ opacity:0, y:6 }} animate={{ opacity:1, y:0 }} transition={{ delay: j*0.04 }}>
            <Card className="border-l-2 border-l-violet-500 hover:shadow-md hover:-translate-y-0.5 transition-all">
              <CardContent className="p-4">
                <div className="flex flex-wrap items-center gap-2"><Badge className="rounded-full">{s.segment_type}</Badge><span className="text-xs font-medium text-muted-foreground bg-zinc-100 px-2 py-1 rounded-full dark:bg-white/10">{s.token_count} tok · conf {s.confidence}</span><Badge variant="outline" className="text-[10px] rounded-full bg-white dark:bg-white/5 font-mono">{s._event} #{s._seq}</Badge> {s.contains_untrusted_input && <Badge variant="destructive" className="text-[10px] rounded-full">UNTRUSTED</Badge>}</div>
                <div className="mt-2 text-xs font-medium text-muted-foreground">reason: {s.included_reason}</div>
                {s.preview && <pre className="mt-3 max-h-32 overflow-auto rounded-xl bg-zinc-950 text-zinc-100 p-3 text-xs font-mono leading-relaxed dark:bg-black/40">{String(s.preview).slice(0,800)}</pre>}
                {s.content_preview && <pre className="mt-3 max-h-32 overflow-auto rounded-xl bg-zinc-950 text-zinc-100 p-3 text-xs font-mono leading-relaxed dark:bg-black/40">{String(s.content_preview).slice(0,800)}</pre>}
              </CardContent>
            </Card>
            </motion.div>
          ))}
          <details className="rounded-2xl border bg-white/40 p-4 backdrop-blur-sm dark:bg-white/[0.02]"><summary className="cursor-pointer text-sm font-semibold text-muted-foreground hover:text-foreground">Raw events ({data?.length})</summary>
            <div className="mt-4 space-y-2">{data!.map((e,i)=>(<Card key={i}><CardContent className="p-4"><Badge variant="outline" className="text-[10px] rounded-full">{e.event_type}</Badge><pre className="mt-3 max-h-40 overflow-auto rounded-xl bg-zinc-950 text-zinc-100 p-3 text-xs font-mono dark:bg-black/40">{JSON.stringify(e.payload||{}, null,2).slice(0,1500)}</pre></CardContent></Card>))}</div>
          </details>
        </div>
      )}
    </div>
  )
}

// ---------- Memory ----------
export function MemoryPage() {
  const [status,setStatus]=useState('candidate')
  const [q,setQ]=useState('')
  const path = q ? `/v1/memory?q=${encodeURIComponent(q)}&status=${status}&limit=50` : `/v1/memory?status=${status}&limit=50`
  const { data, err, loading, reload } = useFetch<MemoryItem[]>(path, [status,q])
  const [busy,setBusy]=useState('')
  const [msg,setMsg]=useState('')
  const [content,setContent]=useState('')
  const [source,setSource]=useState('')
  const [confidence,setConfidence]=useState('0.7')
  const [category,setCategory]=useState('')
  const [creating,setCreating]=useState(false)
  async function decide(id: string, decision: string) {
    setBusy(id); setMsg('')
    try { await api(`/v1/memory/${id}/decision`, { method: 'POST', body: JSON.stringify({ decision }) }); setMsg('✓ saved'); reload() }
    catch(e){ setMsg('⚠ '+errMsg(e)) } finally { setBusy('') }
  }
  async function createCandidate(e: React.FormEvent){
    e.preventDefault()
    if(!content.trim()){ setMsg('⚠ content required'); return }
    const conf = parseFloat(confidence)
    if(isNaN(conf) || conf<0 || conf>1){ setMsg('⚠ confidence must be between 0 and 1'); return }
    setCreating(true); setMsg('')
    try{
      await api('/v1/memory', { method:'POST', body: JSON.stringify({ content: content.trim(), source: source.trim()||undefined, confidence: conf, category: category.trim()||null }) })
      setMsg('✓ candidate created'); setContent(''); setSource(''); setCategory(''); setConfidence('0.7'); reload()
    }catch(ex){ setMsg('⚠ '+errMsg(ex)) } finally { setCreating(false) }
  }
  return (
    <div className="space-y-5">
      <h1 className="text-xl font-bold tracking-tight flex items-center gap-2.5"><span className="h-8 w-8 rounded-xl bg-gradient-to-br from-violet-600 to-indigo-600 flex items-center justify-center text-white shadow-md"><Brain className="h-4 w-4" /></span> Memory</h1>
      <Card className="overflow-hidden border-violet-200/30 bg-gradient-to-br from-violet-50/40 via-white to-indigo-50/20 dark:from-violet-950/10 dark:via-zinc-900/20 dark:to-indigo-950/10">
        <div className="absolute inset-0 bg-gradient-to-br from-violet-500/[0.03] to-transparent pointer-events-none" />
        <CardHeader className="relative pb-3"><CardTitle className="text-sm flex items-center gap-2">Create new candidate <Badge variant="violet" className="text-[10px] rounded-full">POST /v1/memory</Badge></CardTitle><CardDescription>high-confidence (&gt;0.85) records are automatically approved after 2+ successful runs</CardDescription></CardHeader>
        <CardContent className="relative">
          <form onSubmit={createCandidate} className="flex flex-col gap-3">
            <textarea placeholder="content (required)" value={content} onChange={e=>setContent(e.target.value)} rows={3} className="min-h-[80px] w-full rounded-xl border border-input bg-white/60 backdrop-blur-sm px-3.5 py-3 text-sm placeholder:text-muted-foreground/60 hover:border-violet-200 focus:border-violet-300 focus:outline-none focus:ring-2 focus:ring-violet-500/20 transition-all dark:bg-white/[0.04] dark:hover:border-white/10" />
            <div className="flex flex-wrap gap-2">
              <Input placeholder="source (optional)" value={source} onChange={e=>setSource(e.target.value)} className="min-w-[160px] flex-1 rounded-xl" />
              <Input placeholder="category (optional)" value={category} onChange={e=>setCategory(e.target.value)} className="min-w-[160px] flex-1 rounded-xl" />
              <Input type="number" min={0} max={1} step={0.1} value={confidence} onChange={e=>setConfidence(e.target.value)} className="w-[120px] rounded-xl" />
              <Button type="submit" className="rounded-xl" disabled={creating || !content.trim()}>{creating?'creating…':'＋ Create'}</Button>
            </div>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="flex flex-wrap gap-2 p-3">
          <select value={status} onChange={e=>setStatus(e.target.value)} className="h-10 rounded-xl border border-input bg-white/60 backdrop-blur-sm px-3 text-sm font-medium shadow-sm hover:border-violet-200 focus:border-violet-300 focus:ring-2 focus:ring-violet-500/20 outline-none transition-all dark:bg-white/[0.04]">
            <option value="candidate">candidate</option><option value="active">active</option><option value="rejected">rejected</option>
          </select>
          <div className="relative flex-1 min-w-[160px]">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input placeholder="search..." value={q} onChange={e=>setQ(e.target.value)} className="pl-9 rounded-xl" />
          </div>
          <Button variant="outline" size="sm" className="rounded-xl" onClick={reload}><RefreshCw className="h-4 w-4" /> Refresh</Button>
        </CardContent>
      </Card>

      {loading ? <TableSkeleton/> : err ? <Err msg={err} onRetry={reload}/> : !data?.length ? <PremiumEmpty msg={`No records for "${status}"`} title="No records found" /> : data.map((m, idx)=> (
        <motion.div key={m.id} initial={{ opacity:0, y:6 }} animate={{ opacity:1, y:0 }} transition={{ delay: idx*0.03 }}>
        <Card className="group hover:shadow-md hover:-translate-y-0.5 transition-all">
          <CardContent className="space-y-3 p-5">
            <p className="text-sm leading-relaxed font-medium">{m.content}</p>
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <Badge variant={statusVariant(m.status)} className="text-[10px] rounded-full">{m.status}</Badge>
              <span className="bg-zinc-100 px-2.5 py-1 rounded-full font-medium dark:bg-white/10">conf {m.confidence}</span><span className="text-muted-foreground">· {m.source} {m.category?`· ${m.category}`:''}</span>
              {m.confidence>0.85 && <Badge variant="success" className="border-emerald-200 text-[10px] rounded-full">auto-promote candidate</Badge>}
            </div>
            <div className="flex gap-2">
              <Button size="sm" variant="outline" className="rounded-xl" disabled={!!busy} onClick={() => decide(m.id, 'approve')}>✅ Approve</Button>
              <Button size="sm" variant="destructive" className="rounded-xl" disabled={!!busy} onClick={() => decide(m.id, 'reject')}>❌ Reject</Button>
            </div>
          </CardContent>
        </Card>
        </motion.div>
      ))}
      {msg && <motion.div initial={{ opacity:0, y:4 }} animate={{opacity:1,y:0}} className={'rounded-xl border px-4 py-3 text-sm font-medium ' + (msg.startsWith('⚠') ? 'border-red-200 bg-red-50 text-red-700 dark:border-red-900/30 dark:bg-red-950/20 dark:text-red-300' : 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900/30 dark:bg-emerald-950/20 dark:text-emerald-300')}>{msg}</motion.div>}
    </div>
  )
}

// ---------- Sources & report-only digests ----------
function DigestControls({ sources }: { sources: SourceItem[] }) {
  const schedules = useFetch<DigestSchedule[]>('/v1/digest-schedules')
  const preview = useFetch<DigestPreview>('/v1/digests/preview?hours=24&minimum_tier=WATCH')
  const [name, setName] = useState('daily operational digest')
  const [interval, setInterval] = useState('1440')
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  function toggleSource(id: string) {
    setSelectedIds(current => current.includes(id) ? current.filter(value => value !== id) : [...current, id])
  }
  async function createSchedule() {
    setBusy(true); setMsg('')
    try {
      await api<DigestSchedule>('/v1/digest-schedules', { method: 'POST', body: JSON.stringify({ name, interval_minutes: Number(interval), source_ids: selectedIds, minimum_tier: 'WATCH', is_enabled: false }) })
      setMsg('✓ schedule saved as disabled'); schedules.reload()
    } catch (error) { setMsg('⚠ ' + errMsg(error)) } finally { setBusy(false) }
  }
  async function toggleSchedule(schedule: DigestSchedule) {
    setBusy(true); setMsg('')
    try {
      await api<DigestSchedule>(`/v1/digest-schedules/${schedule.id}`, { method: 'PUT', body: JSON.stringify({ is_enabled: !schedule.is_enabled }) })
      setMsg(`✓ ${schedule.name} ${schedule.is_enabled ? 'disabled' : 'enabled'}`); schedules.reload()
    } catch (error) { setMsg('⚠ ' + errMsg(error)) } finally { setBusy(false) }
  }
  async function generate(schedule: DigestSchedule) {
    setBusy(true); setMsg('')
    try {
      const result = await api<{ created: boolean; report_id?: string }> (`/v1/digest-schedules/${schedule.id}/generate`, { method: 'POST' })
      setMsg(result.created ? `✓ report ${result.report_id?.slice(0, 8) || 'created'}` : '✓ no changes; cursor advanced')
      schedules.reload(); preview.reload()
    } catch (error) { setMsg('⚠ ' + errMsg(error)) } finally { setBusy(false) }
  }

  return <div className="space-y-3">
    <Card className="border-indigo-200/40 bg-gradient-to-br from-indigo-50/50 to-violet-50/20 dark:border-indigo-900/30 dark:from-indigo-950/10">
      <CardHeader><CardTitle className="flex items-center gap-2 text-base"><FileText className="h-4 w-4 text-indigo-600" /> Report-only digests</CardTitle><CardDescription>Build deterministic local reports from stored source changes and risk metadata. Delivery remains disabled.</CardDescription></CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-2 sm:grid-cols-2"><Input value={name} onChange={event=>setName(event.target.value)} placeholder="digest name" /><Input type="number" min="15" max="10080" value={interval} onChange={event=>setInterval(event.target.value)} /></div>
        <div className="rounded-xl border border-dashed border-indigo-200/70 p-3 text-xs dark:border-indigo-900/40"><div className="mb-2 font-semibold">Scope (empty = all sources)</div><div className="flex flex-wrap gap-x-4 gap-y-2">{sources.map(source => <label key={source.id} className="flex items-center gap-1.5"><input type="checkbox" checked={selectedIds.includes(source.id)} onChange={()=>toggleSource(source.id)} /> {source.name}</label>)}</div></div>
        <Button size="sm" className="rounded-xl" disabled={busy || !name.trim()} onClick={createSchedule}>Save disabled schedule</Button>
      </CardContent>
    </Card>
    {preview.data && <div className="grid grid-cols-2 gap-3"><Card><CardContent className="p-3 text-center text-sm"><div className="text-xs text-muted-foreground">24h source changes</div><div className="text-xl font-bold">{preview.data.source_changes.length}</div></CardContent></Card><Card><CardContent className="p-3 text-center text-sm"><div className="text-xs text-muted-foreground">Risk items ≥ WATCH</div><div className="text-xl font-bold">{preview.data.risk_items.length}</div></CardContent></Card></div>}
    {schedules.loading ? <TableSkeleton rows={1} /> : schedules.err ? <Err msg={schedules.err} onRetry={schedules.reload} /> : !schedules.data?.length ? <PremiumEmpty title="No digest schedules" msg="Schedules are saved disabled and generate reports only; enable them only after reviewing the environment toggle." icon={FileText} /> : schedules.data.map(schedule => <Card key={schedule.id}><CardContent className="flex flex-wrap items-center justify-between gap-3 p-4"><div><div className="font-semibold">{schedule.name} <Badge variant={schedule.is_enabled ? 'success' : 'secondary'} className="ml-2 text-[10px] rounded-full">{schedule.is_enabled ? 'enabled' : 'disabled'}</Badge></div><div className="mt-1 text-xs text-muted-foreground">every {schedule.interval_minutes} min · {schedule.delivery_mode} · last: {schedule.last_generated_at?.slice(0,19) || 'never'}</div></div><div className="flex gap-2"><Button size="sm" variant="outline" className="rounded-xl" disabled={busy} onClick={()=>toggleSchedule(schedule)}>{schedule.is_enabled ? 'Disable' : 'Enable'}</Button><Button size="sm" variant="outline" className="rounded-xl" disabled={busy} onClick={()=>generate(schedule)}>Generate</Button></div></CardContent></Card>)}
    {msg && <div className={'rounded-xl border px-3 py-2 text-sm font-medium ' + (msg.startsWith('⚠') ? 'border-red-200 bg-red-50 text-red-700 dark:border-red-900/30 dark:bg-red-950/20 dark:text-red-300' : 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900/30 dark:bg-emerald-950/20 dark:text-emerald-300')}>{msg}</div>}
  </div>
}

export function SourcesPage() {
  const sources = useFetch<SourceItem[]>('/v1/sources')
  const status = useFetch<SourceMonitorStatus>('/v1/sources/status')
  const [name, setName] = useState('')
  const [sourceType, setSourceType] = useState('http_json')
  const [reference, setReference] = useState('')
  const [enableOnSave, setEnableOnSave] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const observations = useFetch<SourceObservation[]>(selected ? `/v1/sources/${selected}/observations` : '', [selected])
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const data = sources.data || []
  const referenceLabel = sourceType === 'http_json' ? 'approved https URL' : sourceType === 'github_repo' ? 'owner/repository' : sourceType === 'technocore_room' ? 'room' : 'not required for internal health'

  function config() {
    if (sourceType === 'http_json') return { url: reference.trim(), ingest_mode: 'metadata' }
    if (sourceType === 'github_repo') return { repo: reference.trim() }
    if (sourceType === 'technocore_room') return { room: reference.trim() }
    return {}
  }
  async function createSource() {
    setBusy(true); setMsg('')
    try {
      await api<SourceItem>('/v1/sources', { method: 'POST', body: JSON.stringify({ name, source_type: sourceType, config: config(), is_enabled: enableOnSave }) })
      setName(''); setReference(''); setEnableOnSave(false); setMsg('✓ source saved'); sources.reload(); status.reload()
    } catch (error) { setMsg('⚠ ' + errMsg(error)) } finally { setBusy(false) }
  }
  async function toggleSource(source: SourceItem) {
    setBusy(true); setMsg('')
    try {
      await api<SourceItem>(`/v1/sources/${source.id}`, { method: 'PUT', body: JSON.stringify({ is_enabled: !source.is_enabled }) })
      setMsg(`✓ ${source.name} ${source.is_enabled ? 'disabled' : 'enabled'}`); sources.reload()
    } catch (error) { setMsg('⚠ ' + errMsg(error)) } finally { setBusy(false) }
  }
  async function scanSource(source: SourceItem) {
    setBusy(true); setMsg('')
    try {
      const outcome = await api<{ change_type: string; changed: boolean; error_code?: string }> (`/v1/sources/${source.id}/scan`, { method: 'POST' })
      setMsg(outcome.error_code ? `⚠ ${outcome.error_code}` : `✓ ${source.name}: ${outcome.change_type.toLowerCase()}`)
      sources.reload(); if (selected === source.id) observations.reload()
    } catch (error) { setMsg('⚠ ' + errMsg(error)) } finally { setBusy(false) }
  }

  return <div className="space-y-5">
    <div className="flex flex-wrap items-center justify-between gap-3"><h1 className="text-xl font-bold tracking-tight flex items-center gap-2.5"><span className="h-8 w-8 rounded-xl bg-gradient-to-br from-violet-600 to-indigo-600 flex items-center justify-center text-white shadow-md"><Radio className="h-4 w-4" /></span> Sources</h1><Button variant="outline" size="sm" className="rounded-xl" onClick={()=>{sources.reload(); status.reload()}}><RefreshCw className="h-4 w-4" /> Refresh</Button></div>
    {status.data && <div className="flex flex-wrap gap-2 text-xs"><Badge variant={status.data.monitor_enabled ? 'success' : 'secondary'} className="rounded-full">monitoring {status.data.monitor_enabled ? 'enabled' : 'disabled'}</Badge><Badge variant={status.data.memory_candidates_enabled ? 'violet' : 'outline'} className="rounded-full">memory candidates {status.data.memory_candidates_enabled ? 'enabled' : 'disabled'}</Badge><Badge variant="outline" className="rounded-full">{status.data.allowed_http_hosts_configured} approved HTTP hosts</Badge></div>}
    <Card><CardHeader><CardTitle className="text-base">Add a controlled source</CardTitle><CardDescription>HTTP sources must match the server allowlist. No credentials or arbitrary network targets are stored.</CardDescription></CardHeader><CardContent className="space-y-3"><div className="grid gap-2 sm:grid-cols-3"><Input value={name} onChange={event=>setName(event.target.value)} placeholder="source name" /><select value={sourceType} onChange={event=>setSourceType(event.target.value)} className="h-10 rounded-xl border border-input bg-white/60 px-3 text-sm dark:bg-white/[0.04]"><option value="http_json">HTTP / JSON</option><option value="github_repo">GitHub repository</option><option value="internal_health">Internal health</option><option value="technocore_room">Technocore room</option></select><Input value={reference} disabled={sourceType === 'internal_health'} onChange={event=>setReference(event.target.value)} placeholder={referenceLabel} /></div><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={enableOnSave} onChange={event=>setEnableOnSave(event.target.checked)} /> Enable source after save (global monitor switch still applies)</label><Button className="rounded-xl" disabled={busy || !name.trim() || (sourceType !== 'internal_health' && !reference.trim())} onClick={createSource}>Save source</Button></CardContent></Card>
    {sources.loading ? <TableSkeleton/> : sources.err ? <Err msg={sources.err} onRetry={sources.reload}/> : !data.length ? <PremiumEmpty msg="No sources have been registered. Start with a scoped, read-only source." title="No sources found" icon={Radio} /> : data.map((source, index) => <motion.div key={source.id} initial={{ opacity:0, y:6 }} animate={{ opacity:1, y:0 }} transition={{ delay:index*0.03 }}><Card className="group hover:shadow-md transition-all"><CardContent className="flex flex-wrap items-center justify-between gap-3 p-5"><div><div className="flex items-center gap-2"><span className="font-bold">{source.name}</span><Badge variant="outline" className="rounded-full text-[10px]">{source.source_type}</Badge><Badge variant={source.is_enabled ? 'success' : 'secondary'} className="rounded-full text-[10px]">{source.is_enabled ? 'enabled' : 'disabled'}</Badge></div><div className="mt-1 max-w-xl truncate font-mono text-xs text-muted-foreground">{source.reference || 'internal://health'} · observed {source.last_observed_at?.slice(0,19) || 'never'} · errors {source.error_series.length}</div>{source.backoff_until && <div className="mt-1 text-xs text-amber-700 dark:text-amber-300">backoff until {source.backoff_until.slice(0,19)}</div>}</div><div className="flex gap-2"><Button size="sm" variant="outline" className="rounded-xl" disabled={busy} onClick={()=>toggleSource(source)}>{source.is_enabled ? 'Disable' : 'Enable'}</Button><Button size="sm" variant="outline" className="rounded-xl" disabled={busy} onClick={()=>scanSource(source)}>Scan</Button><Button size="sm" variant="ghost" className="rounded-xl" onClick={()=>setSelected(selected === source.id ? null : source.id)}>{selected === source.id ? 'Hide events' : 'Events'}</Button></div></CardContent></Card></motion.div>)}
    {selected && <Card><CardHeader><CardTitle className="text-base">Source observation events</CardTitle><CardDescription>Only bounded metadata is stored. Remote text remains untrusted and is never auto-activated as memory.</CardDescription></CardHeader><CardContent>{observations.loading ? <TableSkeleton rows={2} /> : observations.err ? <Err msg={observations.err} onRetry={observations.reload} /> : !observations.data?.length ? <Empty msg="No observation events yet." /> : <div className="space-y-2">{observations.data.map(event => <div key={event.id} className="rounded-xl border border-zinc-100 p-3 text-xs dark:border-white/10"><div className="flex gap-2"><Badge variant={event.change_type === 'ERROR' ? 'destructive' : event.change_type === 'UNCHANGED' ? 'secondary' : 'violet'} className="rounded-full text-[10px]">{event.change_type}</Badge><span className="font-mono text-muted-foreground">{event.observed_at.slice(0,19)} · {event.content_hash.slice(0,12)}</span></div>{event.change.metadata && <pre className="mt-2 max-h-28 overflow-auto rounded-lg bg-zinc-950 p-2 text-zinc-100">{JSON.stringify(event.change.metadata, null, 2)}</pre>}</div>)}</div>}</CardContent></Card>}
    <DigestControls sources={data} />
    {msg && <div className={'rounded-xl border px-3 py-2 text-sm font-medium ' + (msg.startsWith('⚠') ? 'border-red-200 bg-red-50 text-red-700 dark:border-red-900/30 dark:bg-red-950/20 dark:text-red-300' : 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900/30 dark:bg-emerald-950/20 dark:text-emerald-300')}>{msg}</div>}
  </div>
}


// ---------- Telegram ----------
export function TelegramPage() {
  const { data, err, loading, reload } = useFetch<{telegram_token_configured:boolean, telegram_allowed_user_ids_count:number, telegram_group_enabled:boolean}>('/v1/settings/non-secret')
  if (loading) return <div className="space-y-5"><h1 className="text-xl font-bold tracking-tight flex items-center gap-2.5"><span className="h-8 w-8 rounded-xl bg-gradient-to-br from-sky-500 to-blue-600 flex items-center justify-center text-white shadow-md"><Send className="h-4 w-4" /></span> Telegram</h1><TableSkeleton rows={2}/></div>
  if (err) return <div className="space-y-5"><h1 className="text-xl font-bold tracking-tight">✈️ Telegram</h1><Err msg={err} onRetry={reload}/></div>
  return (
    <div className="space-y-5">
      <h1 className="text-xl font-bold tracking-tight flex items-center gap-2.5"><span className="h-8 w-8 rounded-xl bg-gradient-to-br from-sky-500 to-blue-600 flex items-center justify-center text-white shadow-md"><Send className="h-4 w-4" /></span> Telegram</h1>
      {data && (<Card><CardContent className="space-y-3 p-5">
        <div><Badge variant={data.telegram_token_configured ? 'success' : 'destructive'} className="rounded-full">{data.telegram_token_configured ? 'bot token ✓' : '⚠️ no token'}</Badge></div>
        <div className="text-sm">Allowlisted users: <span className="font-bold bg-zinc-100 px-2 py-1 rounded-full text-xs dark:bg-white/10">{data.telegram_allowed_user_ids_count}</span></div>
        <div className="text-sm">Group: <Badge variant={data.telegram_group_enabled ? 'success' : 'secondary'} className="rounded-full text-xs">{data.telegram_group_enabled ? 'open' : 'closed'}</Badge></div>
      </CardContent></Card>)}
      <Button variant="outline" size="sm" className="rounded-xl" onClick={reload}><RefreshCw className="h-4 w-4" /> Refresh</Button>
    </div>
  )
}

// ---------- Settings ----------
export function SettingsPage() {
  const { data, err, loading, reload } = useFetch<{app_env:string, llm_provider:string, llm_model:string, llm_base_url:string, llm_key_configured:boolean, run_max_iterations:number, run_max_wall_seconds:number}>('/v1/settings/non-secret')
  if (loading) return <div className="space-y-5"><h1 className="text-xl font-bold tracking-tight">⚙️ Settings</h1><TableSkeleton rows={3}/></div>
  if (err) return <div className="space-y-5"><h1 className="text-xl font-bold tracking-tight">⚙️ Settings</h1><Err msg={err} onRetry={reload}/></div>
  return (
    <div className="space-y-5">
      <h1 className="text-xl font-bold tracking-tight flex items-center gap-2.5"><span className="h-8 w-8 rounded-xl bg-zinc-900 flex items-center justify-center text-white shadow-md dark:bg-white dark:text-zinc-900"><Settings className="h-4 w-4" /></span> Settings</h1>

      <Card className="border-white/10 bg-white/40 backdrop-blur-sm dark:bg-white/[0.02]">
        <CardContent className="p-5">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h3 className="text-sm font-bold tracking-tight">About</h3>
              <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">
                LUMI — Standalone, observation-focused, Telegram + web managed agent runtime. Evidence-based reports, web and Telegram control.
              </p>
            </div>
            <Settings className="h-4 w-4 text-muted-foreground shrink-0 mt-1" />
          </div>
        </CardContent>
      </Card>
      {data && (<Card><CardContent className="space-y-2.5 p-5 text-sm">
        <div className="flex items-center justify-between py-1.5 border-b border-zinc-100 dark:border-white/5"><span className="text-muted-foreground">Environment</span> <Badge variant="outline" className="rounded-full font-mono">{data.app_env}</Badge></div>
        <div className="flex items-center justify-between py-1.5 border-b border-zinc-100 dark:border-white/5"><span className="text-muted-foreground">Provider</span> <Badge variant="violet" className="rounded-full">{data.llm_provider}</Badge></div>
        <div className="flex items-center justify-between py-1.5 border-b border-zinc-100 dark:border-white/5"><span className="text-muted-foreground">Model</span> <span className="font-mono text-xs bg-zinc-100 px-2 py-1 rounded-full dark:bg-white/10">{data.llm_model || '-'}</span></div>
        <div className="flex items-center justify-between py-1.5 border-b border-zinc-100 dark:border-white/5"><span className="text-muted-foreground">Base URL</span> <span className="font-mono text-xs truncate max-w-[200px]">{data.llm_base_url || '-'}</span></div>
        <div className="flex items-center justify-between py-1.5 border-b border-zinc-100 dark:border-white/5"><span className="text-muted-foreground">LLM key</span> <Badge variant={data.llm_key_configured ? 'success' : 'outline'} className="rounded-full">{data.llm_key_configured ? '✓' : '✗'}</Badge></div>
        <div className="flex items-center justify-between py-1.5 border-b border-zinc-100 dark:border-white/5"><span className="text-muted-foreground">Max iterations</span> <span className="font-bold">{data.run_max_iterations}</span></div>
        <div className="flex items-center justify-between py-1.5"><span className="text-muted-foreground">Max wall (s)</span> <span className="font-bold">{data.run_max_wall_seconds}</span></div>
      </CardContent></Card>)}
      <div className="rounded-2xl border border-amber-200/50 bg-gradient-to-br from-amber-50 to-orange-50/40 px-4 py-3 text-sm font-medium text-amber-800 dark:border-amber-900/30 dark:from-amber-950/20 dark:to-orange-950/10 dark:text-amber-200">🔒 Secret values are never displayed.</div>
      <Button variant="outline" size="sm" className="rounded-xl" onClick={reload}><RefreshCw className="h-4 w-4" /> Refresh</Button>
    </div>
  )
}

// ---------- Reports ----------
export function ReportsPage() {
  const { data, err, loading, reload } = useFetch<Report[]>('/v1/reports?limit=50')
  const [selected, setSelected] = useState<Report | null>(null)
  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between"><h1 className="text-xl font-bold tracking-tight flex items-center gap-2.5"><span className="h-8 w-8 rounded-xl bg-gradient-to-br from-violet-600 to-indigo-600 flex items-center justify-center text-white shadow-md"><FileText className="h-4 w-4" /></span> Reports</h1><div className="flex gap-2"><Button variant="outline" size="sm" className="rounded-xl" onClick={reload}><RefreshCw className="h-4 w-4" /> Refresh</Button>{selected && <Button variant="ghost" size="sm" className="rounded-xl" onClick={()=>setSelected(null)}>← Back to list</Button>}<Badge variant="outline" className="rounded-full bg-white dark:bg-white/5">{data ? `${data.length} reports` : ''}</Badge></div></div>
      {selected ? (
        <Card className="overflow-hidden border-violet-200/30">
          <div className="absolute inset-0 bg-gradient-to-br from-violet-500/[0.03] to-transparent pointer-events-none" />
          <CardHeader className="relative"><CardTitle className="flex items-center gap-2 text-base">{selected.subject || '(no subject)'} <Badge className="rounded-full">{selected.report_type}</Badge></CardTitle><CardDescription className="font-mono text-xs">id {selected.id} · {selected.created_at?.slice(0,19)} · confidence {selected.confidence}</CardDescription></CardHeader>
          <CardContent className="relative space-y-4">
            <div><span className="text-sm font-bold tracking-tight">Summary</span><p className="text-sm leading-relaxed text-muted-foreground mt-1">{selected.summary || '—'}</p></div>
            <div><span className="text-sm font-bold tracking-tight">Body</span><pre className="mt-2 max-h-64 overflow-auto rounded-xl bg-zinc-950 text-zinc-100 p-4 text-xs font-mono leading-relaxed dark:bg-black/50">{selected.body ? JSON.stringify(selected.body, null, 2) : (selected.summary || '—')}</pre></div>
            <div className="text-xs font-mono text-muted-foreground">report_type: {selected.report_type} · subject: {selected.subject} · confidence: {selected.confidence}</div>
          </CardContent>
        </Card>
      ) : loading ? <TableSkeleton/> : err ? <Err msg={err} onRetry={reload}/> : !data?.length ? <PremiumEmpty msg="no reports." icon={FileText} /> : (
        <div className="space-y-3">
          {data.map((r, idx)=> (
            <motion.div key={r.id} initial={{ opacity:0, y:6 }} animate={{ opacity:1, y:0 }} transition={{ delay: idx*0.02 }}>
            <Card className="group cursor-pointer hover:shadow-md hover:-translate-y-0.5 hover:border-violet-200/40 transition-all" onClick={()=>setSelected(r)}>
              <CardContent className="p-5">
                <div className="flex items-center gap-2"><Badge variant="outline" className="text-[10px] rounded-full bg-white dark:bg-white/5">{r.report_type}</Badge><span className="font-bold tracking-tight truncate">{r.subject || '(no subject)'}</span><Badge variant="violet" className="text-[10px] rounded-full">confidence {r.confidence}</Badge></div>
                <div className="text-xs font-mono text-muted-foreground mt-1">{r.id.slice(0,8)} · {r.created_at?.slice(0,19)}</div>
                <div className="mt-2 line-clamp-2 text-sm leading-relaxed text-muted-foreground">{r.summary?.slice(0,200) || '—'}</div>
                <Button size="sm" variant="ghost" className="mt-3 h-7 rounded-full text-xs font-semibold group-hover:bg-violet-50 group-hover:text-violet-700 dark:group-hover:bg-violet-950/30" onClick={(e)=>{e.stopPropagation(); setSelected(r)}}>Details <ChevronRight className="h-3 w-3" /></Button>
              </CardContent>
            </Card>
            </motion.div>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------- Audit (now backed by /v1/reports, keeps legacy route) ----------
export function AuditPage() {
  const { data, err, loading, reload } = useFetch<Report[]>('/v1/reports?limit=50')
  const [selected, setSelected] = useState<Report | null>(null)
  return (
    <div className="space-y-5">
      <h1 className="text-xl font-bold tracking-tight flex items-center gap-2.5"><span className="h-8 w-8 rounded-xl bg-zinc-900 flex items-center justify-center text-white shadow-md dark:bg-white dark:text-zinc-900"><ScrollText className="h-4 w-4" /></span> Audit</h1>
      <p className="text-sm text-muted-foreground">GET /api/v1/reports — append-only list of reports.</p>
      <div className="flex gap-2"><Button variant="outline" size="sm" className="rounded-xl" onClick={reload}><RefreshCw className="h-4 w-4" /> Refresh</Button>{selected && <Button variant="ghost" size="sm" className="rounded-xl" onClick={()=>setSelected(null)}>← Back to list</Button>}<Badge variant="outline" className="rounded-full bg-white dark:bg-white/5">{data ? `${data.length} reports` : ''}</Badge></div>
      {selected ? (
        <Card className="overflow-hidden border-violet-200/30">
          <div className="absolute inset-0 bg-gradient-to-br from-violet-500/[0.03] to-transparent pointer-events-none" />
          <CardHeader className="relative"><CardTitle className="flex items-center gap-2 text-base">{selected.subject || '(no subject)'} <Badge className="rounded-full">{selected.report_type}</Badge></CardTitle><CardDescription className="font-mono text-xs">id {selected.id} · {selected.created_at?.slice(0,19)} · confidence {selected.confidence}</CardDescription></CardHeader>
          <CardContent className="relative space-y-4">
            <div><span className="text-sm font-bold">Summary</span><p className="text-sm leading-relaxed text-muted-foreground mt-1">{selected.summary || '—'}</p></div>
            <div><span className="text-sm font-bold">Body</span><pre className="mt-2 max-h-64 overflow-auto rounded-xl bg-zinc-950 text-zinc-100 p-4 text-xs font-mono leading-relaxed dark:bg-black/50">{selected.body ? JSON.stringify(selected.body, null, 2) : (selected.summary || '—')}</pre></div>
            <div className="text-xs font-mono text-muted-foreground">report_type: {selected.report_type} · subject: {selected.subject} · confidence: {selected.confidence}</div>
          </CardContent>
        </Card>
      ) : loading ? <TableSkeleton/> : err ? <Err msg={err} onRetry={reload}/> : !data?.length ? <PremiumEmpty msg="no reports." icon={ScrollText} /> : (
        <div className="space-y-3">
          {data.map((r, idx)=> (
            <motion.div key={r.id} initial={{ opacity:0, y:6 }} animate={{ opacity:1, y:0 }} transition={{ delay: idx*0.02 }}>
            <Card className="group cursor-pointer hover:shadow-md hover:-translate-y-0.5 transition-all" onClick={()=>setSelected(r)}>
              <CardContent className="p-5">
                <div className="flex items-center gap-2"><Badge variant="outline" className="text-[10px] rounded-full bg-white dark:bg-white/5">{r.report_type}</Badge><span className="font-bold tracking-tight truncate">{r.subject || '(no subject)'}</span><Badge variant="violet" className="text-[10px] rounded-full">confidence {r.confidence}</Badge></div>
                <div className="text-xs font-mono text-muted-foreground mt-1">{r.id.slice(0,8)} · {r.created_at?.slice(0,19)}</div>
                <div className="mt-2 text-sm leading-relaxed text-muted-foreground">{r.summary?.slice(0,200) || '—'}</div>
                <Button size="sm" variant="ghost" className="mt-3 h-7 rounded-full text-xs font-semibold group-hover:bg-zinc-100 dark:group-hover:bg-white/10" onClick={(e)=>{e.stopPropagation(); setSelected(r)}}>Details <ChevronRight className="h-3 w-3" /></Button>
              </CardContent>
            </Card>
            </motion.div>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------- Trust Center ----------
// Review durable agent evaluations, risk posture, and bounded skill manifests.
type AgentEvaluation = {
  id: string; room: string; seq: number; global_seq: number; nick: string; did: string | null
  text: string; score: number; tier: string; reason: string; dimensions: Record<string, unknown>
  model: string; evaluated_at: string; created_at: string; link: string
}
export function AgentsPage() {
  const [tier, setTier] = useState('') // '' = all
  const [room, setRoom] = useState('')
  const [limit] = useState(20)
  const [offset, setOffset] = useState(0)
  const qs = new URLSearchParams()
  if (tier) qs.set('tier', tier)
  if (room.trim()) qs.set('room', room.trim())
  qs.set('limit', String(limit)); qs.set('offset', String(offset))
  const { data, err, loading, reload } = useFetch<{total:number, items: AgentEvaluation[]}>('/v1/agents/evaluations?' + qs.toString(), [tier, room, offset])
  const stats = useFetch<{total:number, by_tier: Record<string, number>}>('/v1/agents/evaluations/stats')
  const trust = useFetch<TrustSummary>('/v1/trust/summary')
  const skills = useFetch<AgentSkill[]>('/v1/skills')
  const items = data?.items || []
  const total = data?.total || 0
  const trustData = trust.data
  const skillData = skills.data
  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h1 className="text-xl font-bold tracking-tight flex items-center gap-2.5">
          <span className="h-8 w-8 rounded-xl bg-gradient-to-br from-violet-600 to-indigo-600 flex items-center justify-center text-white shadow-md"><Shield className="h-4 w-4" /></span> Trust Center
        </h1>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" className="rounded-xl" onClick={()=>{reload(); stats.reload(); trust.reload(); skills.reload()}}><RefreshCw className="h-4 w-4" /> Refresh</Button>
        </div>
      </div>

      {/* stats */}
      {stats.data && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {(['SAFE','WATCH','RISKY','DANGEROUS'] as const).map(k => (
            <Card key={k}><CardContent className="p-4 text-center"><div className="text-xs font-medium text-muted-foreground">{k}</div><div className="text-xl font-bold">{stats.data!.by_tier[k] ?? 0}</div></CardContent></Card>
          ))}
          <Card className="col-span-2 sm:col-span-4"><CardContent className="p-3 text-center text-sm">Total: <span className="font-bold">{stats.data.total}</span></CardContent></Card>
        </div>
      )}

      {trustData && <Card className="border-violet-200/40 bg-gradient-to-br from-violet-50/40 to-indigo-50/20 dark:border-violet-900/30 dark:from-violet-950/10"><CardContent className="space-y-3 p-4"><div className="flex flex-wrap items-center gap-2"><span className="font-semibold text-sm">Live controls</span><Badge variant={trustData.monitoring_enabled ? 'success' : 'secondary'} className="rounded-full text-[10px]">monitoring {trustData.monitoring_enabled ? 'enabled' : 'disabled'}</Badge><Badge variant={trustData.risk_alerts_enabled ? 'warning' : 'outline'} className="rounded-full text-[10px]">risk alerts {trustData.risk_alerts_enabled ? 'enabled' : 'disabled'}</Badge></div><div className="grid grid-cols-2 gap-2 sm:grid-cols-4">{(['SAFE','WATCH','RISKY','DANGEROUS'] as const).map(key => <div key={key} className="rounded-xl border border-white/60 bg-white/60 p-2 text-center text-xs dark:border-white/10 dark:bg-white/[0.03]"><div className="text-muted-foreground">{key}</div><div className="font-bold text-base">{trustData.tiers[key] ?? 0}</div></div>)}</div></CardContent></Card>}

      {skillData && <Card><CardHeader><CardTitle className="text-base flex items-center gap-2"><Puzzle className="h-4 w-4" /> Bounded capabilities</CardTitle><CardDescription>Source-controlled manifests constrain skill tool access and required scope; they are not arbitrary prompts.</CardDescription></CardHeader><CardContent className="grid gap-2 md:grid-cols-2">{skillData.map(skill => <div key={skill.id} className="rounded-xl border border-zinc-100 p-3 dark:border-white/10"><div className="flex items-center justify-between gap-2"><span className="font-semibold text-sm">{skill.title}</span><Badge variant="outline" className="rounded-full text-[10px]">{skill.execution_mode}</Badge></div><p className="mt-1 text-xs leading-relaxed text-muted-foreground">{skill.description}</p><div className="mt-2 flex flex-wrap gap-1">{skill.allowed_tools.map(tool => <Badge key={tool} variant="secondary" className="rounded-full text-[9px]">{tool}</Badge>)}</div></div>)}</CardContent></Card>}

      {/* filters */}
      <Card><CardContent className="flex flex-wrap gap-2 p-3">
        <select value={tier} onChange={e=>{setTier(e.target.value); setOffset(0)}} className="h-10 rounded-xl border border-input bg-white/60 px-3 text-sm font-medium dark:bg-white/[0.04]">
          <option value="">All tiers</option><option value="SAFE">SAFE</option><option value="WATCH">WATCH</option><option value="RISKY">RISKY</option><option value="DANGEROUS">DANGEROUS</option>
        </select>
        <div className="relative flex-1 min-w-[160px] flex items-center gap-2">
          <Input placeholder="filter by room (optional)" value={room} onChange={e=>{setRoom(e.target.value)}} className="rounded-xl" />
        </div>
        <Button variant="outline" size="sm" className="rounded-xl" onClick={()=>{setRoom(''); setTier(''); setOffset(0)}}>Clear</Button>
      </CardContent></Card>

      {loading ? <TableSkeleton/> : err ? <Err msg={err} onRetry={reload}/> : !items.length ? <PremiumEmpty title="No evaluations" msg="No agent evaluations match the filter." icon={Shield} /> : (
        <>
          <div className="space-y-3">
            {items.map((ev, idx)=> (
              <motion.div key={ev.id} initial={{opacity:0,y:6}} animate={{opacity:1,y:0}} transition={{delay: idx*0.02}}>
                <Card className="hover:shadow-md transition-all">
                  <CardContent className="p-4 space-y-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant={ev.tier==='DANGEROUS'?'destructive': ev.tier==='RISKY'?'warning': ev.tier==='SAFE'?'success':'outline'} className="rounded-full">{ev.tier}</Badge>
                      <span className="font-mono text-xs bg-zinc-100 px-2 py-1 rounded-full dark:bg-white/10">{ev.room} #{ev.seq}</span>
                      <span className="text-sm font-semibold">{ev.nick || ev.did || 'unknown'}</span>
                      <span className="text-xs text-muted-foreground">score {ev.score}</span>
                      {ev.link && <a href={ev.link} target="_blank" rel="noreferrer" className="ml-auto text-xs text-violet-600 hover:underline font-mono">external/r/{ev.room}</a>}
                    </div>
                    {ev.reason && <div className="text-sm leading-relaxed">reason: <span className="text-muted-foreground">{ev.reason}</span></div>}
                    {ev.text && <details className="text-xs"><summary className="cursor-pointer font-medium">untrusted remote message</summary><pre className="mt-2 rounded-xl bg-zinc-950 text-zinc-100 p-3 whitespace-pre-wrap break-words max-h-32 overflow-auto">{ev.text.slice(0,1000)}</pre></details>}
                    <div className="text-[11px] font-mono text-muted-foreground">{ev.evaluated_at?.slice(0,19)} · {ev.model}</div>
                  </CardContent>
                </Card>
              </motion.div>
            ))}
          </div>
          <div className="flex items-center justify-between pt-2">
            <div className="text-xs text-muted-foreground">{offset+1}–{Math.min(offset+limit, total)} / {total}</div>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" className="rounded-xl" disabled={offset===0} onClick={()=>setOffset(o=>Math.max(0,o-limit))}><ChevronLeft className="h-4 w-4" /> Previous</Button>
              <Button variant="outline" size="sm" className="rounded-xl" disabled={offset+limit>=total} onClick={()=>setOffset(o=>o+limit)}>Next <ChevronRight className="h-4 w-4" /></Button>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
