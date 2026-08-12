import { useNavigate } from 'react-router-dom'

export default function Dashboard() {
  const navigate = useNavigate()

  return (
    <div className="dashboard">
      <div className="dashboard-hero">
        <h1>Claw AI Lab</h1>
        <p className="dashboard-subtitle">AI-powered research assistant platform</p>
      </div>

      <div className="dashboard-cards">
        <article className="dash-card" onClick={() => navigate('/research-lab')}>
          <div className="dash-card-icon">🔬</div>
          <h2>Research Lab</h2>
          <p>完整的研究自动化管线：从选题、文献检索、idea 生成到实验设计与论文撰写。</p>
          <ul>
            <li>文献检索与综述</li>
            <li>Idea 生成与评估</li>
            <li>实验设计与执行</li>
            <li>论文撰写与修改</li>
          </ul>
          <span className="dash-card-link">进入 Research Lab →</span>
        </article>

        <article className="dash-card" onClick={() => navigate('/auto-review')}>
          <div className="dash-card-icon">📋</div>
          <h2>Auto Review</h2>
          <p>上传论文，从多个维度自动评审：方法论、新颖性、实验有效性、写作质量等。</p>
          <ul>
            <li>上传论文 PDF</li>
            <li>多维度自动评审</li>
            <li>雷达图可视化评分</li>
            <li>导出评审报告</li>
          </ul>
          <span className="dash-card-link">进入 Auto Review →</span>
        </article>
      </div>
    </div>
  )
}
