import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useAuth } from "./AuthContext";

export type ProjectSummary = {
  projectId: string;
  topic: string;
  status: string;
  timestamp?: string;
  progress: number;
  workflow?: string;
  completedPhases: string[];
  lastCompletedName?: string;
};
export type WorkspaceOverview = {
  updatedAt: string;
  projects: ProjectSummary[];
  counts: Record<string, number>;
  phaseCounts: Record<string, number>;
};
export type Skill = {
  id: string;
  name: string;
  description: string;
  source: string;
  sha256: string;
  repository?: string;
  revision?: string;
  license?: string;
  category: string;
  stages: number[];
};
export type Capabilities = {
  model: string;
  model_locked: boolean;
  skill_count: number;
  max_skills_per_stage: number;
  preview: string[];
  sources: {
    directory: string;
    repository: string;
    revision: string;
    license: string;
    skill_count: number;
  }[];
  skills: Skill[];
  phases: { id: string; stage: number }[];
};
type Workspace = {
  status: "connecting" | "online" | "offline";
  overview: WorkspaceOverview | null;
  capabilities: Capabilities | null;
  error: string;
  refresh: () => void;
  previewSkills: (topic: string, stage: number) => void;
};
const Context = createContext<Workspace | null>(null);
export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const { token, logout } = useAuth();
  const socket = useRef<WebSocket | null>(null);
  const [status, setStatus] = useState<Workspace["status"]>("connecting");
  const [overview, setOverview] = useState<WorkspaceOverview | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [error, setError] = useState("");
  const send = useCallback((data: object) => {
    if (socket.current?.readyState === WebSocket.OPEN)
      socket.current.send(JSON.stringify(data));
  }, []);
  const refresh = useCallback(
    () => send({ command: "get_workspace_overview" }),
    [send],
  );
  const previewSkills = useCallback(
    (topic: string, stage: number) =>
      send({ command: "get_research_capabilities", topic, stage }),
    [send],
  );
  useEffect(() => {
    let disposed = false;
    let retry: ReturnType<typeof setTimeout>;
    let invalidate: ReturnType<typeof setTimeout>;
    let attempts = 0;
    let authenticated = false;
    const connect = () => {
      if (disposed || !token) return;
      authenticated = false;
      setStatus("connecting");
      const ws = new WebSocket(
        `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/agents`,
      );
      socket.current = ws;
      ws.onopen = () => ws.send(JSON.stringify({ command: "auth", token }));
      ws.onmessage = (event) => {
        if (disposed) return;
        try {
          const message = JSON.parse(event.data);
          if (message.type === "auth_result") {
            if (!message.payload.ok) {
              logout();
              return;
            }
            authenticated = true;
            attempts = 0;
            setStatus("online");
            setError("");
            ws.send(JSON.stringify({ command: "get_workspace_overview" }));
            ws.send(JSON.stringify({ command: "get_research_capabilities" }));
          } else if (message.type === "workspace_overview")
            setOverview(message.payload);
          else if (message.type === "project_list" && authenticated) {
            clearTimeout(invalidate);
            invalidate = setTimeout(refresh, 150);
          }
          else if (message.type === "research_capabilities")
            setCapabilities(message.payload);
          else if (message.type.endsWith("_error") && message.payload?.message)
            setError(message.payload.message);
        } catch {
          setError("服务响应暂时不可用，请刷新重试。");
        }
      };
      ws.onerror = () => ws.close();
      ws.onclose = () => {
        if (disposed) return;
        authenticated = false;
        setStatus("offline");
        retry = setTimeout(connect, Math.min(1500 * 2 ** attempts++, 15000));
      };
    };
    connect();
    const poll = setInterval(() => {
      if (authenticated) refresh();
    }, 30000);
    return () => {
      disposed = true;
      clearTimeout(retry);
      clearTimeout(invalidate);
      clearInterval(poll);
      socket.current?.close();
    };
  }, [token, logout, refresh]);
  return (
    <Context.Provider
      value={{ status, overview, capabilities, error, refresh, previewSkills }}
    >
      {children}
    </Context.Provider>
  );
}
export function useWorkspace() {
  const context = useContext(Context);
  if (!context) throw new Error("WorkspaceProvider is required");
  return context;
}
