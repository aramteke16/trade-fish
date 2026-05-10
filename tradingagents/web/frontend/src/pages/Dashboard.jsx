import React, { useEffect, useState } from 'react'
import TradeCard from '../components/TradeCard'
import PositionTable from '../components/PositionTable'

const cardStyle = {
  background: '#1e293b', borderRadius: 12, padding: 20, marginBottom: 20,
  border: '1px solid #334155',
}

export default function Dashboard() {
  const [data, setData] = useState(null)
  const [wsStatus, setWsStatus] = useState('connecting')

  useEffect(() => {
    fetch('/api/today')
      .then(r => r.json())
      .then(setData)
      .catch(console.error)

    const ws = new WebSocket(`ws://${window.location.host}/ws/live`)
    ws.onopen = () => setWsStatus('connected')
    ws.onclose = () => setWsStatus('disconnected')
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.type === 'price_update') {
        // Could update live prices here
      }
    }
    return () => ws.close()
  }, [])

  if (!data) return <div style={{ padding: 40 }}>Loading dashboard...</div>

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <h2 style={{ margin: 0 }}>Dashboard</h2>
        <div style={{ fontSize: 12, color: wsStatus === 'connected' ? '#4ade80' : '#f87171' }}>
          ● {wsStatus}
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 16, marginBottom: 20 }}>
        <div style={cardStyle}>
          <div style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4 }}>Capital</div>
          <div style={{ fontSize: 24, fontWeight: 700 }}>₹{Number(data.capital || 20000).toLocaleString('en-IN', { maximumFractionDigits: 0 })}</div>
        </div>
        <div style={cardStyle}>
          <div style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4 }}>Daily P&L</div>
          <div style={{ fontSize: 24, fontWeight: 700, color: (data.daily_pnl || 0) >= 0 ? '#4ade80' : '#f87171' }}>
            {(data.daily_pnl || 0) >= 0 ? '+' : ''}₹{Number(data.daily_pnl || 0).toFixed(0)}
          </div>
        </div>
        <div style={cardStyle}>
          <div style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4 }}>Open Positions</div>
          <div style={{ fontSize: 24, fontWeight: 700 }}>{(data.open_positions || []).length}</div>
        </div>
        <div style={cardStyle}>
          <div style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4 }}>Trade Plans</div>
          <div style={{ fontSize: 24, fontWeight: 700 }}>{(data.trade_plans || []).length}</div>
        </div>
      </div>

      <h3>Today's Trade Plans</h3>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 16 }}>
        {(data.trade_plans || []).map(plan => (
          <TradeCard key={plan.id} plan={plan} />
        ))}
        {(!data.trade_plans || data.trade_plans.length === 0) && (
          <div style={{ color: '#64748b' }}>No trade plans for today yet.</div>
        )}
      </div>

      <h3 style={{ marginTop: 24 }}>Open Positions</h3>
      <PositionTable positions={data.open_positions || []} />
    </div>
  )
}
