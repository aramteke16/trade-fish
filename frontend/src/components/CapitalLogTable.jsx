import React from 'react'
import usePolling from '../hooks/usePolling'
import { getCapitalLog, fmtINR } from '../api'

function fmtTime(iso) {
  if (!iso) return ''
  let s = String(iso)
  if (!s.endsWith('Z') && !s.includes('+')) s = s + 'Z'
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true })
}

const td = { padding: '6px 8px', borderBottom: '1px solid #111', fontSize: 12, fontVariantNumeric: 'tabular-nums' }
const th = { ...td, textAlign: 'left', color: '#666', fontWeight: 500, fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.04em', borderBottom: '1px solid #222' }

function pnlColor(n) {
  if (n == null || n === 0) return '#888'
  return n > 0 ? '#7c7' : '#c66'
}

const TRIGGER_LABEL = {
  day_init: 'Day init',
  orders_placed: 'Orders placed',
  monitor_tick: 'Monitor tick',
  hard_exit: 'Hard exit',
  day_finalized: 'Day closed',
}

export default function CapitalLogTable({ date }) {
  const { data } = usePolling(() => getCapitalLog(date, 100), 5000, [date])
  const rows = data?.rows || []

  if (rows.length === 0) {
    return <div style={{ color: '#444', fontSize: 12 }}>No capital snapshots yet for {date}.</div>
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <th style={th}>Time</th>
            <th style={th}>Trigger</th>
            <th style={{ ...th, textAlign: 'right' }}>Current Value</th>
            <th style={{ ...th, textAlign: 'right' }}>Free Cash</th>
            <th style={{ ...th, textAlign: 'right' }}>Invested</th>
            <th style={{ ...th, textAlign: 'right' }}>Pending</th>
            <th style={{ ...th, textAlign: 'right' }}>Realized P&L</th>
            <th style={{ ...th, textAlign: 'right' }}>Unrealized P&L</th>
            <th style={{ ...th, textAlign: 'right' }}>Open</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td style={td}>{fmtTime(r.at)}</td>
              <td style={{ ...td, color: '#888' }}>{TRIGGER_LABEL[r.trigger] || r.trigger || '—'}</td>
              <td style={{ ...td, textAlign: 'right' }}>{fmtINR(r.current_value)}</td>
              <td style={{ ...td, textAlign: 'right' }}>{fmtINR(r.free_cash)}</td>
              <td style={{ ...td, textAlign: 'right' }}>{fmtINR(r.invested)}</td>
              <td style={{ ...td, textAlign: 'right', color: (r.pending_reserved || 0) > 0 ? '#e5e5e5' : '#444' }}>
                {fmtINR(r.pending_reserved)}
              </td>
              <td style={{ ...td, textAlign: 'right', color: pnlColor(r.realized_pnl) }}>{fmtINR(r.realized_pnl)}</td>
              <td style={{ ...td, textAlign: 'right', color: pnlColor(r.unrealized_pnl) }}>{fmtINR(r.unrealized_pnl)}</td>
              <td style={{ ...td, textAlign: 'right' }}>{r.open_positions_count ?? 0}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
