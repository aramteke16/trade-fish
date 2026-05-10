import React from 'react'
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  BarElement,
  Title,
  Tooltip,
  Legend,
} from 'chart.js'
import { Line, Bar } from 'react-chartjs-2'

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, BarElement, Title, Tooltip, Legend)

export default function PnLChart({ metrics }) {
  if (!metrics || metrics.length === 0) {
    return <div style={{ color: '#64748b', padding: 20 }}>No performance data yet.</div>
  }

  const labels = [...metrics].reverse().map(m => m.date)
  const capitalData = [...metrics].reverse().map(m => m.capital)
  const pnlData = [...metrics].reverse().map(m => m.daily_pnl)

  const capitalChart = {
    labels,
    datasets: [{
      label: 'Capital',
      data: capitalData,
      borderColor: '#38bdf8',
      backgroundColor: 'rgba(56, 189, 248, 0.1)',
      fill: true,
      tension: 0.3,
    }],
  }

  const pnlChart = {
    labels,
    datasets: [{
      label: 'Daily P&L',
      data: pnlData,
      backgroundColor: pnlData.map(v => v >= 0 ? '#4ade80' : '#f87171'),
    }],
  }

  const options = {
    responsive: true,
    plugins: {
      legend: { labels: { color: '#e2e8f0' } },
    },
    scales: {
      x: { ticks: { color: '#94a3b8' }, grid: { color: '#334155' } },
      y: { ticks: { color: '#94a3b8' }, grid: { color: '#334155' } },
    },
  }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 24 }}>
      <div style={{ background: '#1e293b', borderRadius: 12, padding: 16, border: '1px solid #334155' }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>Capital Over Time</div>
        <Line data={capitalChart} options={options} />
      </div>
      <div style={{ background: '#1e293b', borderRadius: 12, padding: 16, border: '1px solid #334155' }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>Daily P&L</div>
        <Bar data={pnlChart} options={options} />
      </div>
    </div>
  )
}
