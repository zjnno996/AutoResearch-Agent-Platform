import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Search, ArrowUpRight, X } from "lucide-react";
import { useWorkspace } from "../contexts/WorkspaceContext";

export default function SearchDialog({ onClose }: { onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const { overview, capabilities } = useWorkspace();
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const node = dialog.current;
    node?.showModal();
    input.current?.focus();
    return () => {
      node?.close();
      previous?.focus();
    };
  }, []);
  const results = useMemo(() => {
    const text = query.trim().toLowerCase();
    const pages = [
      { title: "实验室总览", sub: "工作空间", to: "/dashboard" },
      { title: "科研工作台", sub: "发起研究、检查成果", to: "/research-lab" },
      { title: "学术评审", sub: "论文质量与修改建议", to: "/auto-review" },
      { title: "研究技能库", sub: "浏览已安装技能", to: "/skills" },
      { title: "工作空间设置", sub: "模型、主题与研究流程", to: "/settings" },
    ];
    return [
      ...pages,
      ...(overview?.projects ?? []).map((p) => ({
        title: p.topic || p.projectId,
        sub: "科研项目",
        to: `/research-lab?project=${encodeURIComponent(p.projectId)}`,
      })),
      ...(text.length > 1
        ? (capabilities?.skills ?? [])
            .filter((s) =>
              `${s.name} ${s.description}`.toLowerCase().includes(text),
            )
            .slice(0, 5)
            .map((s) => ({
              title: s.name,
              sub: `研究技能 · ${s.description}`,
              to: `/skills?q=${encodeURIComponent(s.name)}`,
            }))
        : []),
    ]
      .filter((row) => `${row.title} ${row.sub}`.toLowerCase().includes(text))
      .slice(0, 10);
  }, [query, overview, capabilities]);
  return (
    <dialog
      ref={dialog}
      className="command-dialog"
      aria-labelledby="search-title"
      onCancel={onClose}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="command-search">
        <Search size={20} />
        <label id="search-title" className="sr-only" htmlFor="workspace-search">
          搜索工作空间
        </label>
        <input
          id="workspace-search"
          ref={input}
          placeholder="搜索页面、科研项目或技能…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <button className="icon-button" onClick={onClose} aria-label="关闭搜索">
          <X size={18} />
        </button>
      </div>
      <div className="command-results">
        {results.map((row, i) => (
          <Link key={`${row.to}:${i}`} to={row.to} onClick={onClose}>
            <span>
              <strong>{row.title}</strong>
              <small>{row.sub}</small>
            </span>
            <ArrowUpRight size={16} />
          </Link>
        ))}
        {!results.length && (
          <p className="empty-hint">没有找到匹配内容，试试其他关键词。</p>
        )}
      </div>
      <footer>使用 Tab 选择结果，Enter 打开，Esc 关闭</footer>
    </dialog>
  );
}
