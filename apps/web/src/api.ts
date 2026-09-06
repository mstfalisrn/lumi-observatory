// LUMI web — API client. Auth token is never written to localStorage (sessionStorage is used).
// Last-Event-ID can be stored in localStorage (not a secret).
const API = (import.meta.env.VITE_API_BASE ?? '/api') as string
const SSE_URL = (import.meta.env.VITE_SSE_BASE ?? '/api/v1/events/stream') as string

// ---- session token (sessionStorage; auth token is NOT written to localStorage) ----
const TOKEN_KEY = 'lumi:session'

export function getToken(): string {
  try { return sessionStorage.getItem(TOKEN_KEY) || '' } catch { return '' }
}
export function setToken(t: string): void {
  try { t ? sessionStorage.setItem(TOKEN_KEY, t) : sessionStorage.removeItem(TOKEN_KEY) } catch {}
}

let _onUnauthorized: (() => void) | null = null
export function setOnUnauthorized(fn: (() => void) | null): void { _onUnauthorized = fn }

export async function api<T = any>(path: string, opts?: RequestInit): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((opts?.headers as Record<string, string> | undefined) || {}),
  }
  if (token) headers['Authorization'] = `Bearer ${token}`
  const res = await fetch(`${API}${path}`, { ...opts, headers })
  if (res.status === 401) _onUnauthorized?.()
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`${res.status}: ${body.slice(0, 240)}`)
  }
  if (res.status === 204) return undefined as any
  const ct = res.headers.get('content-type') || ''
  if (ct.includes('application/json')) return res.json() as Promise<T>
  return res.json() as Promise<T>
}

export const apiBase = API
export const sseUrl = SSE_URL

// ---- SSE with Last-Event-ID (fetch streaming + EventSource fallback) ----
export type SSEState = 'connecting' | 'open' | 'closed' | 'error'
const LS_KEY = 'lumi:lastEventId'

function getLastId() { try { return localStorage.getItem(LS_KEY) || '' } catch { return '' } }
function setLastId(v: string) { try { if (v) localStorage.setItem(LS_KEY, v) } catch {} }

export function openSSE(
  onEvent: (e: any, rawId: string) => void,
  onState?: (s: SSEState) => void,
): () => void {
  let closed = false
  let abort: AbortController | null = null
  let retryMs = 1000
  let lastId = getLastId()

  onState?.('connecting')

  async function connect() {
    if (closed) return
    abort = new AbortController()
    // Build URL with query fallback for Last-Event-ID (EventSource polyfill compat)
    const url = lastId ? `${SSE_URL}${SSE_URL.includes('?') ? '&' : '?'}lastEventId=${encodeURIComponent(lastId)}` : SSE_URL
    onState?.('connecting')
    try {
      const token = getToken()
      const headers: Record<string, string> = { 'Accept': 'text/event-stream' }
      if (token) headers['Authorization'] = `Bearer ${token}`
      if (lastId) headers['Last-Event-ID'] = lastId
      const res = await fetch(url, { headers, signal: abort.signal })
      if (!res.ok || !res.body) throw new Error(`SSE ${res.status}`)
      onState?.('open')
      retryMs = 1000
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      let curId = ''
      let curData = ''
      while (!closed) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        // SSE frames delimited by \n\n
        let idx: number
        while ((idx = buf.indexOf('\n\n')) !== -1) {
          const frame = buf.slice(0, idx)
          buf = buf.slice(idx + 2)
          // parse lines
          const lines = frame.split('\n')
          for (const line of lines) {
            if (line.startsWith('id:')) curId = line.slice(3).trim()
            else if (line.startsWith('data:')) curData += (curData ? '\n' : '') + line.slice(5).trimStart()
            else if (line.startsWith('retry:')) {
              const v = parseInt(line.slice(6).trim(), 10)
              if (!isNaN(v)) retryMs = Math.max(1000, v)
            } else if (line.startsWith(':')) { /* keepalive/comment */ }
          }
          // dispatch when frame ends and has data
          if (frame.includes('data:')) {
            if (curId) { lastId = curId; setLastId(curId) }
            const idToSend = curId
            const dataToSend = curData
            curId = ''; curData = ''
            if (dataToSend) {
              try { onEvent(JSON.parse(dataToSend), idToSend) } catch { onEvent({ raw: dataToSend }, idToSend) }
            }
          } else {
            if (frame.trim().startsWith(':')) { curId=''; curData='' }
          }
        }
      }
      throw new Error('SSE closed')
    } catch (e: any) {
      if (closed || abort?.signal.aborted) { onState?.('closed'); return }
      onState?.('error')
      const wait = Math.min(retryMs, 15000) + Math.random() * 500
      retryMs = Math.min(retryMs * 1.7, 15000)
      await new Promise(r => setTimeout(r, wait))
      if (!closed) connect()
    }
  }
  connect()
  return () => { closed = true; onState?.('closed'); try { abort?.abort() } catch {} }
}

// simple fetch hook helper types
export function errMsg(e: unknown): string { return e instanceof Error ? e.message : String(e) }
