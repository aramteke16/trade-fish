import React from 'react'
import { BrowserRouter, Routes, Route, Link, useLocation } from 'react-router-dom'
import Home from './pages/Home'
import Today from './pages/Today'
import HistoryDate from './pages/HistoryDate'
import Settings from './pages/Settings'

function NavLink({ to, children, exact }) {
  const { pathname } = useLocation()
  const active = exact ? pathname === to : pathname === to || (to !== '/' && pathname.startsWith(to))
  return (
    <Link
      to={to}
      style={{
        color: active ? '#fff' : '#666',
        textDecoration: 'none',
        fontSize: 13,
        fontWeight: active ? 600 : 400,
        letterSpacing: '0.02em',
        padding: '6px 0',
        borderBottom: active ? '1px solid #fff' : '1px solid transparent',
        transition: 'color 0.15s',
      }}
    >
      {children}
    </Link>
  )
}

function Shell() {
  return (
    <>
      <nav style={{
        display: 'flex',
        alignItems: 'center',
        gap: 24,
        padding: '0 32px',
        height: 48,
        borderBottom: '1px solid #1a1a1a',
        background: '#000',
        position: 'sticky',
        top: 0,
        zIndex: 40,
      }}>
        <Link to="/" style={{ textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 8, marginRight: 16 }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: '#fff', letterSpacing: '-0.02em' }}>
            TradeFish
          </span>
        </Link>
        <NavLink to="/" exact>Home</NavLink>
        <NavLink to="/today">Today</NavLink>
        <NavLink to="/history">History</NavLink>
        <NavLink to="/settings">Settings</NavLink>
      </nav>
      <div style={{ padding: '24px 32px', maxWidth: 1080, margin: '0 auto' }}>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/today" element={<Today />} />
          <Route path="/history" element={<HistoryDate />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </div>
    </>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <Shell />
    </BrowserRouter>
  )
}
