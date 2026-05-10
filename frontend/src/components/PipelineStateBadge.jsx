import React from 'react'
import usePolling from '../hooks/usePolling'
import { getPipelineState } from '../api'

export default function PipelineStateBadge() {
  const { data } = usePolling(getPipelineState, 10000)
  const state = data?.state || '...'
  const since = data?.state_since
  const isActive = state === 'precheck' || state === 'monitor' || state === 'waiting'
  return (
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
      {since && <span style={{ color: '#444', fontSize: 11 }}>{since.slice(11, 16)}</span>}
    </div>
  )
}
