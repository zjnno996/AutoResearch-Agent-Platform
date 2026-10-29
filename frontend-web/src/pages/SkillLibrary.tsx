import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Search,
  SlidersHorizontal,
  ArrowUpRight,
  Sparkles,
  Layers3,
  X,
} from "lucide-react";
import { useWorkspace } from "../contexts/WorkspaceContext";

const PHASES = [
  { stage: 8, label: "任务与证据" },
  { stage: 9, label: "研究方案" },
  { stage: 13, label: "代码实现" },
  { stage: 18, label: "实验执行" },
  { stage: 25, label: "成果报告" },
  { stage: 26, label: "独立复核" },
];
const CATEGORY: Record<string, string> = {
  science: "科研方法",
  engineering: "工程质量",
  delivery: "交付与界面",
};
export default function SkillLibrary() {
  const {
    capabilities: catalog,
    status,
    previewSkills,
    error,
  } = useWorkspace();
  const [params] = useSearchParams();
  const [query, setQuery] = useState(params.get("q") ?? "");
  const [category, setCategory] = useState("all");
  const [topic, setTopic] = useState("");
  const [stage, setStage] = useState(9);
  const [limit, setLimit] = useState(18);
  useEffect(() => setQuery(params.get("q") ?? ""), [params]);
  useEffect(() => setLimit(18), [query, category]);
  const visible = useMemo(() => {
    const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    return (catalog?.skills ?? []).filter(
      (skill) =>
        (category === "all" || skill.category === category) &&
        words.every((word) =>
          `${skill.id} ${skill.name} ${skill.description} ${CATEGORY[skill.category] ?? ""}`
            .toLowerCase()
            .includes(word),
        ),
    );
  }, [catalog, query, category]);
  return (
    <div className="skills-page">
      <div className="page-heading">
        <div>
          <div className="page-kicker">METHODS THAT MOVE RESEARCH FORWARD</div>
          <h1>
            研究技能库<span className="heading-dot">.</span>
          </h1>
          <p>把可靠的方法，放进正确的研究阶段。</p>
        </div>
        <div className="catalog-count">
          <Layers3 size={19} />
          <strong>{catalog?.skill_count ?? "—"}</strong>
          <span>项已安装技能</span>
        </div>
      </div>
      {error && (
        <p className="inline-alert" role="alert">
          {error}
        </p>
      )}
      <section className="surface skill-match">
        <div className="skill-match-heading">
          <span className="section-icon">
            <Sparkles size={20} />
          </span>
          <div>
            <h2>为你的研究找到合适的方法</h2>
            <p>
              按问题与阶段匹配，每阶段最多 {catalog?.max_skills_per_stage ?? 3}{" "}
              项技能。预览不会调用模型。
            </p>
          </div>
        </div>
        <div className="skill-match-controls">
          <label>
            研究问题
            <input
              value={topic}
              maxLength={8000}
              onChange={(event) => setTopic(event.target.value)}
              placeholder="例如：检查物理模拟的单位与数值稳定性"
            />
          </label>
          <label>
            研究阶段
            <select
              aria-label="研究阶段"
              value={stage}
              onChange={(event) => setStage(Number(event.target.value))}
            >
              {PHASES.map((phase) => (
                <option key={phase.stage} value={phase.stage}>
                  {phase.label}
                </option>
              ))}
            </select>
          </label>
          <button
            className="button button-primary"
            disabled={status !== "online"}
            onClick={() => previewSkills(topic, stage)}
          >
            <Sparkles size={15} />
            预览技能匹配
          </button>
        </div>
        <div className="matched-skills">
          {catalog?.preview.map((id) => (
            <span key={id}>
              <Layers3 size={13} />
              {id}
            </span>
          ))}
        </div>
      </section>
      <div className="skill-catalog-toolbar">
        <div className="catalog-tabs" role="group" aria-label="技能分类">
          {[["all", "全部技能"], ...Object.entries(CATEGORY)].map(
            ([value, label]) => (
              <button
                key={value}
                className={category === value ? "active" : ""}
                onClick={() => setCategory(value)}
                aria-pressed={category === value}
              >
                {label}
              </button>
            ),
          )}
        </div>
        <label className="catalog-search">
          <Search size={17} />
          <span className="sr-only">搜索已安装技能</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索名称、领域或方法…"
          />
          {query && (
            <button
              className="icon-button"
              onClick={() => setQuery("")}
              aria-label="清空技能搜索"
            >
              <X size={14} />
            </button>
          )}
        </label>
      </div>
      <div className="catalog-results-line">
        <span>
          {catalog ? `${visible.length} 项匹配技能` : "正在读取技能目录…"}
        </span>
        <span>
          <SlidersHorizontal size={13} /> 按研究阶段自动路由
        </span>
      </div>
      <div className="skill-catalog-grid">
        {visible.slice(0, limit).map((skill) => (
          <article className="catalog-skill-card" key={skill.id}>
            <div className="catalog-skill-top">
              <span className="metric-icon blue">
                <Layers3 size={19} />
              </span>
              <span className="category-tag">
                {CATEGORY[skill.category] ?? skill.category}
              </span>
            </div>
            <h2>{skill.name}</h2>
            <p title={skill.description}>{skill.description}</p>
            <footer>
              <span>
                {skill.source} · {skill.license ?? "本地技能"}
              </span>
              {skill.repository && (
                <a
                  href={`${skill.repository}/tree/${skill.revision}`}
                  target="_blank"
                  rel="noreferrer"
                  aria-label={`查看 ${skill.name} 的开源来源`}
                >
                  <ArrowUpRight size={16} />
                </a>
              )}
            </footer>
          </article>
        ))}
      </div>
      {!visible.length && catalog && (
        <div className="empty-state">
          <Search size={30} />
          <h3>没有找到匹配的技能</h3>
          <p>试试领域名称，或使用英文关键词搜索。</p>
          <button
            className="button button-outline"
            onClick={() => {
              setQuery("");
              setCategory("all");
            }}
          >
            清除筛选
          </button>
        </div>
      )}
      {visible.length > limit && (
        <div className="load-more">
          <button
            className="button button-outline"
            onClick={() => setLimit((value) => value + 18)}
          >
            加载更多技能{" "}
            <span>
              {Math.min(limit, visible.length)} / {visible.length}
            </span>
          </button>
        </div>
      )}
      <details className="surface skill-provenance">
        <summary>
          开源来源与固定版本{" "}
          <span>{catalog?.sources.length ?? "—"} 个来源</span>
        </summary>
        <div>
          {catalog?.sources.map((source) => (
            <a
              key={source.directory}
              href={`${source.repository}/tree/${source.revision}`}
              target="_blank"
              rel="noreferrer"
            >
              <span>
                <strong>{source.directory}</strong>
                <small>
                  {source.license} · {source.revision.slice(0, 10)}
                </small>
              </span>
              <span>
                {source.skill_count} 项 <ArrowUpRight size={14} />
              </span>
            </a>
          ))}
        </div>
      </details>
    </div>
  );
}
