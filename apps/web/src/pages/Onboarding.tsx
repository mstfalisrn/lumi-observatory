import { useState } from 'react'
import { api, errMsg } from '../api'
import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'

type Step = 1 | 2 | 3

type Props = {
  onDone?: () => void
}

export default function Onboarding({ onDone }: Props) {
  const [step, setStep] = useState<Step>(1)

  // Step 1 - admin password
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [pwBusy, setPwBusy] = useState(false)
  const [pwMsg, setPwMsg] = useState('')
  const [pwErr, setPwErr] = useState('')

  // Step 2 - LLM
  const [provider, setProvider] = useState<'mock' | 'openai' | 'openrouter' | 'ollama'>('mock')
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [testBusy, setTestBusy] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null)

  // Step 3 - Telegram
  const [tgToken, setTgToken] = useState('')
  const [tgDone, setTgDone] = useState(false)

  async function handleStep1() {
    if (!email.trim() || !password) {
      setPwErr('email and password required')
      return
    }
    setPwBusy(true)
    setPwErr('')
    setPwMsg('')
    try {
      // verify credentials work via login
      await api('/v1/auth/login', {
        method: 'POST',
        body: JSON.stringify({ email: email.trim(), password }),
      })
      setPwMsg('✓ signed in — admin ready')
      setTimeout(() => setStep(2), 600)
    } catch (e) {
      setPwErr(errMsg(e))
    } finally {
      setPwBusy(false)
    }
  }

  async function handleTestLLM() {
    setTestBusy(true)
    setTestResult(null)
    const provMap: Record<string, string> = {
      mock: 'mock',
      openai: 'openai',
      openrouter: 'openai',
      ollama: 'openai',
    }
    const p = provMap[provider] || provider
    // defaults per provider if empty
    let url = baseUrl.trim()
    let mdl = model.trim()
    if (!url) {
      if (provider === 'openai') url = 'https://api.openai.com/v1'
      else if (provider === 'openrouter') url = 'https://openrouter.ai/api/v1'
      else if (provider === 'ollama') url = 'http://localhost:11434/v1'
    }
    if (!mdl && provider === 'ollama') mdl = 'llama3.1'
    if (!mdl && provider === 'openai') mdl = 'gpt-4o-mini'
    try {
      const r = await api<{ ok: boolean; provider: string; detail?: string }>('/v1/settings/llm/test', {
        method: 'POST',
        body: JSON.stringify({ provider: p, base_url: url, model: mdl, api_key: apiKey }),
      })
      if (r.ok) setTestResult({ ok: true, msg: `✓ connection OK (${r.provider})` })
      else setTestResult({ ok: false, msg: r.detail || 'test failed' })
    } catch (e) {
      setTestResult({ ok: false, msg: errMsg(e) })
    } finally {
      setTestBusy(false)
    }
  }

  function handleStep3Done() {
    setTgDone(true)
    onDone?.()
  }

  return (
    <div className="mx-auto max-w-[640px] space-y-6 p-4 md:p-6">
      <div className="text-center space-y-2">
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl bg-gradient-to-br from-violet-600 to-indigo-600 text-white shadow-[0_8px_24px_rgba(99,102,241,0.3)]">
          <span className="text-lg font-bold">R</span>
        </div>
        <h1 className="text-2xl font-bold tracking-tight bg-gradient-to-r from-violet-600 to-indigo-600 bg-clip-text text-transparent">LUMI Setup Wizard</h1>
        <p className="mt-1 text-sm text-muted-foreground">Get started in 3 steps — instant with mock, fully autonomous with an API key.</p>
      </div>

      <div className="flex items-center justify-center gap-2">
        {[1, 2, 3].map((s) => (
          <div key={s} className="flex items-center gap-2">
            <div
              className={
                'flex h-9 w-9 items-center justify-center rounded-xl text-sm font-bold transition-all duration-300 ' +
                (step === s
                  ? 'bg-gradient-to-br from-violet-600 to-indigo-600 text-white shadow-[0_4px_14px_rgba(99,102,241,0.3)] scale-105'
                  : step > s
                    ? 'bg-gradient-to-br from-emerald-500 to-teal-600 text-white shadow-sm'
                    : 'bg-white border border-zinc-200 text-muted-foreground dark:bg-white/5 dark:border-white/10 dark:text-zinc-400')
              }
            >
              {step > s ? '✓' : s}
            </div>
            {s < 3 && <div className={'h-0.5 w-10 rounded-full transition-colors ' + (step > s ? 'bg-emerald-500' : 'bg-zinc-200 dark:bg-white/10')} />}
          </div>
        ))}
      </div>

      {step === 1 && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">Step 1 — Admin account <Badge variant="secondary">required</Badge></CardTitle>
            <CardDescription>Sign in with your local admin email and password. On first setup it is created via .env ADMIN_PASSWORD_HASH.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <Input placeholder="admin email (e.g. admin@example.com)" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" />
            <Input placeholder="password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
            {pwErr && <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">⚠ {pwErr}</div>}
            {pwMsg && <div className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-200">{pwMsg}</div>}
          </CardContent>
          <CardFooter className="flex justify-between">
            <span className="text-xs text-muted-foreground">Verified via POST /api/v1/auth/login</span>
            <Button onClick={handleStep1} disabled={pwBusy || !email.trim() || !password}>
              {pwBusy ? 'verifying…' : 'Verify & continue →'}
            </Button>
          </CardFooter>
        </Card>
      )}

      {step === 2 && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">Step 2 — LLM provider <Badge variant="outline">Mock free</Badge></CardTitle>
            <CardDescription>Mock needs no key. For OpenAI / OpenRouter / Ollama, enter base_url, model and api_key.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {(['mock', 'openai', 'openrouter', 'ollama'] as const).map((p) => (
                <button
                  key={p}
                  onClick={() => setProvider(p)}
                  className={
                    'rounded-lg border px-3 py-2.5 text-sm font-medium capitalize transition-colors ' +
                    (provider === p ? 'border-primary bg-primary text-primary-foreground shadow-sm' : 'border-input bg-background hover:bg-accent')
                  }
                >
                  {p === 'mock' ? 'Mock' : p === 'openai' ? 'OpenAI' : p === 'openrouter' ? 'OpenRouter' : 'Ollama'}
                  {p === 'mock' && <span className="ml-1 text-xs opacity-80">(free)</span>}
                </button>
              ))}
            </div>

            {provider !== 'mock' && (
              <div className="space-y-3">
                <Input placeholder={provider === 'ollama' ? 'base_url (e.g. http://localhost:11434/v1)' : 'base_url (e.g. https://api.openai.com/v1)'} value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
                <Input placeholder={provider === 'ollama' ? 'model (e.g. llama3.1)' : 'model (e.g. gpt-4o-mini)'} value={model} onChange={(e) => setModel(e.target.value)} />
                <Input placeholder="api_key (secret — stored on server, never shown in UI)" type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
                <div className="flex items-center gap-2">
                  <Button variant="secondary" onClick={handleTestLLM} disabled={testBusy}>
                    {testBusy ? 'testing…' : '🔌 Test connection'}
                  </Button>
                  {testResult && (
                    <Badge variant={testResult.ok ? 'default' : 'destructive'} className={testResult.ok ? 'bg-emerald-600' : ''}>
                      {testResult.msg}
                    </Badge>
                  )}
                </div>
                {!testResult && <p className="text-xs text-muted-foreground">Test → POST /api/v1/settings/llm/test (auth required)</p>}
              </div>
            )}

            {provider === 'mock' && (
              <div className="rounded-md border border-violet-200 bg-violet-50 px-3 py-2 text-sm text-violet-800 dark:border-violet-900 dark:bg-violet-950/30 dark:text-violet-200">
                Mock selected — no key needed. Scheduler and worker run the full loop with the mock provider.
              </div>
            )}
          </CardContent>
          <CardFooter className="flex justify-between">
            <Button variant="ghost" onClick={() => setStep(1)}>← Back</Button>
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => setStep(3)}>Skip →</Button>
              <Button onClick={() => setStep(3)}>Continue →</Button>
            </div>
          </CardFooter>
        </Card>
      )}

      {step === 3 && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">Step 3 — Telegram <Badge variant="outline">optional</Badge></CardTitle>
            <CardDescription>Enter a bot token if you have one, otherwise skip. The token is stored in .env TELEGRAM_BOT_TOKEN.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <Input placeholder="Telegram bot token (optional, e.g. 123456:ABC...)" type="password" value={tgToken} onChange={(e) => setTgToken(e.target.value)} />
            <p className="text-xs text-muted-foreground">Leave empty to keep Telegram disabled — you can add it later in Settings.</p>
            {tgDone && <div className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700">✓ setup complete</div>}
          </CardContent>
          <CardFooter className="flex justify-between">
            <Button variant="ghost" onClick={() => setStep(2)}>← Back</Button>
            <div className="flex gap-2">
              <Button variant="outline" onClick={handleStep3Done}>Skip and finish</Button>
              <Button onClick={handleStep3Done}>Finish ✓</Button>
            </div>
          </CardFooter>
        </Card>
      )}

      <div className="flex justify-center gap-1 pt-2">
        {[1, 2, 3].map((s) => (
          <span key={s} className={'h-1.5 rounded-full transition-all ' + (step === s ? 'w-8 bg-primary' : 'w-1.5 bg-border')} />
        ))}
      </div>
    </div>
  )
}
