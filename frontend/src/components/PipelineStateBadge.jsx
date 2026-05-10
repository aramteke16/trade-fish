import React from 'react'
import usePolling from '../hooks/usePolling'
import { getPipelineState } from '../api'

function fmtTime(iso) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true })
  } catch { return '' }
}

export default function PipelineStateBadge() {
  const { data } = usePolling(getPipelineState, 10000)
  const state = data?.state || '...'
  const since = data?.state_since
  const history = (data?.history || []).slice(0, 5)
  const isActive = state === 'precheck' || state === 'monitor' || state === 'waiting'

  return (
    <div>
      <div style={{
        display: 'inline-flex', alignItems: 'center', gap: 8,
        padding: '4px 10px', borderRadius: 2,
        border: '1px solid #222',
      }}>
        <span style={{
          width: 6, height: 6, borderRadius: '50%',
          background: isActive ? '#e5e5e5' : '#333',
        }} />
        <span style={{ fontWeight: 500, fontSize: 12 }}>{state}</span>
        {since && <span style={{ color: '#444', fontSize: 11 }}>{fmtTime(since)}</span>}
      </div>

      {history.length > 0 && (
        <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
          {history.map((h, i) => (
            <React.Fragment key={h.id || i}>
              <span style={{ fontSize: 10, color: '#555' }}>
                {h.to_state} <span style={{ color: '#333' }}>{fmtTime(h.at)}</span>
              </span>
              {i < history.length - 1 && <span style={{ color: '#333', fontSize: 10 }}>{'←'}</span>}
            </React.Fragment>
          ))}
        </div>
      )}
    </div>
  )
}
