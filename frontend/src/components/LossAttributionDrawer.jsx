import React from 'react'
import { fmtINR, fmtPct } from '../api'

export default function LossAttributionDrawer({ day, onClose }) {
  if (!day) return null
  return (
    <>
      <div
        style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)', zIndex: 50 }}
        onClick={onClose}
      />
      <div style={{
        position: 'fixed', top: 0, right: 0, bottom: 0, width: 400,
        background: '#0a0a0a', borderLeft: '1px solid #1a1a1a',
        padding: 24, overflowY: 'auto', zIndex: 51,
      }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <div style={{ fontSize: 12, color: '#555', marginBottom: 4 }}>{day.date}</div>
            <div style={{ fontSize: 26, fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>
              {fmtINR(day.daily_pnl)}
            </div>
          </div>
          <button
            onClick={onClose}
            style={{ background: 'transparent', color: '#555', border: 'none', fontSize: 20, cursor: 'pointer', padding: 4 }}
          >x</button>
        </div>

        <div style={{ color: '#666', fontSize: 13, marginTop: 4 }}>
          {fmtPct(day.daily_return_pct)} / {day.total_trades || (day.all_trades?.length ?? 0)} trades / win rate {fmtPct(day.win_rate, 1)}
        </div>

        {day.worst_trade && day.daily_pnl < 0 && (
          <div style={{ marginTop: 20, padding: 14, border: '1px solid #1a1a1a', borderRadius: 2 }}>
            <div style={{ fontSize: 11, color: '#555', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Biggest loss</div>
            <div style={{ fontWeight: 600, fontSize: 16, marginTop: 6 }}>{day.worst_trade.ticker}</div>
            <div style={{ fontSize: 14, color: '#888', marginTop: 2 }}>
              {fmtINR(day.worst_trade.pnl)} ({fmtPct(day.worst_trade.pnl_pct)})
            </div>
            <div style={{ color: '#555', fontSize: 12, marginTop: 6 }}>
              Exit: {day.worst_trade.exit_reason || '--'}
            </div>
          </div>
        )}

        <div style={{ marginTop: 24, fontSize: 12, color: '#555', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 12 }}>
          All trades
        </div>
        {(day.all_trades || []).map((t) => (
          <div key={t.id} style={{ borderBottom: '1px solid #111', padding: '10px 0' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ fontWeight: 500, fontSize: 13 }}>{t.ticker}</span>
              <span style={{ fontVariantNumeric: 'tabular-nums', fontSize: 13 }}>
                {fmtINR(t.pnl)}
              </span>
            </div>
            <div style={{ color: '#555', fontSize: 11, marginTop: 2 }}>
              {t.quantity} qty / {fmtINR(t.entry_price)} {'→'} {fmtINR(t.exit_price)} / {t.exit_reason || '--'}
            </div>
          </div>
        ))}
        {(!day.all_trades || day.all_trades.length === 0) && (
          <div style={{ color: '#444', marginTop: 8 }}>No closed trades.</div>
        )}
      </div>
    </>
  )
}
