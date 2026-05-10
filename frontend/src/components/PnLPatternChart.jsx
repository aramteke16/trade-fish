import React, { useRef } from 'react'
import { Bar } from 'react-chartjs-2'
import {
  Chart as ChartJS, CategoryScale, LinearScale, BarElement, Tooltip, Legend, Title,
} from 'chart.js'
import { fmtINR } from '../api'

ChartJS.register(CategoryScale, LinearScale, BarElement, Tooltip, Legend, Title)

export default function PnLPatternChart({ rows, onClickDay }) {
  const chartRef = useRef(null)
  if (!rows || rows.length === 0) {
    return <div style={{ color: '#555', padding: 24 }}>No P&L history yet.</div>
  }
  const series = [...rows].reverse()
  const data = {
    labels: series.map((r) => r.date),
    datasets: [{
      label: 'Daily P&L',
      data: series.map((r) => r.daily_pnl ?? 0),
      backgroundColor: series.map((r) => (r.daily_pnl >= 0 ? '#e5e5e5' : '#555')),
      borderRadius: 1,
      borderSkipped: false,
    }],
  }
  const opts = {
    responsive: true,
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: '#111',
        borderColor: '#333',
        borderWidth: 1,
        titleColor: '#999',
        bodyColor: '#e5e5e5',
        titleFont: { size: 11 },
        bodyFont: { size: 12 },
        padding: 10,
        callbacks: {
          label: (ctx) => {
            const r = series[ctx.dataIndex]
            const lines = [`P&L: ${fmtINR(r.daily_pnl)}`]
            if (r.worst_trade) lines.push(`Worst: ${r.worst_trade.ticker} ${fmtINR(r.worst_trade.pnl)}`)
            return lines
          },
        },
      },
    },
    scales: {
      x: {
        ticks: { color: '#444', font: { size: 10 } },
        grid: { color: '#111' },
        border: { color: '#1a1a1a' },
      },
      y: {
        ticks: { color: '#444', font: { size: 10 } },
        grid: { color: '#111' },
        border: { color: '#1a1a1a' },
      },
    },
    onClick: (evt) => {
      const chart = chartRef.current
      if (!chart) return
      const els = chart.getElementsAtEventForMode(evt, 'nearest', { intersect: true }, true)
      if (els.length) onClickDay?.(series[els[0].index])
    },
  }
  return <Bar ref={chartRef} data={data} options={opts} height={80} />
}
