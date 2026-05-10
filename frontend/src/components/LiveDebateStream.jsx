import React, { useReducer } from 'react'
import useWebSocket from '../hooks/useWebSocket'

function reducer(state, msg) {
  if (!msg) return state
  const k = (t, s) => `${t || '_'}::${s || '_'}`
  switch (msg.type) {
    case 'agent_start': {
      const key = k(msg.ticker, msg.stage)
      return { ...state, [key]: { ticker: msg.ticker, stage: msg.stage, status: 'running', text: '', tokens: null } }
    }
    case 'agent_chunk': {
      const matchingKey = Object.keys(state).reverse().find(
        (k2) => state[k2].ticker === msg.ticker && state[k2].status === 'running'
      )
      if (!matchingKey) return state
      return { ...state, [matchingKey]: { ...state[matchingKey], text: state[matchingKey].text + msg.delta } }
    }
    case 'agent_done': {
      const key = k(msg.ticker, msg.stage)
      const prev = state[key] || { ticker: msg.ticker, stage: msg.stage, text: '' }
      const text = prev.text || msg.output || ''
      return { ...state, [key]: { ...prev, status: 'done', text } }
    }
    case 'llm_end': {
      const matchingKey = Object.keys(state).reverse().find(
        (k2) => state[k2].ticker === msg.ticker && state[k2].status === 'running'
      )
      if (!matchingKey) return state
      const prev = state[matchingKey]
      return {
        ...state,
        [matchingKey]: {
          ...prev,
          tokens: { in: (prev.tokens?.in || 0) + msg.tokens_in, out: (prev.tokens?.out || 0) + msg.tokens_out },
        },
      }
    }
    case 'reset': return {}
    default: return state
  }
}

export default function LiveDebateStream() {
  const [state, dispatch] = useReducer(reducer, {})
  const { connected } = useWebSocket('/ws/live', {
    onMessage: (msg) => dispatch(msg),
  })
  const cards = Object.entries(state).sort()

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
        <div style={{ fontSize: 11, color: '#555', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
          Live Agent Stream
        </div>
        <div style={{ fontSize: 11, color: connected ? '#666' : '#444' }}>
          {connected ? 'connected' : 'reconnecting...'}
        </div>
      </div>
      {cards.length === 0 && (
        <div style={{ color: '#333', padding: 20, textAlign: 'center', border: '1px dashed #1a1a1a', borderRadius: 2, fontSize: 13 }}>
          Waiting for next agent run
        </div>
      )}
      {cards.map(([key, c]) => (
        <div key={key} style={{
          border: `1px solid ${c.status === 'running' ? '#333' : '#1a1a1a'}`,
          borderRadius: 2, padding: 12, marginBottom: 8,
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{
                display: 'inline-block', padding: '1px 6px', borderRadius: 2, fontSize: 10,
                background: c.status === 'running' ? '#e5e5e5' : '#1a1a1a',
                color: c.status === 'running' ? '#000' : '#666',
                fontWeight: 600, textTransform: 'uppercase',
              }}>{c.status}</span>
              <span style={{ fontWeight: 500, fontSize: 13 }}>{c.stage}</span>
              <span style={{ color: '#444', fontSize: 12 }}>{c.ticker}</span>
            </div>
            {c.tokens && (
              <div style={{ color: '#333', fontSize: 10, fontVariantNumeric: 'tabular-nums' }}>
                {c.tokens.in}in / {c.tokens.out}out
              </div>
            )}
          </div>
          {c.text && (
            <pre style={{
              marginTop: 8, color: '#888', fontSize: 11, fontFamily: 'monospace',
              whiteSpace: 'pre-wrap', maxHeight: 180, overflow: 'auto',
              padding: 10, background: '#0a0a0a', borderRadius: 2, border: '1px solid #111',
            }}>{c.text}</pre>
          )}
        </div>
      ))}
    </div>
  )
}
