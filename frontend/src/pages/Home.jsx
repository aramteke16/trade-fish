import React, { useState } from 'react'
import usePolling from '../hooks/usePolling'
import { getGlobalSummary, getLossAttribution } from '../api'
import SummaryCards from '../components/SummaryCards'
import ResetCapitalModal from '../components/ResetCapitalModal'
import PnLPatternChart from '../components/PnLPatternChart'
import LossAttributionDrawer from '../components/LossAttributionDrawer'
import AnalyzeStockBox from '../components/AnalyzeStockBox'
import OnDemandList from '../components/OnDemandList'

const section = {
  marginBottom: 16,
  border: '1px solid #1a1a1a',
  borderRadius: 2,
  padding: 16,
}

export default function Home() {
  const summaryQ = usePolling(getGlobalSummary, 10000)
  const lossQ = usePolling(() => getLossAttribution(60), 30000)
  const [resetOpen, setResetOpen] = useState(false)
  const [drawerDay, setDrawerDay] = useState(null)
  const [refreshKey, setRefreshKey] = useState(0)

  const summary = summaryQ.data
  const lossDays = lossQ.data?.days || []

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, flexWrap: 'wrap', gap: 8 }}>
        <h1 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>Home</h1>
        {summary?.paper_mode && (
          <button
            onClick={() => setResetOpen(true)}
            style={{
              background: 'transparent', color: '#666', border: '1px solid #222',
              padding: '5px 12px', borderRadius: 2, cursor: 'pointer', fontSize: 12,
            }}
          >Reset Capital</button>
        )}
      </div>

      <div style={{ marginBottom: 16 }}>
        <SummaryCards summary={summary} />
      </div>

      <div style={section}>
        <div style={{ fontSize: 11, color: '#555', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 4 }}>
          P&L — last 60 days
        </div>
        <div style={{ fontSize: 11, color: '#333', marginBottom: 10 }}>Click a bar to see what drove that day</div>
        <PnLPatternChart rows={lossDays} onClickDay={setDrawerDay} />
      </div>

      <div style={section}>
        <div style={{ fontSize: 11, color: '#555', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10 }}>
          Analyze a stock
        </div>
        <AnalyzeStockBox onSubmitted={() => setRefreshKey((k) => k + 1)} />
      </div>

      <OnDemandList refreshKey={refreshKey} />

      <ResetCapitalModal
        open={resetOpen}
        onClose={() => setResetOpen(false)}
        onDone={() => summaryQ.refresh()}
        initialCapital={summary?.initial_capital}
      />
      <LossAttributionDrawer day={drawerDay} onClose={() => setDrawerDay(null)} />
    </div>
  )
}
