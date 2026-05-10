import React, { useEffect, useState } from 'react'
import PnLChart from '../components/PnLChart'

const cardStyle = {
  background: '#1e293b', borderRadius: 12, padding: 20, marginBottom: 16,
  border: '1px solid #334155',
}

export default function Performance() {
  const [metrics, setMetrics] = useState([])

  useEffect(() => {
    fetch('/api/performance')
      .then(r => r.json())
      .then(d => setMetrics(d.metrics || []))
      .catch(console.error)
  }, [])

  const latest = metrics[0] || {}

  return (
    <div>
      <h2>Performance</h2>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 16, marginBottom: 20 }}>
        <div style={cardStyle}>
          <div style={{ fontSize: 12, color: '#94a3b8' }}>Total Return</div>
          <div style={{ fontSize: 22, fontWeight: 700, color: (latest.daily_return_pct || 0) >= 0 ? '#4ade80' : '#f87171' }}>
            {(latest.daily_return_pct || 0).toFixed(2)}%
          </div>
        </div>
        <div style={cardStyle}>
          <div style={{ fontSize: 12, color: '#94a3b8' }}>Win Rate</div>
          <div style={{ fontSize: 22, fontWeight: 700 }}>{(latest.win_rate || 0).toFixed(1)}%</div>
        </div>
        <div style={cardStyle}>
          <div style={{ fontSize: 12, color: '#94a3b8' }}>Max Drawdown</div>
          <div style={{ fontSize: 22, fontWeight: 700, color: '#f87171' }}>
            {(latest.max_drawdown_pct || 0).toFixed(2)}%
          </div>
        </div>
        <div style={cardStyle}>
          <div style={{ fontSize: 12, color: '#94a3b8' }}>Total Trades</div>
          <div style={{ fontSize: 22, fontWeight: 700 }}>{latest.total_trades || 0}</div>
        </div>
      </div>

      <h3>Daily P&L</h3>
      <PnLChart metrics={metrics} />
    </div>
  )
}
