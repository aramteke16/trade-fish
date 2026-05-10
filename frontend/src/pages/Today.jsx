import React from 'react'
import usePolling from '../hooks/usePolling'
import { getToday, getTokenStats, getDayFiles } from '../api'
import PipelineStateBadge from '../components/PipelineStateBadge'
import LiveDebateStream from '../components/LiveDebateStream'
import PositionTableLive from '../components/PositionTableLive'
import TokenUsagePanel from '../components/TokenUsagePanel'
import DayFiles from '../components/DayFiles'

const section = {
  marginBottom: 16,
  border: '1px solid #1a1a1a',
  borderRadius: 2,
  padding: 16,
}
const label = { fontSize: 11, color: '#555', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 12 }

const today = () => new Date().toISOString().slice(0, 10)

export default function Today() {
  const dateStr = today()
  const todayQ = usePolling(() => getToday(dateStr), 5000)
  const tokensQ = usePolling(() => getTokenStats(dateStr), 10000)
  const filesQ = usePolling(() => getDayFiles(dateStr), 15000)

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
                  <span style={{ fontWeight: 600, fontSize: 13 }}>{p.ticker}</span>
                  <span style={{ fontWeight: 500, color: '#888' }}>{p.rating}</span>
                </div>
                <div style={{ color: '#555' }}>
                  Conf {p.confidence_score}/10 / entry {p.entry_zone_low?.toFixed(1)}-{p.entry_zone_high?.toFixed(1)}
                </div>
                <div style={{ color: '#555' }}>
                  SL {p.stop_loss?.toFixed(1)} / T1 {p.target_1?.toFixed(1)}
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
