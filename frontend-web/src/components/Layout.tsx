import { Outlet, NavLink } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'

export default function Layout() {
  const { user, logout } = useAuth()

  return (
    <div className="web-app">
      <header className="web-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
          <NavLink to="/dashboard" className="nav-brand">🦞 Claw AI Lab</NavLink>
          <nav className="nav-tabs">
            <NavLink to="/dashboard" end className={({ isActive }) => isActive ? 'nav-tab active' : 'nav-tab'}>
              Dashboard
            </NavLink>
            <NavLink to="/research-lab" className={({ isActive }) => isActive ? 'nav-tab active' : 'nav-tab'}>
              Research Lab
            </NavLink>
            <NavLink to="/auto-review" className={({ isActive }) => isActive ? 'nav-tab active' : 'nav-tab'}>
              Auto Review
            </NavLink>
          </nav>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          {user && <span className="user-badge">{user.username}</span>}
          <button className="logout-btn" onClick={logout} title="退出登录">🚪</button>
        </div>
      </header>
      <main>
        <Outlet />
      </main>
    </div>
  )
}
