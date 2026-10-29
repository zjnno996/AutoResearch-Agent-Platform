import { HashRouter, Routes, Route, Navigate } from "react-router-dom";
import { lazy } from "react";
import { AuthProvider, useAuth } from "./contexts/AuthContext";
import LoginPage from "./components/LoginPage";
import Layout from "./components/Layout";
import { WorkspaceProvider } from "./contexts/WorkspaceContext";
import { ThemeProvider } from "./contexts/ThemeContext";
const Dashboard = lazy(() => import("./pages/Dashboard"));
const ResearchLab = lazy(() => import("./pages/ResearchLab"));
const AutoReview = lazy(() => import("./pages/AutoReview"));
const SkillLibrary = lazy(() => import("./pages/SkillLibrary"));
const Settings = lazy(() => import("./pages/Settings"));

function AuthenticatedApp() {
  const { token, login } = useAuth();

  if (!token) {
    return <LoginPage onLogin={login} />;
  }

  return (
    <WorkspaceProvider>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/research-lab" element={<ResearchLab />} />
          <Route path="/auto-review" element={<AutoReview />} />
          <Route path="/skills" element={<SkillLibrary />} />
          <Route path="/settings" element={<Settings />} />
          <Route
            path="*"
            element={
              <div className="empty-state">
                <h1>页面未找到</h1>
                <p>请从侧栏选择工作空间。</p>
              </div>
            }
          />
        </Route>
      </Routes>
    </WorkspaceProvider>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <HashRouter>
        <AuthProvider>
          <AuthenticatedApp />
        </AuthProvider>
      </HashRouter>
    </ThemeProvider>
  );
}
