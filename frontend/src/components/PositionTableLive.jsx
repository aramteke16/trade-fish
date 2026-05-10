import React, { useState } from 'react'
import { exitPosition, excludeFromFeedback, fmtINR } from '../api'

const th = {
  textAlign: 'left', color: '#555', fontSize: 10, textTransform: 'uppercase',
  letterSpacing: '0.06em', borderBottom: '1px solid #1a1a1a', padding: '8px 8px',
  fontWeight: 500,
}
const td = { padding: '10px 8px', borderBottom: '1px solid #111', fontSize: 13, fontVariantNumeric: 'tabular-nums' }

export default function PositionTableLive({ open = [], plansById = {}, onChange }) {
  const [busyId, setBusyId] = useState(null)
  const [err, setErr] = useState(null)

  async function doExit(p) {
    if (!confirm(`Force-exit ${p.ticker} at current market price?`)) return
    setErr(null); setBusyId(p.id)
    try {
      await exitPosition(p.id, { reason: 'manual_exit' })
      onChange?.()
    } catch (e) {
      setErr(`${p.ticker}: ${e.message}`)
    } finally {
      setBusyId(null)
    }
  }

  async function toggleExclude(planId, exclude) {
    try {
      await excludeFromFeedback(planId, exclude)
      onChange?.()
    } catch (e) {
      setErr(String(e.message || e))
    }
  }

  return (
    <div>
      {err && <div style={{ color: '#888', fontSize: 12, marginBottom: 8 }}>{err}</div>}
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <th style={th}>Ticker</th>
            <th style={th}>Qty</th>
            <th style={th}>Entry</th>
            <th style={th}>SL / T1</th>
            <th style={th}>P&L</th>
            <th style={th}>Excl</th>
            <th style={th}></th>
          </tr>
        </thead>
        <tbody>
          {open.length === 0 && (
            <tr><td colSpan={7} style={{ ...td, color: '#333', textAlign: 'center' }}>No open positions</td></tr>
          )}
          {open.map((p) => {
            const plan = plansById[p.ticker]
            const excluded = !!plan?.exclude_from_feedback
            return (
              <tr key={p.id}>
                <td style={td}><span style={{ fontWeight: 500 }}>{p.ticker}</span></td>
                <td style={td}>{p.quantity}</td>
                <td style={td}>{fmtINR(p.entry_price)}</td>
                <td style={td}>{fmtINR(p.stop_loss)} / {fmtINR(p.target_1)}</td>
                <td style={td}>{p.pnl == null ? '--' : fmtINR(p.pnl)}</td>
                <td style={td}>
                  {plan ? (
                    <input type="checkbox" checked={excluded}
                           onChange={(e) => toggleExclude(plan.id, e.target.checked)}
                           style={{ accentColor: '#666' }} />
                  ) : '--'}
                </td>
                <td style={td}>
                  <button
                    disabled={busyId === p.id}
                    onClick={() => doExit(p)}
                    style={{
                      background: 'transparent', color: '#e5e5e5',
                      border: '1px solid #333', padding: '3px 10px',
                      fontSize: 11, borderRadius: 2, cursor: 'pointer',
                    }}
                  >{busyId === p.id ? '...' : 'Exit'}</button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
