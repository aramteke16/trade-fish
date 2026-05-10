import React from 'react'
import { fmtINR, fmtPct } from '../api'

function Card({ label, value, sub, negative }) {
  return (
    <div style={{
      flex: 1,
      minWidth: 180,
      padding: '16px 20px',
      border: '1px solid #1a1a1a',
      borderRadius: 2,
    }}>
      <div style={{ fontSize: 11, color: '#666', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
        {label}
      </div>
      <div style={{
        fontSize: 22,
        fontWeight: 600,
        marginTop: 6,
        fontVariantNumeric: 'tabular-nums',
        color: negative === true ? '#888' : '#e5e5e5',
      }}>
        {value}
      </div>
      {sub && <div style={{ fontSize: 12, color: '#555', marginTop: 4 }}>{sub}</div>}
    </div>
  )
}

export default function SummaryCards({ summary }) {
  if (!summary) return <div style={{ color: '#555', padding: 16 }}>Loading...</div>
  return (
    <div style={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
      <Card label="Capital" value={fmtINR(summary.current_capital)}
            sub={`Initial: ${fmtINR(summary.initial_capital)}`} />
      <Card label="Lifetime P&L" value={fmtINR(summary.lifetime_pnl)}
            sub={fmtPct(summary.lifetime_pnl_pct)}
            negative={summary.lifetime_pnl < 0} />
      <Card label="Days Traded" value={summary.days_traded}
            sub={`${summary.total_trades} trades`} />
      <Card label="Win Rate" value={fmtPct(summary.win_rate_pct, 1)}
            sub={summary.best_trade ? `Best: ${summary.best_trade.ticker} ${fmtINR(summary.best_trade.pnl)}` : null} />
    </div>
  )
}
