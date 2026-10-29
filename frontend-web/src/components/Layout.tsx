import { useEffect, useState, Suspense } from "react";
import { Outlet, NavLink, Link, useLocation } from "react-router-dom";
import {
  LayoutDashboard,
  FlaskConical,
  FileCheck2,
  Layers3,
  Settings2,
  ArrowUpRight,
  PanelLeft,
  Search,
  Sun,
  Moon,
  LogOut,
  ChevronRight,
  CircleHelp,
  X,
} from "lucide-react";
import { useAuth } from "../contexts/AuthContext";
import { useWorkspace } from "../contexts/WorkspaceContext";
import { useTheme } from "../contexts/ThemeContext";
import Brand from "./Brand";
import SearchDialog from "./SearchDialog";

const NAVIGATION = [
  { to: "/dashboard", label: "实验室总览", icon: LayoutDashboard },
  { to: "/research-lab", label: "科研工作台", icon: FlaskConical },
  { to: "/auto-review", label: "学术评审", icon: FileCheck2 },
  { to: "/skills", label: "研究技能库", icon: Layers3 },
];
export default function Layout() {
  const { user, logout } = useAuth();
  const { status, capabilities } = useWorkspace();
  const { theme, toggle } = useTheme();
  const location = useLocation();
  const [search, setSearch] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [isMobile, setIsMobile] = useState(() => window.innerWidth < 900);
  useEffect(() => {
    const resize = () => setIsMobile(window.innerWidth < 900);
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);
  const current =
    NAVIGATION.find((item) => location.pathname.startsWith(item.to))?.label ??
    "工作空间设置";
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setSearch((value) => !value);
      }
      if (event.key === "Escape") setMobileOpen(false);
    };
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, []);
  useEffect(() => {
    setMobileOpen(false);
    document.title = `${current} · EMNETS-RA`;
  }, [location.pathname, current]);
  return (
    <div
      className={`app-shell ${collapsed ? "is-collapsed" : ""} ${mobileOpen ? "mobile-open" : ""}`}
    >
      <a
        className="skip-link"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        跳转到主要内容
      </a>
      {mobileOpen && (
        <button
          className="sidebar-scrim"
          aria-label="收起导航"
          onClick={() => setMobileOpen(false)}
        />
      )}
      <aside
        className="app-sidebar"
        id="workspace-navigation"
        aria-label="主导航"
        aria-hidden={isMobile && !mobileOpen}
        inert={isMobile && !mobileOpen}
      >
        <Link
          to="/dashboard"
          className="sidebar-brand"
          aria-label="EMNETS-RA 首页"
        >
          <Brand />
        </Link>
        <div className="lab-identity">
          <span className="lab-identity-icon">浙</span>
          <span>
            浙江大学 · EMNETS 实验室<small>ZHEJIANG UNIVERSITY</small>
          </span>
        </div>
        <div className="nav-section-label">
          WORKSPACE<span>工作空间</span>
        </div>
        <nav className="workspace-nav">
          {NAVIGATION.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              title={label}
              className={({ isActive }) =>
                `workspace-nav-item ${isActive ? "active" : ""}`
              }
            >
              <Icon size={19} />
              <span>{label}</span>
              <ChevronRight size={14} className="nav-chevron" />
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-spacer" />
        <div className="sidebar-note">
          <span className="sidebar-note-symbol">求是 · 创新</span>
          <p>
            让研究有迹可循，
            <br />
            让发现值得信赖。
          </p>
          <Link to="/settings">
            <CircleHelp size={14} /> 了解科研工作流 <ArrowUpRight size={13} />
          </Link>
        </div>
        <nav className="workspace-nav secondary-nav">
          <NavLink
            className="workspace-nav-item"
            to="/settings"
            title="工作空间设置"
          >
            <Settings2 size={19} />
            <span>工作空间设置</span>
          </NavLink>
          <a
            className="workspace-nav-item"
            href="https://www.emnets.cn/"
            target="_blank"
            rel="noreferrer"
            title="EMNETS 实验室官网"
          >
            <ArrowUpRight size={19} />
            <span>实验室官网</span>
          </a>
        </nav>
        <div className="sidebar-bottom">
          <span className="sidebar-version-dot" />
          <span>EMNETS Research Assistant</span>
          <small>RA</small>
        </div>
      </aside>
      <div className="app-body">
        <header className="app-topbar">
          <div className="topbar-leading">
            <button
              className="icon-button"
              aria-label="切换导航"
              aria-controls="workspace-navigation"
              onClick={() =>
                window.innerWidth < 900
                  ? setMobileOpen((value) => !value)
                  : setCollapsed((value) => !value)
              }
            >
              <PanelLeft size={19} />
            </button>
            <div className="breadcrumb">
              <span>工作空间</span>
              <ChevronRight size={14} />
              <strong>{current}</strong>
            </div>
          </div>
          <div className="topbar-actions">
            <button
              className="global-search-button"
              onClick={() => setSearch(true)}
            >
              <Search size={16} />
              <span>搜索项目、技能…</span>
              <kbd>⌘ K</kbd>
            </button>
            <span
              className={`service-indicator ${status}`}
              title={capabilities?.model}
            >
              <i />
              {status === "online"
                ? "服务已连接"
                : status === "connecting"
                  ? "正在连接"
                  : "等待重连"}
            </span>
            <span className="topbar-divider" />
            <button
              className="icon-button"
              onClick={toggle}
              aria-label={theme === "light" ? "切换深色主题" : "切换浅色主题"}
            >
              {theme === "light" ? <Moon size={18} /> : <Sun size={18} />}
            </button>
            <span className="user-avatar" title={user?.username}>
              {user?.username?.slice(0, 1).toUpperCase() ?? "E"}
            </span>
            <button
              className="icon-button logout-action"
              onClick={logout}
              aria-label="退出登录"
            >
              <LogOut size={17} />
            </button>
          </div>
        </header>
        <main className="app-content" id="main-content" tabIndex={-1}>
          <Suspense
            fallback={
              <div className="page-loading" role="status">
                正在加载页面…
              </div>
            }
          >
            <Outlet />
          </Suspense>
        </main>
        <footer className="app-footer">
          <span>
            EMNETS-RA <span className="footer-separator">/</span>{" "}
            为严谨的研究而设计
          </span>
          <span>求是创新 · 探索无界</span>
        </footer>
      </div>
      {search && <SearchDialog onClose={() => setSearch(false)} />}
      {mobileOpen && (
        <button
          className="mobile-close icon-button"
          aria-label="关闭导航"
          onClick={() => setMobileOpen(false)}
        >
          <X size={20} />
        </button>
      )}
    </div>
  );
}
