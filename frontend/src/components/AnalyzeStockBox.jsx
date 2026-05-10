import React, { useState, useReducer, useEffect, useRef } from 'react'
import { startAnalyze, getAnalysis, getAnalysisReport } from '../api'
import useWebSocket from '../hooks/useWebSocket'

function agentReducer(state, msg) {
  if (!msg) return state
  if (msg.type === 'reset') return {}
  const k = (s) => `${msg.ticker || '_'}::${s || '_'}`
  switch (msg.type) {
    case 'agent_start': {
      const key = k(msg.stage)
      return { ...state, [key]: { stage: msg.stage, status: 'running', text: '', tokens: null } }
    }
    case 'agent_chunk': {
      const match = Object.keys(state).reverse().find(
        (k2) => state[k2].status === 'running'
      )
      if (!match) return state
      return { ...state, [match]: { ...state[match], text: state[match].text + msg.delta } }
    }
    case 'agent_done': {
      const key = k(msg.stage)
      const prev = state[key] || { stage: msg.stage, text: '' }
      const text = prev.text || msg.output || ''
      return { ...state, [key]: { ...prev, status: 'done', text } }
    }
    case 'llm_end': {
      const match = Object.keys(state).reverse().find(
        (k2) => state[k2].status === 'running'
      )
      if (!match) return state
      const prev = state[match]
      return {
        ...state,
        [match]: {
          ...prev,
          tokens: { in: (prev.tokens?.in || 0) + msg.tokens_in, out: (prev.tokens?.out || 0) + msg.tokens_out },
        },
      }
    }
    default: return state
  }
}

function todayISO() {
  return new Date().toISOString().slice(0, 10)
}

export default function AnalyzeStockBox({ onSubmitted }) {
  const [ticker, setTicker] = useState('')
  const [date, setDate] = useState(todayISO())
  const [running, setRunning] = useState(null)
  const [activeTicker, setActiveTicker] = useState(null)
  const [agents, dispatch] = useReducer(agentReducer, {})
  const [report, setReport] = useState(null)
  const [err, setErr] = useState(null)
  const streamRef = useRef(null)

  const { connected } = useWebSocket('/ws/live', {
    onMessage: (msg) => {
      if (!activeTicker) return
      if (msg.ticker !== activeTicker) return
      dispatch(msg)
    },
  })

  useEffect(() => {
    if (streamRef.current) {
      streamRef.current.scrollTop = streamRef.current.scrollHeight
    }
  }, [agents])

  async function submit() {
    setErr(null)
    setReport(null)
    dispatch({ type: 'reset' })
    if (!ticker.trim()) return
    const t = ticker.trim().toUpperCase()
    try {
      const r = await startAnalyze(t, date)
      const initial = { id: r.analysis_id, status: 'pending', ticker: t }
      setRunning(initial)
      setActiveTicker(t)
      pollUntilDone(r.analysis_id)
      onSubmitted?.()
      setTicker('')
    } catch (e) {
      setErr(String(e.message || e))
    }
  }

  async function pollUntilDone(id) {
    const tick = async () => {
      try {
        const row = await getAnalysis(id)
        setRunning(row)
        if (row.status === 'done') {
          try {
            const rpt = await getAnalysisReport(id)
            setReport(rpt.content)
          } catch { /* report might not be ready */ }
          return
        }
        if (row.status === 'error') return
      } catch { /* retry */ }
      setTimeout(tick, 3000)
    }
    setTimeout(tick, 3000)
  }

  const cards = Object.entries(agents).sort()
  const isActive = running && (running.status === 'pending' || running.status === 'running')

  return (
    <div>
      <div style={{ fontSize: 12, color: '#555', marginBottom: 8 }}>
        Run full multi-agent debate on any NSE ticker (~3-5 min)
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <input
          style={{
            flex: 1, minWidth: 140, padding: '8px 10px', fontSize: 13, borderRadius: 2,
            border: '1px solid #222', background: '#000', color: '#e5e5e5', outline: 'none',
          }}
          placeholder="e.g. RELIANCE.NS"
          value={ticker}
          onChange={(e) => setTicker(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && submit()}
          disabled={isActive}
        />
        <input
          type="date"
          style={{
            padding: '8px 10px', fontSize: 13, borderRadius: 2,
            border: '1px solid #222', background: '#000', color: '#e5e5e5', outline: 'none',
            colorScheme: 'dark',
          }}
          value={date}
          onChange={(e) => setDate(e.target.value)}
          disabled={isActive}
        />
        <button
          onClick={submit}
          disabled={isActive}
          style={{
            background: isActive ? '#1a1a1a' : '#fff',
            color: isActive ? '#555' : '#000',
            border: 'none',
            padding: '8px 16px', fontWeight: 600, borderRadius: 2,
            cursor: isActive ? 'default' : 'pointer', fontSize: 13,
          }}
        >{isActive ? 'Running...' : 'Analyze'}</button>
      </div>
      {err && <div style={{ color: '#888', fontSize: 12, marginTop: 8 }}>{err}</div>}

      {running && (
        <div style={{ marginTop: 12 }}>
          <div style={{
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            padding: '8px 0', borderBottom: '1px solid #1a1a1a', marginBottom: 8,
          }}>
            <div style={{ fontSize: 13 }}>
              <span style={{ fontWeight: 600 }}>{running.ticker || activeTicker}</span>
              <span style={{ color: '#444', marginLeft: 8 }}>#{running.id}</span>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{
                fontSize: 11, fontWeight: 500, textTransform: 'uppercase',
                color: running.status === 'done' ? '#999' : running.status === 'error' ? '#666' : '#e5e5e5',
              }}>{running.status}</span>
              {isActive && connected && (
                <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#e5e5e5', display: 'inline-block' }} />
              )}
            </div>
          </div>

          {/* Live agent stream */}
          {cards.length > 0 && (
            <div ref={streamRef} style={{
              maxHeight: 400, overflowY: 'auto', marginBottom: 8,
              border: '1px solid #1a1a1a', borderRadius: 2,
            }}>
              {cards.map(([key, c]) => (
                <div key={key} style={{
                  padding: '8px 10px',
                  borderBottom: '1px solid #111',
                }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={{
                        display: 'inline-block', padding: '1px 5px', borderRadius: 2, fontSize: 9,
                        background: c.status === 'running' ? '#e5e5e5' : '#1a1a1a',
                        color: c.status === 'running' ? '#000' : '#666',
                        fontWeight: 600, textTransform: 'uppercase',
                      }}>{c.status === 'running' ? 'LIVE' : 'DONE'}</span>
                      <span style={{ fontWeight: 500, fontSize: 12 }}>{c.stage}</span>
                    </div>
                    {c.tokens && (
                      <span style={{ color: '#333', fontSize: 10, fontVariantNumeric: 'tabular-nums' }}>
                        {c.tokens.in}in / {c.tokens.out}out
                      </span>
                    )}
                  </div>
                  {c.text && (
                    <pre style={{
                      margin: '6px 0 0', color: '#777', fontSize: 11, fontFamily: 'monospace',
                      whiteSpace: 'pre-wrap', maxHeight: 140, overflow: 'auto',
                      padding: 8, background: '#0a0a0a', borderRadius: 2, border: '1px solid #111',
                    }}>{c.text}</pre>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Final report */}
          {running.status === 'done' && report && (
            <details style={{ marginTop: 4 }}>
              <summary style={{ cursor: 'pointer', fontSize: 12, fontWeight: 500, color: '#888', padding: '6px 0' }}>
                View full report
              </summary>
              <pre style={{
                margin: '8px 0 0', color: '#999', fontSize: 11, fontFamily: 'monospace',
                whiteSpace: 'pre-wrap', maxHeight: 500, overflow: 'auto',
                padding: 12, background: '#0a0a0a', borderRadius: 2, border: '1px solid #1a1a1a',
                lineHeight: 1.5,
              }}>{report}</pre>
            </details>
          )}

          {running.status === 'done' && running.summary && !report && (
            <div style={{ fontSize: 12, color: '#777', marginTop: 4 }}>{running.summary}</div>
          )}

          {running.status === 'error' && (
            <div style={{ color: '#888', fontSize: 12, marginTop: 4 }}>{running.error}</div>
          )}
        </div>
      )}
    </div>
  )
}
