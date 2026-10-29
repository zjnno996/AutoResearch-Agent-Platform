import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ArrowRight,
  UserRound,
  LockKeyhole,
  Eye,
  EyeOff,
  Sun,
  Moon,
} from "lucide-react";
import { saveAuth } from "../auth";
import { useTheme } from "../contexts/ThemeContext";
import Brand from "./Brand";
import ResearchOrbit from "./ResearchOrbit";

export default function LoginPage({
  onLogin,
}: {
  onLogin: (token: string, username: string) => void;
}) {
  const [page, setPage] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [visible, setVisible] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const { theme, toggle } = useTheme();
  const socket = useRef<WebSocket | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      if (timer.current) clearTimeout(timer.current);
      socket.current?.close();
    };
  }, []);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (loading || !username.trim() || !password.trim()) return;
    setError("");
    setLoading(true);
    let settled = false;
    const ws = new WebSocket(
      `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/agents`,
    );
    socket.current = ws;
    const finish = (message?: string) => {
      if (settled) return;
      settled = true;
      if (timer.current) clearTimeout(timer.current);
      ws.close();
      if (alive.current) {
        setLoading(false);
        if (message) setError(message);
      }
    };
    ws.onopen = () =>
      ws.send(
        JSON.stringify({ command: page, username: username.trim(), password }),
      );
    ws.onmessage = (event) => {
      if (!alive.current || settled) return;
      try {
        const message = JSON.parse(event.data);
        if (message.type !== "auth_result") return;
        if (
          message.payload.ok &&
          message.payload.token &&
          message.payload.user
        ) {
          saveAuth(message.payload.token, message.payload.user);
          finish();
          onLogin(message.payload.token, message.payload.user.username);
        } else finish(message.payload.error || "账号或密码不正确，请重试。");
      } catch {
        finish("登录响应无法解析，请稍后重试。");
      }
    };
    ws.onerror = () => finish("暂时无法连接研究服务，请稍后重试。");
    ws.onclose = () => {
      if (!settled) finish("连接已断开，请重新登录。");
    };
    timer.current = setTimeout(
      () => finish("连接超时，请检查服务后重试。"),
      10000,
    );
  };
  return (
    <div className="auth-page">
      <section className="auth-story">
        <Brand />
        <div className="auth-institution">
          浙江大学 · EMNETS 实验室<span>ZHEJIANG UNIVERSITY</span>
        </div>
        <div className="auth-story-content">
          <div className="page-kicker">A WORKSPACE FOR DISCOVERY</div>
          <h1>
            从一个问题，
            <br />
            抵达新的可能。
          </h1>
          <p>
            让文献、方法、代码与实验彼此连接。
            <br />
            为每一个值得探索的想法，提供可靠的科研工作空间。
          </p>
          <ResearchOrbit />
        </div>
        <footer>
          <span>求是创新</span>
          <span>EMNETS RESEARCH ASSISTANT</span>
        </footer>
      </section>
      <section className="auth-form-side">
        <button
          className="icon-button auth-theme"
          onClick={toggle}
          aria-label={theme === "light" ? "切换深色主题" : "切换浅色主题"}
        >
          {theme === "light" ? <Moon size={19} /> : <Sun size={19} />}
        </button>
        <div className="auth-form-wrap">
          <div className="auth-mobile-brand">
            <Brand />
          </div>
          <span className="auth-form-kicker">WELCOME TO EMNETS-RA</span>
          <h2>
            {page === "login" ? "欢迎回到科研工作空间" : "开启你的科研工作空间"}
          </h2>
          <p className="auth-form-subtitle">
            {page === "login"
              ? "登录账号，继续你的下一次探索。"
              : "创建实验室账号，让研究从这里开始。"}
          </p>
          <form onSubmit={submit}>
            <label className="auth-field" htmlFor="auth-username">
              用户名
              <span>
                <UserRound size={17} />
                <input
                  id="auth-username"
                  autoComplete="username"
                  placeholder="输入你的用户名"
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  required
                  maxLength={80}
                />
              </span>
            </label>
            <label className="auth-field" htmlFor="auth-password">
              密码
              <span>
                <LockKeyhole size={17} />
                <input
                  id="auth-password"
                  type={visible ? "text" : "password"}
                  autoComplete={
                    page === "login" ? "current-password" : "new-password"
                  }
                  placeholder="输入你的密码"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  required
                />
                <button
                  type="button"
                  className="icon-button"
                  aria-label={visible ? "隐藏密码" : "显示密码"}
                  onClick={() => setVisible((value) => !value)}
                >
                  {visible ? <EyeOff size={17} /> : <Eye size={17} />}
                </button>
              </span>
            </label>
            {error && (
              <p className="auth-error" role="alert">
                {error}
              </p>
            )}
            <button
              className="button button-primary auth-submit"
              type="submit"
              disabled={loading || !username.trim() || !password.trim()}
            >
              {loading
                ? "正在连接…"
                : page === "login"
                  ? "登录工作空间"
                  : "创建账号"}
              <ArrowRight size={17} />
            </button>
          </form>
          <p className="auth-switch">
            {page === "login" ? "还没有账号？" : "已有账号？"}
            <button
              disabled={loading}
              onClick={() => {
                setPage(page === "login" ? "register" : "login");
                setError("");
                setPassword("");
              }}
            >
              {page === "login" ? "创建账号" : "返回登录"}
            </button>
          </p>
          <div className="auth-trust">
            <span />
            来源可追溯 · 实验可复核 · 成果可交付
          </div>
        </div>
        <footer className="auth-footer">EMNETS-RA · 为严谨的研究而设计</footer>
      </section>
    </div>
  );
}
