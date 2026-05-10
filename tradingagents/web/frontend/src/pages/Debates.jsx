import React, { useEffect, useState } from 'react'
import DebateCard from '../components/DebateCard'

export default function Debates() {
  const [debates, setDebates] = useState([])
  const [tickers, setTickers] = useState([])
  const [selectedTicker, setSelectedTicker] = useState('')

  useEffect(() => {
    fetch('/api/debates')
      .then(r => r.json())
      .then(d => {
        setDebates(d.debates || [])
        const ts = [...new Set((d.debates || []).map(x => x.ticker))]
        setTickers(ts)
      })
      .catch(console.error)
  }, [])

  const filtered = selectedTicker
    ? debates.filter(d => d.ticker === selectedTicker)
    : debates

  return (
    <div>
      <h2>Agent Debates</h2>
      <div style={{ marginBottom: 16 }}>
        <select
          value={selectedTicker}
          onChange={e => setSelectedTicker(e.target.value)}
          style={{ padding: '8px 12px', borderRadius: 6, background: '#1e293b', color: '#e2e8f0', border: '1px solid #334155' }}
        >
          <option value="">All Tickers</option>
          {tickers.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        {filtered.map((d, i) => (
          <DebateCard key={i} debate={d} />
        ))}
        {filtered.length === 0 && <div style={{ color: '#64748b' }}>No debates recorded yet.</div>}
      </div>
    </div>
  )
}
