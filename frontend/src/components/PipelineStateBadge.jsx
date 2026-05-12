import React, { useState } from 'react'
import usePolling from '../hooks/usePolling'
import { getPipelineState, forceRerun } from '../api'

function fmtTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: true })
}

export default function PipelineStateBadge() {
  const { data, refresh } = usePolling(getPipelineState, 10000)
  const [busy, setBusy] = useState(false)
  const state = data?.state || '...'
  const since = data?.state_since
  const heartbeat = data?.last_heartbeat_at
  const isActive = state === 'precheck' || state === 'monitor' || state === 'waiting'
  const canRerun = state === 'idle' || state === 'waiting' || state === 'holiday'

  async function handleRerun() {
    if (!confirm('This will delete today\'s plans/reports and restart the full analysis. Continue?')) return
    setBusy(true)
    try {
      await forceRerun()
      refresh()
    } catch (e) {
      alert('Rerun failed: ' + (e.message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
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
        {since && (
          <span style={{ color: '#444', fontSize: 11 }}>since {fmtTime(since)}</span>
        )}
        {heartbeat && heartbeat !== since && (
          <span style={{ color: '#555', fontSize: 11 }}>live {fmtTime(heartbeat)}</span>
        )}
      </div>
      {canRerun && (
        <button
          onClick={handleRerun}
          disabled={busy}
          style={{
            background: 'transparent', color: '#666', border: '1px solid #222',
            padding: '3px 10px', borderRadius: 2, cursor: 'pointer', fontSize: 11,
          }}
        >{busy ? '...' : 'Force Rerun'}</button>
      )}
    </div>
  )
}
