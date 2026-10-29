import { Link } from "react-router-dom";
import {
  ArrowRight,
  ArrowUpRight,
  Plus,
  FolderKanban,
  Activity,
  CheckCheck,
  Layers3,
  FlaskConical,
  FileCheck2,
  Clock3,
  RefreshCw,
  BookOpen,
  Compass,
} from "lucide-react";
import { useAuth } from "../contexts/AuthContext";
import { useWorkspace } from "../contexts/WorkspaceContext";
import ResearchOrbit from "../components/ResearchOrbit";

const STATUS: Record<string, string> = {
  running: "研究中",
  queued: "排队中",
  completed: "已完成",
  failed: "待处理",
  interrupted: "已暂停",
  new: "待开始",
};
const PHASES = [
  "任务与证据",
  "方案与校验",
  "代码实现",
  "实验执行",
  "成果报告",
  "独立复核",
];
export default function Dashboard() {
  const { user } = useAuth();
  const { overview, capabilities, status, error, refresh } = useWorkspace();
  const cards = [
    {
      label: "科研项目",
      value: overview?.counts.total,
      caption: "你的研究工作空间",
      icon: FolderKanban,
      color: "blue",
    },
    {
      label: "正在研究",
      value: overview?.counts.running,
      caption: overview
        ? `${overview.counts.queued} 个项目等待运行`
        : "等待项目状态同步",
      icon: Activity,
      color: "cyan",
    },
    {
      label: "已完成项目",
      value: overview?.counts.completed,
      caption: "回顾研究过程与成果",
      icon: CheckCheck,
      color: "green",
    },
    {
      label: "可用研究技能",
      value: capabilities?.skill_count,
      caption: capabilities
        ? `${capabilities.sources.length} 个固定版本开源来源`
        : "正在读取技能目录",
      icon: Layers3,
      color: "violet",
    },
  ];
  return (
    <div className="overview-page">
      <div className="page-heading">
        <div>
          <div className="page-kicker">YOUR RESEARCH, CONNECTED</div>
          <h1>
            实验室总览<span className="heading-dot">.</span>
          </h1>
          <p>{user?.username}，欢迎回来。从一个好问题，开始下一次发现。</p>
        </div>
        <div className="heading-date">
          <Clock3 size={14} />
          {new Date().toLocaleDateString("zh-CN", {
            month: "long",
            day: "numeric",
            weekday: "long",
          })}
        </div>
      </div>
      <section className="research-hero">
        <div className="hero-copy">
          <span className="hero-label">
            <i /> ZHEJIANG UNIVERSITY · EMNETS LAB
          </span>
          <h2>
            让每一个想法，
            <br />
            走向可信的发现。
          </h2>
          <p>
            连接文献、方法与真实实验。
            <br className="mobile-break" />
            在一个工作空间里，推进完整科研流程。
          </p>
          <div className="hero-actions">
            <Link className="button button-white" to="/research-lab?new=1">
              <Plus size={17} /> 开始一项研究 <ArrowUpRight size={16} />
            </Link>
            <Link className="hero-secondary" to="/settings">
              了解研究流程 <ArrowRight size={15} />
            </Link>
          </div>
          <div className="hero-footnote">
            <span>来源可追溯</span>
            <i />
            <span>真实实验执行</span>
            <i />
            <span>独立质量复核</span>
          </div>
        </div>
        <div className="hero-visual">
          <ResearchOrbit />
        </div>
      </section>
      {error && (
        <div className="inline-alert" role="alert">
          {error}
          <button onClick={refresh}>重新获取</button>
        </div>
      )}
      <section className="metric-grid" aria-label="工作空间统计">
        {cards.map(({ label, value, caption, icon: Icon, color }) => (
          <article className="metric-card" key={label}>
            <div className="metric-top">
              <span>{label}</span>
              <span className={`metric-icon ${color}`}>
                <Icon size={18} />
              </span>
            </div>
            <strong className={value === undefined ? "metric-pending" : ""}>
              {value ?? "—"}
            </strong>
            <small>{caption}</small>
          </article>
        ))}
      </section>
      <div className="overview-columns">
        <section className="surface projects-surface">
          <div className="surface-heading">
            <div>
              <h2>最近的研究</h2>
              <p>持续推进你的问题，保留每一步探索。</p>
            </div>
            <div className="surface-heading-actions">
              <button
                className="icon-button"
                onClick={refresh}
                disabled={status !== "online"}
                aria-label="刷新项目"
              >
                <RefreshCw size={16} />
              </button>
              <Link className="text-link" to="/research-lab">
                全部项目 <ArrowUpRight size={14} />
              </Link>
            </div>
          </div>
          {!overview ? (
            <div className="empty-state" role="status">
              <span className="empty-state-icon">
                <FolderKanban size={28} />
              </span>
              <h3>
                {status === "offline" ? "等待连接工作空间" : "正在同步你的项目"}
              </h3>
              <p>
                {status === "offline"
                  ? "服务恢复后会自动重新连接。"
                  : "项目数据由实验室后端实时提供。"}
              </p>
            </div>
          ) : !overview.projects.length ? (
            <div className="empty-state">
              <span className="empty-state-icon">
                <Compass size={30} strokeWidth={1.4} />
              </span>
              <h3>下一次发现，从这里开始</h3>
              <p>创建你的第一个科研项目，让问题、证据和实验有序连接。</p>
              <Link className="button button-primary" to="/research-lab?new=1">
                <Plus size={16} /> 新建科研项目
              </Link>
            </div>
          ) : (
            <div className="project-table-wrap">
              <table className="project-table">
                <thead>
                  <tr>
                    <th>研究项目</th>
                    <th>状态</th>
                    <th>研究进度</th>
                    <th>
                      <span className="sr-only">打开项目</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {overview.projects.slice(0, 5).map((project) => (
                    <tr key={project.projectId}>
                      <td>
                        <Link
                          className="project-title"
                          to={`/research-lab?project=${encodeURIComponent(project.projectId)}`}
                        >
                          <span className="project-table-icon">
                            <FlaskConical size={17} />
                          </span>
                          <span>
                            <strong>{project.topic || "未命名研究"}</strong>
                            <small>
                              {project.lastCompletedName || project.projectId}
                            </small>
                          </span>
                        </Link>
                      </td>
                      <td>
                        <span className={`project-status ${project.status}`}>
                          <i />
                          {STATUS[project.status] ?? project.status}
                        </span>
                      </td>
                      <td>
                        <div className="table-progress">
                          <span>{project.progress}%</span>
                          <div
                            role="progressbar"
                            aria-label="项目进度"
                            aria-valuenow={project.progress}
                            aria-valuemin={0}
                            aria-valuemax={100}
                          >
                            <i style={{ width: `${project.progress}%` }} />
                          </div>
                        </div>
                      </td>
                      <td>
                        <Link
                          className="icon-button"
                          aria-label={`打开项目 ${project.topic}`}
                          to={`/research-lab?project=${encodeURIComponent(project.projectId)}`}
                        >
                          <ArrowUpRight size={17} />
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="surface-footer">
            <span className={`service-indicator ${status}`}>
              <i />
              {status === "online" ? "已连接项目服务" : "等待服务连接"}
            </span>
            <span>
              {overview
                ? `最近同步 ${new Date(overview.updatedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`
                : "数据尚未同步"}
            </span>
          </div>
        </section>
        <aside className="overview-rail">
          <section className="surface quick-launch">
            <div className="surface-heading">
              <h2>快捷开始</h2>
              <span className="small-label">QUICK START</span>
            </div>
            {[
              {
                to: "/research-lab?new=1",
                icon: FlaskConical,
                label: "开展一项研究",
                desc: "从问题到代码、实验与报告",
                color: "blue",
              },
              {
                to: "/auto-review",
                icon: FileCheck2,
                label: "评审一篇论文",
                desc: "多维度检查与改进建议",
                color: "cyan",
              },
              {
                to: "/skills",
                icon: BookOpen,
                label: "探索研究技能",
                desc: "发现适合当前阶段的方法",
                color: "violet",
              },
            ].map(({ to, icon: Icon, label, desc, color }) => (
              <Link key={to} className="quick-launch-item" to={to}>
                <span className={`metric-icon ${color}`}>
                  <Icon size={19} />
                </span>
                <span>
                  <strong>{label}</strong>
                  <small>{desc}</small>
                </span>
                <ArrowUpRight size={16} />
              </Link>
            ))}
          </section>
          <div className="lab-note">
            <span className="lab-note-index">THE EMNETS WAY</span>
            <h3>求是，而后创新。</h3>
            <p>
              以真实证据支撑结论，
              <br />
              让每一步探索都可复核。
            </p>
            <span className="lab-note-signature">
              EMNETS LAB <span>↗</span>
            </span>
          </div>
        </aside>
      </div>
      <section className="surface workflow-surface">
        <div className="surface-heading">
          <div>
            <h2>一条完整的科研路径</h2>
            <p>六个阶段，连接探索、执行与交付。</p>
          </div>
          <Link className="text-link" to="/settings">
            查看工作流 <ArrowUpRight size={14} />
          </Link>
        </div>
        <div className="workflow-track">
          {PHASES.map((label, i) => (
            <div className="workflow-step" key={label}>
              <span className="workflow-number">0{i + 1}</span>
              <strong>{label}</strong>
              <small>
                {
                  [
                    "梳理问题与原始来源",
                    "设计方法与验证条件",
                    "生成可执行研究代码",
                    "运行实验并保留证据",
                    "汇总结果与研究报告",
                    "检查方法、证据和结论",
                  ][i]
                }
              </small>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
