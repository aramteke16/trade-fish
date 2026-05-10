import React from 'react'
import { BrowserRouter, Routes, Route, Link } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import Debates from './pages/Debates'
import Performance from './pages/Performance'
import History from './pages/History'

const navStyle = {
  display: 'flex', gap: 16, padding: '16px 24px', background: '#1e293b',
  borderBottom: '1px solid #334155', alignItems: 'center',
}
const linkStyle = { color: '#94a3b8', textDecoration: 'none', fontWeight: 500 }
const activeStyle = { color: '#38bdf8', textDecoration: 'none', fontWeight: 600 }

function NavLink({ to, children }) {
  const path = window.location.pathname
  const isActive = path === to || (to !== '/' && path.startsWith(to))
  return <Link to={to} style={isActive ? activeStyle : linkStyle}>{children}</Link>
}

export default function App() {
  return (
    <BrowserRouter>
      <nav style={navStyle}>
        <div style={{ fontWeight: 700, fontSize: 18, color: '#38bdf8', marginRight: 16 }}>
          🐟 TradeFish
        </div>
        <NavLink to="/">Dashboard</NavLink>
        <NavLink to="/debates">Debates</NavLink>
        <NavLink to="/performance">Performance</NavLink>
        <NavLink to="/history">History</NavLink>
      </nav>
      <div style={{ padding: 24, maxWidth: 1200, margin: '0 auto' }}>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/debates" element={<Debates />} />
          <Route path="/performance" element={<Performance />} />
          <Route path="/history" element={<History />} />
        </Routes>
      </div>
    </BrowserRouter>
  )
}
