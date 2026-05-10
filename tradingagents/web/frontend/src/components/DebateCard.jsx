import React from 'react'

export default function DebateCard({ debate }) {
  return (
    <div style={{
      background: '#1e293b', borderRadius: 12, padding: 20,
      border: '1px solid #334155',
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 12 }}>
        <div style={{ fontWeight: 700, fontSize: 16 }}>{debate.ticker}</div>
        <div style={{ fontSize: 12, color: '#94a3b8' }}>Round {debate.round_num}</div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        <div style={{
          background: '#0f172a', borderRadius: 8, padding: 12,
          borderLeft: '4px solid #4ade80',
        }}>
          <div style={{ fontWeight: 700, fontSize: 13, color: '#4ade80', marginBottom: 8 }}>🐂 BULL</div>
          <div style={{ fontSize: 13, lineHeight: 1.5, whiteSpace: 'pre-wrap' }}>{debate.bull_argument}</div>
        </div>
        <div style={{
          background: '#0f172a', borderRadius: 8, padding: 12,
          borderLeft: '4px solid #f87171',
        }}>
          <div style={{ fontWeight: 700, fontSize: 13, color: '#f87171', marginBottom: 8 }}>🐻 BEAR</div>
          <div style={{ fontSize: 13, lineHeight: 1.5, whiteSpace: 'pre-wrap' }}>{debate.bear_argument}</div>
        </div>
      </div>

      <div style={{
        marginTop: 12, padding: '10px 14px', background: '#0f172a', borderRadius: 8,
        fontSize: 13, fontWeight: 600, color: '#38bdf8',
      }}>
        Verdict: {debate.verdict} {debate.confidence ? `(${debate.confidence}/10)` : ''}
      </div>
    </div>
  )
}
