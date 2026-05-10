import React from 'react'

const tableStyle = {
  width: '100%', borderCollapse: 'collapse', fontSize: 14,
}
const thStyle = {
  textAlign: 'left', padding: '10px 12px', borderBottom: '1px solid #334155', color: '#94a3b8', fontWeight: 600,
}
const tdStyle = {
  padding: '10px 12px', borderBottom: '1px solid #1e293b',
}

export default function PositionTable({ positions }) {
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={tableStyle}>
        <thead>
          <tr>
            <th style={thStyle}>Ticker</th>
            <th style={thStyle}>Qty</th>
            <th style={thStyle}>Entry</th>
            <th style={thStyle}>SL</th>
            <th style={thStyle}>T1</th>
            <th style={thStyle}>T2</th>
            <th style={thStyle}>Status</th>
          </tr>
        </thead>
        <tbody>
          {positions.map(p => (
            <tr key={p.id || p.ticker}>
              <td style={tdStyle}>{p.ticker}</td>
              <td style={tdStyle}>{p.quantity}</td>
              <td style={tdStyle}>₹{Number(p.entry_price).toFixed(2)}</td>
              <td style={tdStyle}>₹{Number(p.stop_loss).toFixed(2)}</td>
              <td style={tdStyle}>₹{Number(p.target_1).toFixed(2)}</td>
              <td style={tdStyle}>₹{Number(p.target_2).toFixed(2)}</td>
              <td style={tdStyle}>
                <span style={{
                  fontSize: 11, fontWeight: 600, padding: '3px 8px', borderRadius: 12,
                  background: p.status === 'open' ? '#064e3b' : '#1e293b',
                  color: p.status === 'open' ? '#4ade80' : '#94a3b8',
                }}>
                  {p.status}
                </span>
              </td>
            </tr>
          ))}
          {positions.length === 0 && (
            <tr><td colSpan={7} style={{ ...tdStyle, color: '#64748b' }}>No open positions.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
