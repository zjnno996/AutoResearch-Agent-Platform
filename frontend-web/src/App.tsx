import { HashRouter, Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider, useAuth } from './contexts/AuthContext'
import LoginPage from './components/LoginPage'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import ResearchLab from './pages/ResearchLab'
import AutoReview from './pages/AutoReview'

function AuthenticatedApp() {
  const { token, login } = useAuth()

  if (!token) {
    return <LoginPage onLogin={login} />
  }

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/research-lab" element={<ResearchLab />} />
        <Route path="/auto-review" element={<AutoReview />} />
      </Route>
    </Routes>
  )
}

export default function App() {
  return (
    <HashRouter>
      <AuthProvider>
        <AuthenticatedApp />
      </AuthProvider>
    </HashRouter>
  )
}
