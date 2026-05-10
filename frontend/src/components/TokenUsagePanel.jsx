import React from 'react'

const th = {
  textAlign: 'left', color: '#555', fontSize: 10, textTransform: 'uppercase',
  letterSpacing: '0.06em', borderBottom: '1px solid #1a1a1a', padding: '6px 8px',
  fontWeight: 500,
}
const td = { padding: '6px 8px', borderBottom: '1px solid #111', fontSize: 12, fontVariantNumeric: 'tabular-nums' }

export default function TokenUsagePanel({ rows = [] }) {
  if (rows.length === 0) {
    return <div style={{ color: '#333', fontSize: 13 }}>No usage recorded</div>
  }
  const totals = rows.reduce(
    (acc, r) => ({
      llm_calls: acc.llm_calls + (r.llm_calls || 0),
      tool_calls: acc.tool_calls + (r.tool_calls || 0),
      tokens_in: acc.tokens_in + (r.tokens_in || 0),
      tokens_out: acc.tokens_out + (r.tokens_out || 0),
    }),
    { llm_calls: 0, tool_calls: 0, tokens_in: 0, tokens_out: 0 },
  )
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse' }}>
      <thead>
        <tr>
          <th style={th}>Stage</th>
          <th style={th}>Ticker</th>
          <th style={th}>Model</th>
          <th style={th}>LLM</th>
          <th style={th}>Tool</th>
          <th style={th}>In</th>
          <th style={th}>Out</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id || `${r.date}-${r.stage}-${r.ticker || ''}`}>
            <td style={td}>{r.stage}</td>
            <td style={{ ...td, color: '#666' }}>{r.ticker || '--'}</td>
            <td style={{ ...td, color: '#666' }}>{r.model || '--'}</td>
            <td style={td}>{r.llm_calls}</td>
            <td style={td}>{r.tool_calls}</td>
            <td style={td}>{r.tokens_in?.toLocaleString()}</td>
            <td style={td}>{r.tokens_out?.toLocaleString()}</td>
          </tr>
        ))}
        <tr>
          <td style={{ ...td, fontWeight: 600 }} colSpan={3}>Total</td>
          <td style={{ ...td, fontWeight: 600 }}>{totals.llm_calls}</td>
          <td style={{ ...td, fontWeight: 600 }}>{totals.tool_calls}</td>
          <td style={{ ...td, fontWeight: 600 }}>{totals.tokens_in.toLocaleString()}</td>
          <td style={{ ...td, fontWeight: 600 }}>{totals.tokens_out.toLocaleString()}</td>
        </tr>
      </tbody>
    </table>
  )
}
