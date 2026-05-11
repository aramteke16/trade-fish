// Centralized fetch wrappers. All endpoints live under /api on the same
// origin as the SPA (FastAPI serves both). Throws on non-2xx so callers
// can bubble errors into UI state.

async function req(path, opts = {}) {
  const r = await fetch(`/api${path}`, {
    headers: { 'content-type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!r.ok) {
    const text = await r.text().catch(() => '')
    throw new Error(`${r.status} ${r.statusText}: ${text}`)
  }
  return r.json()
}

// Dashboard / global
export const getToday = (date) =>
  req(`/today${date ? `?date=${date}` : ''}`)
export const getGlobalSummary = () => req('/global-summary')
export const getLossAttribution = (days = 30) =>
  req(`/history/loss-attribution?days=${days}`)
export const getPerformance = () => req('/performance')

// Positions
export const getPositions = () => req('/positions')
export const exitPosition = (id, body = {}) =>
  req(`/positions/${id}/exit`, { method: 'POST', body: JSON.stringify(body) })

// Trades
export const excludeFromFeedback = (id, exclude = true) =>
  req(`/trades/${id}/exclude-from-feedback`, {
    method: 'POST',
    body: JSON.stringify({ exclude }),
  })

// Debates / agent reports
export const getDebates = (date, ticker) => {
  const q = new URLSearchParams()
  if (date) q.set('date', date)
  if (ticker) q.set('ticker', ticker)
  return req(`/debates${q.toString() ? `?${q}` : ''}`)
}
export const getTickerDebates = (ticker, date) =>
  req(`/debates/${ticker}${date ? `?date=${date}` : ''}`)

// History
export const getHistory = () => req('/history')

// Config
export const getConfig = () => req('/config')
export const patchConfig = (key, value) =>
  req(`/config/key/${key}`, { method: 'PATCH', body: JSON.stringify({ value }) })
export const resetConfig = () => req('/config/reset', { method: 'POST' })
export const getConfigHistory = (limit = 50) => req(`/config/history?limit=${limit}`)

// Pipeline
export const getPipelineState = () => req('/pipeline/state')
export const transitionPipeline = (to, note) =>
  req('/pipeline/transition', { method: 'POST', body: JSON.stringify({ to, note }) })
export const runStageNow = (stage) =>
  req(`/pipeline/run-now/${stage}`, { method: 'POST' })
export const forceRerun = () =>
  req('/pipeline/force-rerun', { method: 'POST' })

// Admin
export const resetCapital = (capital = null) =>
  req('/admin/reset-capital', {
    method: 'POST',
    body: JSON.stringify({ capital }),
  })

// Analyze (on-demand)
export const startAnalyze = (ticker, date) =>
  req('/analyze', { method: 'POST', body: JSON.stringify({ ticker, date }) })
export const getAnalyses = () => req('/analyze')
export const getAnalysis = (id) => req(`/analyze/${id}`)
export const getAnalysisReport = (id) => req(`/analyze/${id}/report`)

// Stats
export const getTokenStats = (date) =>
  req(`/stats/tokens${date ? `?date=${date}` : ''}`)

// Files / reports
export const getDayFiles = (date) => req(`/files?date=${date}`)
export const getFileContent = (path) =>
  req(`/files/content?path=${encodeURIComponent(path)}`)
export const downloadFileUrl = (path) =>
  `/api/files/download?path=${encodeURIComponent(path)}`

// Helpers
export const fmtINR = (n) => {
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  const sign = n < 0 ? '-' : ''
  return `${sign}₹${Math.abs(n).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`
}
export const fmtPct = (n, digits = 2) => {
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  return `${n >= 0 ? '+' : ''}${n.toFixed(digits)}%`
}
