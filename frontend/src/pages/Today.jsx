import React, { useEffect, useState } from 'react'
import usePolling from '../hooks/usePolling'
import { getToday, getTokenStats, getDayFiles, fmtINR, todayIST } from '../api'
import PipelineStateBadge from '../components/PipelineStateBadge'
import LiveDebateStream from '../components/LiveDebateStream'
import PositionTableLive from '../components/PositionTableLive'
import TokenUsagePanel from '../components/TokenUsagePanel'
import DayFiles from '../components/DayFiles'
import CapitalLogTable from '../components/CapitalLogTable'

const section = {
  marginBottom: 16,
  border: '1px solid #1a1a1a',
  borderRadius: 2,
  padding: 16,
}
const label = { fontSize: 11, color: '#555', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 12 }

export default function Today() {
  // Roll the date over automatically when IST midnight passes while the
  // tab stays open. Without this, a tab opened at 23:55 IST would keep
  // querying yesterday's date forever.
  const [dateStr, setDateStr] = useState(todayIST())
  useEffect(() => {
    const id = setInterval(() => {
      const t = todayIST()
      setDateStr((prev) => (prev === t ? prev : t))
    }, 30_000)
    return () => clearInterval(id)
  }, [])
  const todayQ = usePolling(() => getToday(dateStr), 5000, [dateStr])
  const tokensQ = usePolling(() => getTokenStats(dateStr), 10000, [dateStr])
  const filesQ = usePolling(() => getDayFiles(dateStr), 15000, [dateStr])

  const data = todayQ.data
  const tokens = tokensQ.data
  const open = data?.open_positions || []
  const plans = data?.trade_plans || []
  const plansByTicker = Object.fromEntries(plans.map((p) => [p.ticker, p]))

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, flexWrap: 'wrap', gap: 8 }}>
        <h1 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>{dateStr}</h1>
        <PipelineStateBadge />
      </div>

      {data?.portfolio && (() => {
        const p = data.portfolio
        const realized = p.realized_pnl || 0
        const pendingShown = (p.pending_reserved || 0) > 0
        const tiles = [
          { label: 'Invested Amount', value: fmtINR(p.seed_capital), sub: 'lifetime seed' },
          { label: 'Current Value', value: fmtINR(p.current_value), sub: `start ${fmtINR(p.start_capital)} ${realized >= 0 ? '+' : '−'} ${fmtINR(Math.abs(realized))}` },
          { label: 'Free Cash', value: fmtINR(p.free_cash), sub: pendingShown ? `pending ${fmtINR(p.pending_reserved)} · invested ${fmtINR(p.invested)}` : `invested ${fmtINR(p.invested)}` },
          { label: 'Realized P&L', value: fmtINR(realized), negative: realized < 0, sub: p.is_finalized ? 'day closed' : 'live' },
        ]
        return (
          <div style={{ display: 'flex', gap: 1, flexWrap: 'wrap', marginBottom: 16 }}>
            {tiles.map((c) => (
              <div key={c.label} style={{
                flex: 1, minWidth: 160, padding: '12px 16px',
                border: '1px solid #1a1a1a', borderRadius: 2,
              }}>
                <div style={{ fontSize: 11, color: '#666', textTransform: 'uppercase', letterSpacing: '0.06em' }}>{c.label}</div>
                <div style={{
                  fontSize: 20, fontWeight: 600, marginTop: 4,
                  fontVariantNumeric: 'tabular-nums',
                  color: c.negative ? '#c66' : '#e5e5e5',
                }}>{c.value}</div>
                {c.sub && (
                  <div style={{ fontSize: 10, color: '#555', marginTop: 4, fontVariantNumeric: 'tabular-nums' }}>{c.sub}</div>
                )}
              </div>
            ))}
          </div>
        )
      })()}

      <div style={section}>
        <LiveDebateStream />
      </div>

      <div style={section}>
        <div style={label}>Trade plans</div>
        {plans.length === 0 ? (
          <div style={{ color: '#333', fontSize: 13 }}>No plans yet</div>
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: 8 }}>
            {plans.map((p) => (
              <div key={p.id} style={{ border: '1px solid #1a1a1a', borderRadius: 2, padding: 12, fontSize: 12 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                  <span style={{ fontWeight: 600, fontSize: 13 }}>
                    {p.ticker}
                    {p.is_dry_run ? (
                      <span style={{ fontSize: 9, background: '#2a2a00', color: '#e8c838', border: '1px solid #555', borderRadius: 2, padding: '1px 4px', marginLeft: 5, fontWeight: 400 }}>DRY RUN</span>
                    ) : null}
                    {!p.is_dry_run && p.price_adjusted_pct != null && Math.abs(p.price_adjusted_pct) > 0 && (
                      <span style={{ fontSize: 10, color: p.price_adjusted_pct > 0 ? '#e8a838' : '#5b9cf6', marginLeft: 5, fontWeight: 400 }}>
                        {p.price_adjusted_pct > 0 ? '↑' : '↓'}{Math.abs(p.price_adjusted_pct).toFixed(1)}% adj
                      </span>
                    )}
                  </span>
                  <span style={{ fontWeight: 500, color: '#888' }}>{p.rating}</span>
                </div>
                <div style={{ color: '#555' }}>
                  Conf {p.confidence_score ?? '–'}/10 / entry {p.entry_zone_low != null && p.entry_zone_high != null
                    ? `${p.entry_zone_low.toFixed(1)}-${p.entry_zone_high.toFixed(1)}` : '–'}
                </div>
                <div style={{ color: '#555' }}>
                  SL {p.stop_loss != null ? p.stop_loss.toFixed(1) : '–'} / T1 {p.target_1 != null ? p.target_1.toFixed(1) : '–'}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div style={section}>
        <div style={label}>Open positions</div>
        <PositionTableLive open={open} plansById={plansByTicker} onChange={() => todayQ.refresh()} />
      </div>

      <div style={section}>
        <div style={label}>Capital log (per monitor check)</div>
        <CapitalLogTable date={dateStr} />
      </div>

      <div style={section}>
        <div style={label}>LLM usage</div>
        <TokenUsagePanel rows={tokens?.rows || []} />
      </div>

      <div style={section}>
        <div style={label}>Reports & files</div>
        <DayFiles files={filesQ.data?.files || []} date={dateStr} />
      </div>
    </div>
  )
}
