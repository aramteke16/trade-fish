import React, { useEffect, useState } from 'react'

const tableStyle = {
  width: '100%', borderCollapse: 'collapse', fontSize: 14,
}
const thStyle = {
  textAlign: 'left', padding: '10px 12px', borderBottom: '1px solid #334155', color: '#94a3b8', fontWeight: 600,
}
const tdStyle = {
  padding: '10px 12px', borderBottom: '1px solid #1e293b',
}

export default function History() {
  const [trades, setTrades] = useState([])

  useEffect(() => {
    fetch('/api/history')
      .then(r => r.json())
      .then(d => setTrades(d.trades || []))
      .catch(console.error)
  }, [])

  return (
    <div>
      <h2>Trade History</h2>
      <div style={{ overflowX: 'auto' }}>
        <table style={tableStyle}>
          <thead>
            <tr>
              <th style={thStyle}>Date</th>
              <th style={thStyle}>Ticker</th>
              <th style={thStyle}>Qty</th>
              <th style={thStyle}>Entry</th>
              <th style={thStyle}>Exit</th>
              <th style={thStyle}>Reason</th>
              <th style={thStyle}>P&L</th>
              <th style={thStyle}>P&L %</th>
            </tr>
          </thead>
          <tbody>
            {trades.map(t => (
              <tr key={t.id}>
                <td style={tdStyle}>{t.date}</td>
                <td style={tdStyle}>{t.ticker}</td>
                <td style={tdStyle}>{t.quantity}</td>
                <td style={tdStyle}>₹{Number(t.entry_price).toFixed(2)}</td>
                <td style={tdStyle}>{t.exit_price ? `₹${Number(t.exit_price).toFixed(2)}` : '-'}</td>
                <td style={tdStyle}>{t.exit_reason || 'Open'}</td>
                <td style={{ ...tdStyle, color: (t.pnl || 0) >= 0 ? '#4ade80' : '#f87171' }}>
                  {t.pnl ? `${t.pnl >= 0 ? '+' : ''}₹${Number(t.pnl).toFixed(0)}` : '-'}
                </td>
                <td style={{ ...tdStyle, color: (t.pnl_pct || 0) >= 0 ? '#4ade80' : '#f87171' }}>
                  {t.pnl_pct ? `${t.pnl_pct >= 0 ? '+' : ''}${Number(t.pnl_pct).toFixed(2)}%` : '-'}
                </td>
              </tr>
            ))}
            {trades.length === 0 && (
              <tr><td colSpan={8} style={{ ...tdStyle, color: '#64748b' }}>No trades recorded yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
