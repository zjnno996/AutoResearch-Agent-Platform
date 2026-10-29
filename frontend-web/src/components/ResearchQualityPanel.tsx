type Artifact = { filename: string; content?: string; stage?: number }
type SkillUsage = { id?: string; name: string; repository?: string; revision?: string; resources?: unknown[] }
type MethodWitnesses = { status?: string; pilot?: { target_call_counts?: Record<string, number> }; observe?: { target_call_counts?: Record<string, number> } }
type Criterion = { criterion_id: string; requirement: string; status: string; evidence_gap?: string }
type Verification = {
  all_satisfied?: boolean
  induction_status?: string
  summary?: { passed?: number; failed?: number; pending?: number }
  criteria?: Criterion[]
  dimensions?: Record<string, { status?: string; reason?: string }>
}
type Engine = {
  skill_count?: number
  profile?: string
  method?: { study_type?: string; obligations?: string[] }
  stages?: Record<string, { skills?: string[]; skill_records?: SkillUsage[] }>
  workflow?: string
  model_calls?: { attempts?: number; limit?: number }
}
const dimensionNames: Record<string, string> = {
  task_fidelity: '任务覆盖', method_validity: '方法与推导', source_grounding: '来源依据',
  real_execution: '真实执行', measurement_validity: '测量与统计', figure_semantics: '图表含义',
  claim_integrity: '结论依据', reproducibility: '可复现性',
}
const reviewStatus: Record<string, string> = { pass: '通过', gap: '有缺口', unverified: '待核实', not_applicable: '不适用' }
const methodNames: Record<string, string> = {
  comparison: '对比实验', reproduction: '复现研究', observational: '观测研究',
  numerical: '数值研究', formal: '理论与证明',
}

function parse<T>(artifacts: Artifact[], filename: string): T | null {
  const artifact = artifacts.filter(item => item.filename === filename && item.content)
    .sort((a, b) => (b.stage ?? 0) - (a.stage ?? 0))[0]
  if (!artifact?.content) return null
  try {
    return JSON.parse(artifact.content) as T
  } catch {
    return null
  }
}

export default function ResearchQualityPanel({ artifacts, downloadUrl }: {
  artifacts: Artifact[]
  downloadUrl: (filename: string) => string
}) {
  const verification = parse<Verification>(artifacts, 'research_verification.json')
  const engine = parse<Engine>(artifacts, 'research_engine_summary.json')
  const witnesses = parse<MethodWitnesses>(artifacts, 'method_probe_results.json')
  if (!verification && !engine) return null
  const summary = verification?.summary
  const skills = [...new Set(Object.values(engine?.stages ?? {}).flatMap(stage => stage.skills ?? []))]
  const skillRecords = [...new Map(Object.values(engine?.stages ?? {}).flatMap(stage => stage.skill_records ?? [])
    .map(row => [(row.id ?? row.name) + (row.revision ?? ''), row])).values()]
  const unmet = (verification?.criteria ?? []).filter(item => item.status === 'unmet')
  const method = engine?.method

  return (
    <section className="artifact-section focus-card context-card research-quality-panel">
      <div className="panel-heading">
        <h3>研究要求与证据</h3>
        <span>{verification?.all_satisfied ? '要求已满足' : '质量优先'}</span>
      </div>
      <div className="experiment-status-grid">
        <span className="evidence-pill evidence-ok">已满足 {summary?.passed ?? 0}</span>
        <span className={(summary?.failed ?? 0) > 0 ? 'evidence-pill evidence-warn' : 'evidence-pill'}>待修复 {summary?.failed ?? 0}</span>
        <span className="evidence-pill">待后续核验 {summary?.pending ?? 0}</span>
        <span className="evidence-pill">技能库 {engine?.skill_count ?? 0} 项</span>
        {engine?.model_calls && <span className="evidence-pill">模型请求 {engine.model_calls.attempts ?? 0}/{engine.model_calls.limit === 0 ? '不限额' : engine.model_calls.limit ?? 48}</span>}
      </div>
      <p className="research-quality-note">先核验任务要求、真实实验和结论，再比较耗时与 token。此处为内部证据检查，科研效果提升仍需独立评测。</p>
      {verification?.dimensions && (
        <details>
          <summary>逐项评审依据</summary>
          {Object.entries(verification.dimensions).map(([name, row]) => (
            <p className="research-quality-note" key={name}>
              <strong>{dimensionNames[name] ?? name} · {reviewStatus[row.status ?? ''] ?? '待核实'}</strong>：{row.reason || '尚无评审依据'}
            </p>
          ))}
        </details>
      )}
      {method?.study_type && (
        <details>
          <summary>当前方法：{methodNames[method.study_type] ?? method.study_type}</summary>
          <p className="research-quality-note">方法选择会增加专门的检查要求，所有明确的任务目标都会保留。完整策略与参考来源可在研究方法记录中查看。</p>
        </details>
      )}
      {verification && verification.induction_status !== 'completed' && (
        <div className="evidence-warning">任务专属标准尚未生成成功，当前检查不能确认已覆盖全部研究要求。</div>
      )}
      {unmet.length > 0 && (
        <div className="research-quality-gaps">
          {unmet.map(item => (
            <div className="research-quality-gap" key={item.criterion_id}>
              <strong>{item.requirement}</strong>
              <p>{item.evidence_gap}</p>
            </div>
          ))}
        </div>
      )}
      {skills.length > 0 && (
        <details>
          <summary>本次已使用的科研技能（{skills.length}）</summary>
          <p className="research-quality-note">{skills.join(' · ')}</p>
          {skillRecords.map(row => <p className="research-quality-note" key={(row.id ?? row.name) + row.revision}>
            {row.repository ? <a href={`${row.repository}/tree/${row.revision}`} target="_blank" rel="noreferrer">{row.id ?? row.name}</a> : row.name}
            {row.revision && <> · {row.revision.slice(0, 10)}</>}
            {!!row.resources?.length && <> · 已加载 {row.resources.length} 份参考资料</>}
          </p>)}
        </details>
      )}
      {witnesses && <details>
        <summary>方法局部检查：{witnesses.status === 'passed' ? '通过，已记录正式运行调用' : '未提供适用的可执行检查'}</summary>
        <p className="research-quality-note">解析例子和不变性检查用于发现指定错误，不能代替数学证明或完整的方法核验。</p>
      </details>}
      <div className="context-card-export">
        <a className="export-btn" href={downloadUrl('research_rubric.json')} target="_blank" rel="noreferrer">研究要求</a>
        <a className="export-btn" href={downloadUrl('research_verification.json')} target="_blank" rel="noreferrer">逐项核验</a>
        <a className="export-btn" href={downloadUrl('research_engine_summary.json')} target="_blank" rel="noreferrer">技能与执行记录</a>
        {method && <a className="export-btn" href={downloadUrl('research_method_plan.json')} target="_blank" rel="noreferrer">研究方法与来源</a>}
      </div>
    </section>
  )
}
