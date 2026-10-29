import { useEffect, useState } from "react";
import {
  Monitor,
  Moon,
  Sun,
  ShieldCheck,
  Cpu,
  Download,
  ArrowUpRight,
} from "lucide-react";
import { useTheme } from "../contexts/ThemeContext";
import { useWorkspace } from "../contexts/WorkspaceContext";

export default function Settings() {
  const { theme, toggle } = useTheme();
  const { status, capabilities, overview } = useWorkspace();
  const [review, setReview] = useState("checking");
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    fetch("/api/health", { signal: controller.signal })
      .then((response) => {
        if (active) setReview(response.ok ? "online" : "offline");
      })
      .catch(() => {
        if (active) setReview("offline");
      });
    return () => {
      active = false;
      clearTimeout(timer);
      controller.abort();
    };
  }, []);
  const download = () => {
    if (!overview) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(overview, null, 2)], {
        type: "application/json",
      }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = "emnets-workspace.json";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <div className="settings-page">
      <div className="page-heading">
        <div>
          <div className="page-kicker">YOUR WORKSPACE, YOUR WAY</div>
          <h1>
            工作空间设置<span className="heading-dot">.</span>
          </h1>
          <p>查看服务状态、研究配置与工作空间偏好。</p>
        </div>
      </div>
      <div className="settings-grid">
        <section className="surface settings-card">
          <div className="section-icon">
            <Cpu size={20} />
          </div>
          <h2>研究引擎</h2>
          <p>统一的模型配置，连接完整的研究流程。</p>
          <dl className="settings-facts">
            <div>
              <dt>研究模型</dt>
              <dd>{capabilities?.model || "等待服务同步"}</dd>
            </div>
            <div>
              <dt>模型策略</dt>
              <dd>
                {capabilities
                  ? capabilities.model_locked
                    ? "统一模型 · 已锁定"
                    : "由后端配置管理"
                  : "—"}
              </dd>
            </div>
            <div>
              <dt>可用技能</dt>
              <dd>
                {capabilities
                  ? `${capabilities.skill_count} 项 / ${capabilities.sources.length} 个来源`
                  : "—"}
              </dd>
            </div>
          </dl>
          <div className="settings-note">
            <ShieldCheck size={16} />
            <span>模型凭据保存在后端，工作空间不会展示 API Key。</span>
          </div>
        </section>
        <section className="surface settings-card">
          <div className="section-icon">
            <Monitor size={20} />
          </div>
          <h2>界面与服务</h2>
          <p>选择适合你当前工作环境的显示方式。</p>
          <div className="theme-options" role="group" aria-label="显示主题">
            {[
              { value: "light", label: "浅色工作空间", icon: Sun },
              { value: "dark", label: "深色工作空间", icon: Moon },
            ].map(({ value, label, icon: Icon }) => (
              <button
                key={value}
                className={theme === value ? "selected" : ""}
                aria-pressed={theme === value}
                onClick={() => {
                  if (theme !== value) toggle();
                }}
              >
                <Icon size={20} />
                {label}
              </button>
            ))}
          </div>
          <div className="service-list">
            {[
              { label: "研究与项目服务", state: status },
              { label: "学术评审服务", state: review },
              {
                label: "研究技能目录",
                state: capabilities ? "online" : "checking",
              },
            ].map((item) => (
              <div key={item.label}>
                <span>{item.label}</span>
                <span className={`service-indicator ${item.state}`}>
                  <i />
                  {item.state === "online"
                    ? "已连接"
                    : item.state === "offline"
                      ? "未连接"
                      : "连接中"}
                </span>
              </div>
            ))}
          </div>
        </section>
      </div>
      <section className="surface settings-card workflow-guide">
        <span className="page-kicker">THE RESEARCH WORKFLOW</span>
        <h2>让每一步研究都有清晰的下一步</h2>
        <div className="guide-grid">
          {[
            [
              "01",
              "任务与证据",
              "梳理原始问题、输入数据与可靠文献，建立可追溯的研究依据。",
            ],
            [
              "02",
              "方案与校验",
              "明确科学方法、实验条件与验证标准，独立检查研究设计。",
            ],
            ["03", "代码实现", "将研究方案转为可运行的代码，绑定数据与方法。"],
            ["04", "实验执行", "开展真实实验，保留日志、输出与修复过程。"],
            ["05", "成果报告", "汇总图表、结果和限制，形成可检查的研究报告。"],
            [
              "06",
              "独立复核",
              "检查证据是否支持结论，定位缺口并返回相应阶段。",
            ],
          ].map(([number, title, description]) => (
            <article key={number}>
              <span>{number}</span>
              <h3>{title}</h3>
              <p>{description}</p>
            </article>
          ))}
        </div>
      </section>
      <section className="surface workspace-export">
        <div>
          <h2>你的工作空间概览</h2>
          <p>导出当前账号的项目清单与阶段状态，便于归档和协作。</p>
        </div>
        <button
          className="button button-outline"
          onClick={download}
          disabled={!overview}
        >
          <Download size={16} />
          导出概览 JSON
        </button>
      </section>
      <p className="settings-lab-link">
        EMNETS Lab · Zhejiang University{" "}
        <a href="https://www.emnets.cn/" target="_blank" rel="noreferrer">
          实验室官网 <ArrowUpRight size={14} />
        </a>
      </p>
    </div>
  );
}
