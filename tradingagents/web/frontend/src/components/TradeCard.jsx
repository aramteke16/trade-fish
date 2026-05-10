import React from 'react'

export default function TradeCard({ plan }) {
  return (
    <div style={{
      background: '#1e293b', borderRadius: 12, padding: 16,
      border: '1px solid #334155', display: 'flex', flexDirection: 'column', gap: 8,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div style={{ fontWeight: 700, fontSize: 16 }}>{plan.ticker}</div>
        <div style={{
          fontSize: 12, fontWeight: 600, padding: '4px 10px', borderRadius: 20,
          background: plan.rating === 'Buy' || plan.rating === 'Overweight' ? '#064e3b' :
                     plan.rating === 'Sell' || plan.rating === 'Underweight' ? '#7f1d1d' : '#713f12',
          color: plan.rating === 'Buy' || plan.rating === 'Overweight' ? '#4ade80' :
                 plan.rating === 'Sell' || plan.rating === 'Underweight' ? '#f87171' : '#fbbf24',
        }}>
          {plan.rating}
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, fontSize: 13 }}>
        <div>
          <span style={{ color: '#94a3b8' }}>Entry Zone:</span>{' '}
          <span style={{ fontWeight: 600 }}>₹{plan.entry_zone_low} - ₹{plan.entry_zone_high}</span>
        </div>
        <div>
          <span style={{ color: '#94a3b8' }}>SL:</span>{' '}
          <span style={{ fontWeight: 600, color: '#f87171' }}>₹{plan.stop_loss}</span>
        </div>
        <div>
          <span style={{ color: '#94a3b8' }}>T1:</span>{' '}
          <span style={{ fontWeight: 600, color: '#4ade80' }}>₹{plan.target_1}</span>
        </div>
        <div>
          <span style={{ color: '#94a3b8' }}>T2:</span>{' '}
          <span style={{ fontWeight: 600, color: '#4ade80' }}>₹{plan.target_2}</span>
        </div>
      </div>

      <div style={{ fontSize: 12, color: '#94a3b8', marginTop: 4 }}>
        Confidence: {plan.confidence_score}/10 · Size: {plan.position_size_pct}%
      </div>
      {plan.skip_rule && (
        <div style={{ fontSize: 11, color: '#fbbf24', marginTop: 2 }}>⚠ {plan.skip_rule}</div>
      )}
    </div>
  )
}
