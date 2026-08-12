import { useState, useRef, useMemo, useEffect, useCallback } from 'react'
import html2canvas from 'html2canvas'
import jsPDF from 'jspdf'

const STANDARD_REVIEW_DIMENSIONS = [
  { id: 'methodology', label: '方法论严谨性', labelEn: 'Methodological Rigor', desc: '研究设计、假设检验、统计方法的合理性', descEn: 'Research design, hypothesis testing, statistical rigor' },
  { id: 'novelty', label: '新颖性评估', labelEn: 'Novelty Assessment', desc: '与现有工作的区分度和创新点', descEn: 'Differentiation from prior work and innovation' },
  { id: 'experiment', label: '实验有效性', labelEn: 'Experimental Validity', desc: '基准选择、消融实验、结果可靠性', descEn: 'Baselines, ablation, result reliability' },
  { id: 'writing', label: '写作质量', labelEn: 'Writing Quality', desc: '结构清晰度、论证逻辑、语言表达', descEn: 'Clarity, argumentation, language' },
  { id: 'related_work', label: '相关工作覆盖', labelEn: 'Related Work Coverage', desc: '文献引用的充分性和定位准确性', descEn: 'Citation adequacy and positioning' },
  { id: 'reproducibility', label: '可复现性', labelEn: 'Reproducibility', desc: '代码、数据、随机种子、实验配置', descEn: 'Code, data, seeds, configuration' },
  { id: 'ethics', label: '伦理考量', labelEn: 'Ethics', desc: '数据隐私、偏见、社会影响', descEn: 'Privacy, bias, societal impact' },
  { id: 'skeptic', label: '批判性假设检查', labelEn: 'Skeptic Review', desc: '隐藏假设、替代解释、边界条件与结论外推', descEn: 'Hidden assumptions, alternative explanations' },
] as const

const THESIS_REVIEW_DIMENSIONS = [
  { id: 'writing_format', label: '格式与排版规范', labelEn: 'Writing Format', desc: '图表、公式、缩写、参考文献与章节编号', descEn: 'Figures, formulas, references, numbering' },
  { id: 'structure_logic', label: '结构与布局', labelEn: 'Structure & Logic', desc: '章节递进、逻辑链、内容重复与页面组织', descEn: 'Section progression, logic flow' },
  { id: 'theory_depth', label: '理论深度', labelEn: 'Theoretical Depth', desc: '理论基础、复杂度、收敛性与数学推导', descEn: 'Theory foundation, complexity, convergence' },
] as const

const REVIEW_DIMENSIONS = [...STANDARD_REVIEW_DIMENSIONS, ...THESIS_REVIEW_DIMENSIONS] as const

type DimensionId = typeof REVIEW_DIMENSIONS[number]['id']

type ReviewResult = {
  dimensionId: DimensionId
  score: number
  summary: string
  strengths: string[]
  weaknesses: string[]
  suggestions: string[]
  generatedWeaknessCount?: number
  generatedSuggestionCount?: number
  verifiedWeaknessCount?: number
  verifiedSuggestionCount?: number
  filteredLowConfidenceCount?: number
  candidateWeaknesses?: string[]
  candidateSuggestions?: string[]
  allFindingDetails?: TopIssue[]
  diagnostic_only?: boolean
}

type ModelOption = {
  value: string
  label: string
  baseUrl: string
}

type Phase = 'upload' | 'reviewing' | 'results'
type DimStatus = 'pending' | 'running' | 'done'

type ReviewHistoryItem = {
  id: string
  fileName: string
  model: string
  overallScore: number
  dimensionCount: number
  timestamp: string
}

type Severity = 'critical' | 'major' | 'minor'

type KeyFindingItem = {
  text: string
  severity?: Severity
  dimensionId: string
  dimScore?: number
}

type KeyFindings = {
  weaknesses: KeyFindingItem[]
  suggestions: KeyFindingItem[]
}

type TopIssue = {
  candidate_id: string
  text: string
  dimension: string
  suggestion?: string
  severity: Severity
  evidence?: string
  evidence_confidence: number
  priority_score: number
  verdict?: 'supported' | 'uncertain' | 'contradicted'
  source_count?: number
  dataset_prior?: number
  debate?: Record<string, unknown>
  issue_category?: string
  reason?: string
  claim_impact?: number
  fixability?: number
  counterfactual?: string
  counterfactual_impact?: string
}

type CategorizedItem = { text: string; dimension: string }
type CategorizedFinding = {
  id: string
  label: string
  dimensions: string[]
  score: number
  confidence: number
  confidenceLevel: 'high' | 'medium' | 'low'
  confidenceBasis: string
  summaries: CategorizedItem[]
  strengths: CategorizedItem[]
  weaknesses: CategorizedItem[]
  suggestions: CategorizedItem[]
  topIssues: TopIssue[]
}

type ConfidenceSummary = {
  overall: number
  level: 'high' | 'medium' | 'low'
  issueBands: { high: number; medium: number; low: number }
  verdicts: { supported: number; uncertain: number; contradicted: number }
  evidenceUnits: number
  visualEvidenceUsed: boolean
  visualPages: number[]
  visualRegions: number
  visualCoverageMode: string
  visualCoverageComplete: boolean
  visualFailedPages: number[]
  debatesTriggered: number
  hybridDebateAgents?: number
  hybridDebateRoles?: string[]
  note: string
}

type ReportTextItem = {
  text: string
  dimensionId?: string
  dimension?: string
  severity?: Severity
  confidence?: number
  priorityScore?: number
  evidence?: string
}

type PriorityAction = {
  issue: string
  suggestion?: string
  dimensionId?: string
  severity?: Severity | string
  confidence?: number
  priorityScore?: number
  evidence?: string
  claimImpact?: number
  fixability?: number
  counterfactual?: string
  counterfactualImpact?: string
}

type ReportDimension = {
  dimensionId: string
  label?: string
  score: number
  summary: string
  strengths: ReportTextItem[]
  weaknesses: ReportTextItem[]
  suggestions: ReportTextItem[]
  generatedWeaknessCount?: number
  generatedSuggestionCount?: number
  verifiedWeaknessCount?: number
  verifiedSuggestionCount?: number
  filteredLowConfidenceCount?: number
  candidateWeaknesses?: ReportTextItem[]
  candidateSuggestions?: ReportTextItem[]
  findings?: TopIssue[]
  allFindings?: TopIssue[]
  filterReasons?: Record<string, number>
  diagnosticOnly?: boolean
}

type ConfidenceFilter = 'formal' | 'all' | 'high' | 'medium' | 'low'

type ModificationTask = {
  id: string
  title: string
  location: string
  goal: string
  action: string
  needsExperiment: boolean
  acceptanceCriteria: string
  priorityScore: number
  confidence: number
}

type ReportSummary = {
  overallComment: string
  overallScore?: number
  strengths: ReportTextItem[]
  weaknesses: ReportTextItem[]
  suggestions: ReportTextItem[]
  findings?: TopIssue[]
  priorityActions: PriorityAction[]
  modificationTasks?: ModificationTask[]
  filterSummary?: {
    filteredCount: number
    minConfidence: number
    borderlineReverified: number
    borderlineRecovered: number
    patternRecallCandidates: number
    documentLintCandidates?: number
    documentLintRetained?: number
    documentLintRuleCounts?: Record<string, number>
    expertLensCandidates?: number
    expertLensCounts?: Record<string, number>
    debatesTriggered?: number
    hybridDebateAgents?: number
    absenceClaimsCalibrated?: number
    riskAdjustedThresholds?: Record<string, number>
    issueCategoryCounts?: Record<string, number>
    filteredReasonCounts?: Record<string, number>
    suggestionsReplaced?: number
  }
  dimensions: ReportDimension[]
}

const MAX_FILE_SIZE_MB = 40
const DIM_LABEL_MAP: Record<string, string> = {}
const DIM_LABEL_MAP_EN: Record<string, string> = {}
REVIEW_DIMENSIONS.forEach((d) => {
  DIM_LABEL_MAP[d.id] = d.label
  DIM_LABEL_MAP_EN[d.id] = d.labelEn
})

type ReviewMode = 'thesis' | 'paper'

function RadarChart({ results }: { results: ReviewResult[] }) {
  const size = 200
  const cx = size / 2
  const cy = size / 2
  const radius = 80
  const angleStep = (2 * Math.PI) / results.length

  const gridLevels = [0.25, 0.5, 0.75, 1.0]
  const polygonPoints = (value: number) =>
    results
      .map((_, i) => {
        const angle = angleStep * i - Math.PI / 2
        const r = radius * value
        return `${cx + r * Math.cos(angle)},${cy + r * Math.sin(angle)}`
      })
      .join(' ')

  const dataPoints = results
    .map((r, i) => {
      const angle = angleStep * i - Math.PI / 2
      const v = r.score / 100
      return `${cx + radius * v * Math.cos(angle)},${cy + radius * v * Math.sin(angle)}`
    })
    .join(' ')

  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      {gridLevels.map((level) => (
        <polygon key={level} points={polygonPoints(level)} fill="none" stroke="#dbe1ea" strokeWidth={1} />
      ))}
      {results.map((_, i) => {
        const angle = angleStep * i - Math.PI / 2
        const x = cx + radius * Math.cos(angle)
        const y = cy + radius * Math.sin(angle)
        return <line key={i} x1={cx} y1={cy} x2={x} y2={y} stroke="#dbe1ea" strokeWidth={1} />
      })}
      <polygon points={dataPoints} fill="rgba(37, 99, 235, 0.2)" stroke="#2563eb" strokeWidth={2} />
      {results.map((r, i) => {
        const angle = angleStep * i - Math.PI / 2
        const v = r.score / 100
        const x = cx + radius * v * Math.cos(angle)
        const y = cy + radius * v * Math.sin(angle)
        return <circle key={i} cx={x} cy={y} r={4} fill="#2563eb" />
      })}
    </svg>
  )
}

function DimProgress({ label, status }: { label: string; status: DimStatus }) {
  const icons: Record<DimStatus, string> = {
    pending: '⏳',
    running: '🔄',
    done: '✅',
  }
  return (
    <div className={`review-progress-dim ${status}`}>
      <span className="review-progress-dim-icon">{icons[status]}</span>
      <span className="review-progress-dim-label">{label}</span>
    </div>
  )
}

/** Get dimension label based on current mode. */
function dimLabel(id: string, mode: ReviewMode): string {
  return mode === 'paper' ? (DIM_LABEL_MAP_EN[id] || id) : (DIM_LABEL_MAP[id] || id)
}

const itemText = (item: ReportTextItem | string | undefined) =>
  typeof item === 'string' ? item : (item?.text || '')

const itemDimension = (item: ReportTextItem | undefined, mode: ReviewMode) => {
  const id = item?.dimensionId || item?.dimension || ''
  return id ? dimLabel(id, mode) : ''
}

const hasReportItems = (items?: ReportTextItem[]) =>
  (items || []).some((item) => itemText(item).trim())

const toReportDimension = (result: ReviewResult, mode: ReviewMode): ReportDimension => ({
  dimensionId: result.dimensionId,
  label: dimLabel(result.dimensionId, mode),
  score: result.score,
  summary: result.summary,
  strengths: result.strengths.map((text) => ({ text })),
  weaknesses: result.weaknesses.map((text) => ({ text })),
  suggestions: result.suggestions.map((text) => ({ text })),
  generatedWeaknessCount: result.generatedWeaknessCount,
  generatedSuggestionCount: result.generatedSuggestionCount,
  verifiedWeaknessCount: result.verifiedWeaknessCount,
  verifiedSuggestionCount: result.verifiedSuggestionCount,
  filteredLowConfidenceCount: result.filteredLowConfidenceCount,
  candidateWeaknesses: (result.candidateWeaknesses || []).map((text) => ({ text })),
  candidateSuggestions: (result.candidateSuggestions || []).map((text) => ({ text })),
  allFindings: result.allFindingDetails || [],
  diagnosticOnly: result.diagnostic_only,
})

const sameDimension = (left?: string, right?: string) =>
  !!left && !!right && left.toLowerCase() === right.toLowerCase()

const makeReportItem = (
  text: string,
  dimensionId: string,
  extra: Partial<ReportTextItem> = {},
): ReportTextItem => ({
  text,
  dimensionId,
  ...extra,
})

const appendUniqueReportItem = (items: ReportTextItem[], item: ReportTextItem, limit = 8) => {
  const text = itemText(item).trim()
  if (!text) return items
  const exists = items.some((existing) => itemText(existing).trim() === text)
  return exists ? items : [...items, item].slice(0, limit)
}

const enrichDimensionFindings = (
  dimensions: ReportDimension[],
  actions: PriorityAction[],
  issues: TopIssue[],
  categories: CategorizedFinding[],
) => dimensions.map((dimension) => {
  let weaknesses = [...(dimension.weaknesses || [])]
  let suggestions = [...(dimension.suggestions || [])]

  actions
    .filter((action) => sameDimension(action.dimensionId, dimension.dimensionId))
    .forEach((action) => {
      weaknesses = appendUniqueReportItem(weaknesses, makeReportItem(action.issue || '', dimension.dimensionId, {
        severity: action.severity as Severity,
        confidence: action.confidence,
        priorityScore: action.priorityScore,
        evidence: action.evidence,
      }))
      suggestions = appendUniqueReportItem(suggestions, makeReportItem(action.suggestion || '', dimension.dimensionId, {
        severity: action.severity as Severity,
        confidence: action.confidence,
        priorityScore: action.priorityScore,
        evidence: action.evidence,
      }))
    })

  issues
    .filter((issue) => sameDimension(issue.dimension, dimension.dimensionId))
    .forEach((issue) => {
      weaknesses = appendUniqueReportItem(weaknesses, makeReportItem(issue.text, dimension.dimensionId, {
        severity: issue.severity,
        confidence: issue.evidence_confidence,
        priorityScore: issue.priority_score,
        evidence: issue.evidence,
      }))
      suggestions = appendUniqueReportItem(suggestions, makeReportItem(issue.suggestion || '', dimension.dimensionId, {
        severity: issue.severity,
        confidence: issue.evidence_confidence,
        priorityScore: issue.priority_score,
        evidence: issue.evidence,
      }))
    })

  categories.forEach((category) => {
    category.weaknesses
      .filter((item) => sameDimension(item.dimension, dimension.dimensionId))
      .forEach((item) => {
        weaknesses = appendUniqueReportItem(weaknesses, makeReportItem(item.text, dimension.dimensionId, {
          confidence: category.confidence,
        }))
      })
    category.suggestions
      .filter((item) => sameDimension(item.dimension, dimension.dimensionId))
      .forEach((item) => {
        suggestions = appendUniqueReportItem(suggestions, makeReportItem(item.text, dimension.dimensionId, {
          confidence: category.confidence,
        }))
      })
  })

  return { ...dimension, weaknesses, suggestions }
})

export default function AutoReview() {
  const [phase, setPhase] = useState<Phase>(() => {
    try {
      const saved = localStorage.getItem('autoReviewResults')
      const name = localStorage.getItem('autoReviewFileName')
      if (saved && name && JSON.parse(saved).length > 0) return 'results'
    } catch { /* ignore */ }
    return 'upload'
  })
  const [reviewMode, setReviewMode] = useState<ReviewMode>('thesis')
  const [file, setFile] = useState<File | null>(null)
  const [fileError, setFileError] = useState('')
  const [selectedDims, setSelectedDims] = useState<DimensionId[]>(
    REVIEW_DIMENSIONS.map((d) => d.id)
  )
  const [results, setResults] = useState<ReviewResult[]>(() => {
    try {
      const saved = localStorage.getItem('autoReviewResults')
      return saved ? JSON.parse(saved) : []
    } catch { return [] }
  })
  const [savedFileName, setSavedFileName] = useState(() => {
    try { return localStorage.getItem('autoReviewFileName') || '' } catch { return '' }
  })
  const [dimStatus, setDimStatus] = useState<Record<string, DimStatus>>({})
  const [models, setModels] = useState<ModelOption[]>([])
  const [selectedModel, setSelectedModel] = useState('')
  const [reviewError, setReviewError] = useState('')
  const [exportingPdf, setExportingPdf] = useState(false)
  const [history, setHistory] = useState<ReviewHistoryItem[]>([])
  const [showHistory, setShowHistory] = useState(false)
  const [loadingHistory, setLoadingHistory] = useState(false)
  const [keyFindings, setKeyFindings] = useState<KeyFindings | null>(null)
  const [topIssues, setTopIssues] = useState<TopIssue[]>(() => {
    try { return JSON.parse(localStorage.getItem('autoReviewTopIssues') || '[]') } catch { return [] }
  })
  const [categorizedFindings, setCategorizedFindings] = useState<CategorizedFinding[]>(() => {
    try { return JSON.parse(localStorage.getItem('autoReviewCategories') || '[]') } catch { return [] }
  })
  const [confidenceSummary, setConfidenceSummary] = useState<ConfidenceSummary | null>(() => {
    try { return JSON.parse(localStorage.getItem('autoReviewConfidence') || 'null') } catch { return null }
  })
  const [overallSummary, setOverallSummary] = useState<any>(() => {
    try { return JSON.parse(localStorage.getItem('autoReviewOverallSummary') || 'null') } catch { return null }
  })
  const [reportSummary, setReportSummary] = useState<ReportSummary | null>(() => {
    try { return JSON.parse(localStorage.getItem('autoReviewReportSummary') || 'null') } catch { return null }
  })
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const resultsRef = useRef<HTMLDivElement | null>(null)
  const pdfSummaryRef = useRef<HTMLDivElement | null>(null)

  const toggleDim = (id: DimensionId) => {
    setSelectedDims((prev) =>
      prev.includes(id) ? prev.filter((d) => d !== id) : [...prev, id]
    )
  }

  const switchMode = (mode: ReviewMode) => {
    setReviewMode(mode)
    if (mode === 'paper') {
      setSelectedDims(STANDARD_REVIEW_DIMENSIONS.map((d) => d.id))
    } else {
      setSelectedDims(REVIEW_DIMENSIONS.map((d) => d.id))
    }
  }

  const handleFilePick = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]
    if (!f) return
    validateAndSetFile(f)
  }

  const validateAndSetFile = (f: File) => {
    setFileError('')
    if (f.type !== 'application/pdf' && !f.name.toLowerCase().endsWith('.pdf')) {
      setFileError('仅支持 PDF 格式')
      return
    }
    if (f.size > MAX_FILE_SIZE_MB * 1024 * 1024) {
      setFileError(`文件过大 (${(f.size / 1024 / 1024).toFixed(1)} MB)，最大 ${MAX_FILE_SIZE_MB} MB`)
      return
    }
    setFile(f)
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    const f = e.dataTransfer.files?.[0]
    if (f) validateAndSetFile(f)
  }

  // Sync phase on mount
  useEffect(() => {
    if (results.length > 0 && savedFileName) {
      setPhase('results')
    }
  }, [])

  // Mark pending dims as running after a delay
  useEffect(() => {
    if (phase !== 'reviewing') return
    const timer = setTimeout(() => {
      setDimStatus((prev) => {
        const next = { ...prev }
        for (const key of Object.keys(next)) {
          if (next[key] === 'pending') next[key] = 'running'
        }
        return next
      })
    }, 3000)
    return () => clearTimeout(timer)
  }, [phase])

  // Warn before leaving during review
  useEffect(() => {
    if (phase === 'reviewing') {
      const handler = (e: BeforeUnloadEvent) => {
        e.preventDefault()
        e.returnValue = ''
      }
      window.addEventListener('beforeunload', handler)
      return () => window.removeEventListener('beforeunload', handler)
    }
  }, [phase])

  // Fetch available models on mount
  useEffect(() => {
    fetch('/api/models')
      .then((r) => r.json())
      .then((data) => {
        if (data.models?.length) {
          const qwenModels = data.models.filter((item: ModelOption) =>
            item.value.toLowerCase().includes('qwen')
          )
          if (qwenModels.length === 0) throw new Error('AutoReview requires Qwen')
          setModels(qwenModels)
          setSelectedModel(qwenModels[0].value)
        }
      })
      .catch(() => {
        setModels([
          { value: 'Qwen3.5-122B-A10B-FP8', label: 'Qwen3.5-122B (Local)', baseUrl: '' },
        ])
        setSelectedModel('Qwen3.5-122B-A10B-FP8')
      })
  }, [])

  const startReview = async () => {
    if (!file) return
    setPhase('reviewing')
    setReviewError('')

    // Init dim status
    const status: Record<string, DimStatus> = {}
    selectedDims.forEach((d) => { status[d] = 'pending' })
    setDimStatus(status)

    try {
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), 600000) // 10 min timeout
      const form = new FormData()
      form.append('file', file, file.name)
      form.append('dimensions', JSON.stringify(selectedDims))
      if (selectedModel) form.append('model', selectedModel)
      form.append('vision_reader', 'true')
      form.append('debate', 'true')
      form.append('max_debates', '4')
      form.append('hybrid', 'true')
      form.append('venue', reviewMode === 'thesis' ? 'THESIS' : '')

      const response = await fetch('/api/review', {
        method: 'POST',
        headers: {
          Accept: 'application/x-ndjson',
        },
        body: form,
        signal: controller.signal,
      })

      clearTimeout(timer)

      if (!response.ok) {
        const errData = await response.json().catch(() => ({}))
        throw new Error(errData.error || `服务器错误: ${response.status}`)
      }

      const reader = response.body?.getReader()
      if (!reader) throw new Error('浏览器不支持流式读取')

      const decoder = new TextDecoder()
      let buffer = ''
      const allResults: ReviewResult[] = []

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (!line.trim()) continue
          try {
            const event = JSON.parse(line)

            if (event.type === 'progress') {
              allResults.push(event.result)
              setDimStatus((prev) => ({ ...prev, [event.dimensionId]: 'done' }))
            } else if (event.type === 'complete') {
              setResults(event.results)
              setKeyFindings(event.keyFindings || null)
              const nextTopIssues = event.topIssues || event.meta?.topIssues || []
              const nextCategories = event.categorizedFindings || event.meta?.categorizedFindings || []
              const nextConfidence = event.confidenceSummary || event.meta?.confidenceSummary || null
              const nextSummary = event.overallSummary || null
              const nextReportSummary = event.reportSummary || event.meta?.reportSummary || null
              setTopIssues(nextTopIssues)
              setCategorizedFindings(nextCategories)
              setConfidenceSummary(nextConfidence)
              setOverallSummary(nextSummary)
              setReportSummary(nextReportSummary)
              setSavedFileName(file.name)
              setPhase('results')
              try {
                localStorage.setItem('autoReviewResults', JSON.stringify(event.results))
                localStorage.setItem('autoReviewFileName', file.name)
                localStorage.setItem('autoReviewTopIssues', JSON.stringify(nextTopIssues))
                localStorage.setItem('autoReviewCategories', JSON.stringify(nextCategories))
                localStorage.setItem('autoReviewConfidence', JSON.stringify(nextConfidence))
                if (nextSummary) localStorage.setItem('autoReviewOverallSummary', JSON.stringify(nextSummary))
                else localStorage.removeItem('autoReviewOverallSummary')
                if (nextReportSummary) localStorage.setItem('autoReviewReportSummary', JSON.stringify(nextReportSummary))
                else localStorage.removeItem('autoReviewReportSummary')
              } catch { /* ignore */ }
            } else if (event.type === 'error') {
              throw new Error(event.error || '评审服务异常')
            }
          } catch (e) {
            if (e instanceof SyntaxError) continue // incomplete line
            throw e
          }
        }
      }

      // If stream ended without a complete event, something went wrong
      if (allResults.length > 0) {
        setResults(allResults)
        setTopIssues([])
        setCategorizedFindings([])
        setConfidenceSummary(null)
        setOverallSummary(null)
        setReportSummary(null)
        setSavedFileName(file.name)
        setPhase('results')
        try {
          localStorage.setItem('autoReviewResults', JSON.stringify(allResults))
          localStorage.setItem('autoReviewFileName', file.name)
          localStorage.removeItem('autoReviewTopIssues')
          localStorage.removeItem('autoReviewCategories')
          localStorage.removeItem('autoReviewConfidence')
          localStorage.removeItem('autoReviewOverallSummary')
          localStorage.removeItem('autoReviewReportSummary')
        } catch { /* ignore */ }
      } else {
        throw new Error('评审连接提前中断，未收到有效结果')
      }
    } catch (err: any) {
      if (err.name === 'AbortError') {
        setReviewError('评审超时（超过 10 分钟），请重试')
      } else if (err instanceof TypeError && /fetch/i.test(err.message || '')) {
        // Browser reports a generic "Failed to fetch" when the review
        // service (8907) is down or the Vite proxy cannot reach it. Give the
        // user an actionable diagnosis instead of exposing that opaque text.
        setReviewError('无法连接 Auto Review 服务（8907 端口）。请启动 start-web.sh，确认 review_service 正在运行后重试。')
      } else {
        setReviewError(err instanceof Error ? err.message : '未知错误')
      }
      setPhase('upload')
    }
  }

  const loadHistory = useCallback(async () => {
    setLoadingHistory(true)
    try {
      const res = await fetch('/api/review/history')
      const data = await res.json()
      setHistory(data.history || [])
    } catch {
      // ignore
    } finally {
      setLoadingHistory(false)
    }
  }, [])

  const openHistory = () => {
    loadHistory()
    setShowHistory(true)
  }

  const loadHistoryItem = async (id: string) => {
    try {
      const res = await fetch(`/api/review/history/${id}`)
      if (!res.ok) return
      const data = await res.json()
      if (data.results) {
        setResults(data.results)
        setTopIssues(data.topIssues || data.meta?.topIssues || [])
        setCategorizedFindings(data.categorizedFindings || data.meta?.categorizedFindings || [])
        setConfidenceSummary(data.confidenceSummary || data.meta?.confidenceSummary || null)
        setKeyFindings(data.meta?.keyFindings || null)
        const nextOverallSummary = data.overallSummary || data.meta?.overallSummary || null
        const nextReportSummary = data.reportSummary || data.meta?.reportSummary || null
        setOverallSummary(nextOverallSummary)
        setReportSummary(nextReportSummary)
        setSavedFileName(data.fileName || '')
        setPhase('results')
        try {
          localStorage.setItem('autoReviewResults', JSON.stringify(data.results))
          localStorage.setItem('autoReviewFileName', data.fileName || '')
          localStorage.setItem('autoReviewTopIssues', JSON.stringify(data.topIssues || data.meta?.topIssues || []))
          localStorage.setItem('autoReviewCategories', JSON.stringify(data.categorizedFindings || data.meta?.categorizedFindings || []))
          localStorage.setItem('autoReviewConfidence', JSON.stringify(data.confidenceSummary || data.meta?.confidenceSummary || null))
          if (nextOverallSummary) localStorage.setItem('autoReviewOverallSummary', JSON.stringify(nextOverallSummary))
          else localStorage.removeItem('autoReviewOverallSummary')
          if (nextReportSummary) localStorage.setItem('autoReviewReportSummary', JSON.stringify(nextReportSummary))
          else localStorage.removeItem('autoReviewReportSummary')
        } catch { /* ignore */ }
        setShowHistory(false)
      }
    } catch { /* ignore */ }
  }

  const deleteHistoryItem = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation()
    try {
      await fetch(`/api/review/history/${id}`, { method: 'DELETE' })
      setHistory((prev) => prev.filter((h) => h.id !== id))
    } catch { /* ignore */ }
  }

  const overallScore = useMemo(() => {
    const scoredResults = results.filter((result) => (
      !result.diagnostic_only && !['deep_dive', 'patch'].includes(result.dimensionId)
    ))
    if (scoredResults.length === 0) return 0
    return Math.round(scoredResults.reduce((sum, result) => sum + result.score, 0) / scoredResults.length)
  }, [results])
  const [confidenceFilter, setConfidenceFilter] = useState<ConfidenceFilter>('low')

  const scoredResults = useMemo(() => results.filter((result) => (
    !result.diagnostic_only && !['deep_dive', 'patch'].includes(result.dimensionId)
  )), [results])

  const displayDimensions = useMemo(() => {
    const base = reportSummary?.dimensions?.length
      ? reportSummary.dimensions
      : results.map((r) => toReportDimension(r, reviewMode))
    return enrichDimensionFindings(
      base,
      reportSummary?.priorityActions || [],
      topIssues,
      categorizedFindings,
    ).filter((dimension) => !dimension.diagnosticOnly && !['deep_dive', 'patch'].includes(dimension.dimensionId))
  }, [reportSummary, results, reviewMode, topIssues, categorizedFindings])

  const handleExportMarkdown = () => {
    if (results.length === 0) return
    const reportDimensions = reportSummary?.dimensions?.length
      ? reportSummary.dimensions
      : results.map((r) => toReportDimension(r, reviewMode))
    const lines = [
      reviewMode === 'thesis' ? '# 自动评审报告' : '# Auto Review Report',
      '',
      `**Paper:** ${savedFileName || 'Unknown'}`,
      `**Overall Score:** ${overallScore}/100`,
      reviewMode === 'thesis' ? `**Mode:** 毕业论文` : `**Mode:** English Paper`,
      '',
      '---',
      '',
    ]
    if (reportSummary) {
      lines.push('## 总评')
      lines.push(reportSummary.overallComment || '暂无总评')
      lines.push('')
      if (reportSummary.filterSummary) {
        const filter = reportSummary.filterSummary
        lines.push('## 召回与置信度门控')
        lines.push(`- Dataset 专家模式召回候选：${filter.patternRecallCandidates}`)
        lines.push(`- 确定性论文规范扫描：${filter.documentLintCandidates || 0} 条候选；最终保留：${filter.documentLintRetained || 0}`)
        lines.push(`- 并行专家视角召回候选：${filter.expertLensCandidates || 0}`)
        lines.push(`- 定向混合 Agent Debate：${filter.debatesTriggered || 0} 个问题 / ${filter.hybridDebateAgents || 0} 个 Agent 席位`)
        lines.push(`- 临界意见二次取证：${filter.borderlineReverified}；恢复：${filter.borderlineRecovered}`)
        lines.push(`- 缺失类断言保守校准：${filter.absenceClaimsCalibrated || 0}`)
        if (filter.riskAdjustedThresholds) lines.push(`- 风险自适应置信度阈值：${JSON.stringify(filter.riskAdjustedThresholds)}`)
        lines.push(`- 未通过精度门控并隐藏：${filter.filteredCount}`)
        lines.push(`- 泛化或核实型建议改写为可执行动作：${filter.suggestionsReplaced || 0}`)
        lines.push('')
      }
      const pushReportItems = (title: string, items: ReportTextItem[] = []) => {
        const validItems = items.filter((item) => itemText(item).trim())
        if (validItems.length === 0) return
        lines.push(`## ${title}`)
        validItems.forEach((item) => {
          const dim = itemDimension(item, reviewMode)
          lines.push(`- ${dim ? `【${dim}】` : ''}${itemText(item)}`)
        })
        lines.push('')
      }
      pushReportItems('优点', reportSummary.strengths || [])
      pushReportItems('不足', reportSummary.weaknesses || [])
      pushReportItems('建议', reportSummary.suggestions || [])
      if ((reportSummary.priorityActions || []).length > 0) {
        lines.push('## 优先修改项')
        reportSummary.priorityActions.forEach((action, index) => {
          const dim = action.dimensionId ? dimLabel(action.dimensionId, reviewMode) : ''
          const confidence = typeof action.confidence === 'number' ? `；置信度 ${(action.confidence * 100).toFixed(0)}%` : ''
          lines.push(`${index + 1}. ${dim ? `【${dim}】` : ''}${action.issue || action.suggestion || '待补充问题描述'}${action.severity ? `（${action.severity}${confidence}）` : confidence}`)
          if (action.suggestion) lines.push(`   - 建议：${action.suggestion}`)
          if (action.evidence) lines.push(`   - 证据：${action.evidence}`)
        })
        lines.push('')
      }
    }
    if (confidenceSummary) {
      lines.push('## 置信度与证据覆盖')
      lines.push(`- 总体证据置信度：${(confidenceSummary.overall * 100).toFixed(0)}%（${confidenceLabel(confidenceSummary.overall)}）`)
      lines.push(`- 支持 / 不确定 / 反驳：${confidenceSummary.verdicts.supported} / ${confidenceSummary.verdicts.uncertain} / ${confidenceSummary.verdicts.contradicted}`)
      lines.push(`- 证据单元：${confidenceSummary.evidenceUnits}；图片页：${confidenceSummary.visualPages.join('、') || '无'}；Debate：${confidenceSummary.debatesTriggered}`)
      lines.push('')
    }
    if (!reportSummary && topIssues.length > 0) {
      lines.push(`## Top ${topIssues.length} Evidence-gated Issues`)
      topIssues.forEach((issue, index) => {
        lines.push(`- **#${index + 1} [${issue.severity}] [confidence ${(issue.evidence_confidence * 100).toFixed(0)}%]** ${issue.text}`)
        if (issue.evidence) lines.push(`  - Evidence: ${issue.evidence}`)
        if (issue.suggestion) lines.push(`  - Suggestion: ${issue.suggestion}`)
      })
      lines.push('')
    }
    if (categorizedFindings.length > 0) {
      lines.push('## Categorized Findings')
      categorizedFindings.forEach((category) => {
        lines.push(`### ${category.label} — ${category.score}/100; confidence ${(category.confidence * 100).toFixed(0)}%`)
        category.strengths.forEach((item) => lines.push(`- Strength [${dimLabel(item.dimension, reviewMode)}]: ${item.text}`))
        category.weaknesses.forEach((item) => lines.push(`- Weakness [${dimLabel(item.dimension, reviewMode)}]: ${item.text}`))
        category.suggestions.forEach((item) => lines.push(`- Suggestion [${dimLabel(item.dimension, reviewMode)}]: ${item.text}`))
        lines.push('')
      })
    }
    lines.push('## 分维度详情', '')
    reportDimensions.forEach((r) => {
      lines.push(`### ${r.label || dimLabel(r.dimensionId, reviewMode)}`)
      lines.push(`**Score:** ${r.score}/100`)
      lines.push('')
      lines.push(`**Summary:** ${r.summary}`)
      lines.push('')
      const pushDimensionItems = (title: string, items: ReportTextItem[] = []) => {
        const validItems = items.filter((item) => itemText(item).trim())
        if (validItems.length === 0) return
        lines.push(`**${title}:**`)
        validItems.forEach((item) => lines.push(`- ${itemText(item)}`))
        lines.push('')
      }
      pushDimensionItems('优点', r.strengths)
      if ((r.findings || []).length > 0) {
        lines.push('**不足与对应建议：**')
        ;(r.findings || []).forEach((finding, index) => {
          lines.push(`${index + 1}. ${finding.text}（置信度 ${(finding.evidence_confidence * 100).toFixed(0)}%）`)
          if (finding.suggestion) lines.push(`   - 建议：${finding.suggestion}`)
          if (finding.evidence) lines.push(`   - 证据：${finding.evidence}`)
        })
        lines.push('')
      } else {
        pushDimensionItems('不足', r.weaknesses)
        pushDimensionItems('建议', r.suggestions)
      }
      const shownIds = new Set((r.findings || []).map((finding) => finding.candidate_id))
      const candidates = (r.allFindings || []).filter((finding) => !shownIds.has(finding.candidate_id))
      if (candidates.length > 0) {
        lines.push('**候选意见（未纳入正式结论，按置信度分层）：**')
        candidates.forEach((finding) => {
          const confidence = Number(finding.evidence_confidence || 0)
          const level = confidence >= 0.78 ? '高' : confidence >= 0.55 ? '中' : '低'
          lines.push(`- [${level}置信度 ${(confidence * 100).toFixed(0)}%] ${finding.text || ''}`)
          if (finding.suggestion) lines.push(`  - 候选建议：${finding.suggestion}`)
          if (finding.reason) lines.push(`  - 未纳入原因：${finding.reason}`)
        })
        lines.push('')
      }
      lines.push('---')
      lines.push('')
    })
    const blob = new Blob([lines.join('\n')], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    const baseName = savedFileName
    a.download = `auto-review-${baseName?.replace(/\.[^/.]+$/, '') || 'report'}.md`
    a.click()
    URL.revokeObjectURL(url)
  }

  const handleExportPdf = async () => {
    if (!pdfSummaryRef.current || exportingPdf) return
    setExportingPdf(true)
    try {
      const canvas = await html2canvas(pdfSummaryRef.current, {
        scale: 2,
        useCORS: true,
        backgroundColor: '#ffffff',
      })
      const pdf = new jsPDF('p', 'mm', 'a4')
      const pdfW = pdf.internal.pageSize.getWidth()
      const pdfH = pdf.internal.pageSize.getHeight()
      const ratio = pdfW / canvas.width
      const pageH = pdfH / ratio

      let srcY = 0
      let page = 0
      while (srcY < canvas.height) {
        if (page > 0) pdf.addPage()
        const cropH = Math.min(pageH, canvas.height - srcY)
        const pageCanvas = document.createElement('canvas')
        pageCanvas.width = canvas.width
        pageCanvas.height = cropH
        const ctx = pageCanvas.getContext('2d')!
        ctx.drawImage(canvas, 0, srcY, canvas.width, cropH, 0, 0, canvas.width, cropH)
        pdf.addImage(pageCanvas.toDataURL('image/png'), 'PNG', 0, 0, pdfW, cropH * ratio)
        srcY += cropH
        page++
      }

      pdf.save(`auto-review-summary-${savedFileName?.replace(/\.[^/.]+$/, '') || 'report'}.pdf`)
    } catch (err) {
      console.error('PDF export failed:', err)
    } finally {
      setExportingPdf(false)
    }
  }

  const resetReview = () => {
    setPhase('upload')
    setResults([])
    setFile(null)
    setFileError('')
    setSavedFileName('')
    setDimStatus({})
    setKeyFindings(null)
    setTopIssues([])
    setCategorizedFindings([])
    setConfidenceSummary(null)
    setOverallSummary(null)
    setReportSummary(null)
    try {
      localStorage.removeItem('autoReviewResults')
      localStorage.removeItem('autoReviewFileName')
      localStorage.removeItem('autoReviewTopIssues')
      localStorage.removeItem('autoReviewCategories')
      localStorage.removeItem('autoReviewConfidence')
      localStorage.removeItem('autoReviewOverallSummary')
      localStorage.removeItem('autoReviewReportSummary')
    } catch { /* ignore */ }
  }

  // -- Render ----------------------------------------------------------------

  return (
    <div className="review-page">
      <div className="review-header">
        <h1>Auto Review</h1>
        <p className="review-subtitle">{reviewMode === 'thesis' ? '上传论文，从多个维度自动评审' : 'Upload a paper for multi-dimensional auto review'}</p>
      </div>

      {/* History Panel */}
      {showHistory && (
        <div className="review-history-overlay" onClick={() => setShowHistory(false)}>
          <div className="review-history-panel" onClick={(e) => e.stopPropagation()}>
            <div className="review-history-header">
              <h2>评审历史</h2>
              <button className="review-history-close" onClick={() => setShowHistory(false)}>✕</button>
            </div>
            <div className="review-history-body">
              {loadingHistory ? (
                <p className="review-history-loading">加载中...</p>
              ) : history.length === 0 ? (
                <p className="review-history-empty">暂无评审记录</p>
              ) : (
                <ul className="review-history-list">
                  {history.map((item) => (
                    <li key={item.id} className="review-history-item" onClick={() => loadHistoryItem(item.id)}>
                      <div className="review-history-item-top">
                        <span className="review-history-item-name">{item.fileName}</span>
                        <span className="review-dim-score-badge"
                          data-color={item.overallScore >= 80 ? 'high' : item.overallScore >= 60 ? 'mid' : 'low'}>
                          {item.overallScore}
                        </span>
                      </div>
                      <div className="review-history-item-meta">
                        <span>{item.model}</span>
                        <span>{item.dimensionCount} 维度</span>
                        <span>{new Date(item.timestamp).toLocaleString('zh-CN')}</span>
                      </div>
                      <button className="review-history-delete" onClick={(e) => deleteHistoryItem(item.id, e)}>删除</button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Phase: Upload */}
      {phase === 'upload' && (
        <div className="review-upload-phase">
          <section className="review-section">
            <h2>上传论文</h2>
            <div
              className="review-dropzone"
              onClick={() => fileInputRef.current?.click()}
              onDragOver={(e) => e.preventDefault()}
              onDrop={handleDrop}
            >
              {file ? (
                <div className="review-file-selected">
                  <span className="review-file-icon">📄</span>
                  <span>{file.name} ({(file.size / 1024 / 1024).toFixed(1)} MB)</span>
                  <button className="review-file-remove" onClick={(e) => { e.stopPropagation(); setFile(null); setFileError('') }}>✕</button>
                </div>
              ) : (
                <>
                  <div className="review-dropzone-icon">📄</div>
                  <p>点击选择或拖放 PDF 文件到此处</p>
                  <span className="review-dropzone-hint">支持 .pdf 格式，最大 {MAX_FILE_SIZE_MB} MB</span>
                </>
              )}
              <input
                ref={fileInputRef}
                type="file"
                accept=".pdf,application/pdf"
                hidden
                onChange={handleFilePick}
              />
            </div>
            {fileError && <div className="review-error">{fileError}</div>}
          </section>

          <section className="review-section">
            <h2>{reviewMode === 'thesis' ? '评审模式' : 'Review Mode'}</h2>
            <p className="review-section-desc">
              {reviewMode === 'thesis' ? '选择论文类型以使用对应的评审标准' : 'Select paper type for appropriate review criteria'}
            </p>
            <div className="review-mode-toggle">
              <button
                className={`review-mode-btn ${reviewMode === 'thesis' ? 'active' : ''}`}
                onClick={() => switchMode('thesis')}
              >
                📝 毕业论文 / Thesis
              </button>
              <button
                className={`review-mode-btn ${reviewMode === 'paper' ? 'active' : ''}`}
                onClick={() => switchMode('paper')}
              >
                📄 英文论文 / Paper
              </button>
            </div>
          </section>

          <section className="review-section">
            <h2>{reviewMode === 'thesis' ? '评审维度' : 'Review Dimensions'}</h2>
            <p className="review-section-desc">
              {reviewMode === 'thesis' ? '选择你要评审的维度（默认全选）' : 'Select dimensions to review (all selected by default)'}
            </p>
            <div className="review-dims">
              {(reviewMode === 'thesis' ? REVIEW_DIMENSIONS : STANDARD_REVIEW_DIMENSIONS).map((dim) => (
                <label
                  key={dim.id}
                  className={`review-dim-item ${selectedDims.includes(dim.id) ? 'selected' : ''}`}
                >
                  <input
                    type="checkbox"
                    checked={selectedDims.includes(dim.id)}
                    onChange={() => toggleDim(dim.id)}
                  />
                  <div>
                    <strong>{reviewMode === 'thesis' ? dim.label : dim.labelEn}</strong>
                    <span>{reviewMode === 'thesis' ? dim.desc : dim.descEn}</span>
                  </div>
                </label>
              ))}
            </div>
          </section>

          {models.length > 0 && (
            <section className="review-section">
              <h2>统一模型策略</h2>
              <p className="review-section-desc">
                图表读取、分维度评审、并行专家召回、证据核验、定向 Debate 和总评均使用 Qwen3 系列模型。
              </p>
              <div className="review-model-select">
                <strong>{models.find((model) => model.value === selectedModel)?.label || selectedModel}</strong>
              </div>
            </section>
          )}

          {reviewError && (
            <div className="review-error">
              <strong>评审失败:</strong> {reviewError}
            </div>
          )}

          <div className="review-start-row">
            <button
              className="primary-button review-start-btn"
              onClick={startReview}
              disabled={!file || selectedDims.length === 0 || !!fileError}
            >
              {reviewMode === 'thesis' ? `开始评审 (${selectedDims.length} 个维度)` : `Start Review (${selectedDims.length} dimensions)`}
            </button>
            <button className="review-action-btn" onClick={openHistory}>
              📋 历史记录
            </button>
          </div>
        </div>
      )}

      {/* Phase: Reviewing */}
      {phase === 'reviewing' && (
        <div className="review-progress-phase">
          <div className="review-progress-card">
            <h2>{reviewMode === 'thesis' ? '评审进行中...' : 'Review in Progress...'}</h2>
            <div className="review-progress-spinner" />
            <div className="review-progress-grid">
              {selectedDims.map((dimId) => {
                const dim = REVIEW_DIMENSIONS.find((d) => d.id === dimId)
                return (
                  <DimProgress
                    key={dimId}
                    label={dim ? (reviewMode === 'thesis' ? dim.label : dim.labelEn) : dimId}
                    status={dimStatus[dimId] || 'pending'}
                  />
                )
              })}
            </div>
            <ElapsedTimer />
          </div>
        </div>
      )}

      {/* Phase: Results */}
      {phase === 'results' && (
        <>
        <div className="review-pdf-summary-host" aria-hidden="true">
          <div ref={pdfSummaryRef}>
            <PdfSummaryReport
              fileName={savedFileName}
              overallScore={overallScore}
              report={reportSummary}
              overallSummary={overallSummary}
              fallbackIssues={topIssues}
              confidenceSummary={confidenceSummary}
              mode={reviewMode}
            />
          </div>
        </div>
        <div className="review-results-phase" ref={resultsRef}>
          <div className="review-results-actions">
            <button className="review-action-btn" onClick={handleExportMarkdown}>
              ⬇ 导出 Markdown
            </button>
            <button className="review-action-btn" onClick={handleExportPdf} disabled={exportingPdf}>
              {exportingPdf ? '⏳ 生成中...' : '⬇ 导出总结 PDF'}
            </button>
            <button className="review-action-btn" onClick={openHistory}>
              📋 历史记录
            </button>
            <button className="review-action-btn" onClick={resetReview}>
              ↻ 新评审
            </button>
          </div>

          <div className="review-confidence-filter" role="group" aria-label="置信度筛选">
            <strong>意见置信度</strong>
            {(['formal', 'all', 'high', 'medium', 'low'] as ConfidenceFilter[]).map((level) => (
              <button key={level} type="button" className={confidenceFilter === level ? 'active' : ''} onClick={() => setConfidenceFilter(level)}>
                {level === 'formal' ? '正式意见' : level === 'all' ? '全部' : level === 'high' ? '高' : level === 'medium' ? '中' : '低'}
              </button>
            ))}
            <small>默认展示低置信候选；候选不计入正式评分</small>
          </div>

          <div className="review-overview">
            <div className="review-overall-score">
              <div className="review-score-ring">
                <svg width="120" height="120" viewBox="0 0 120 120">
                  <circle cx="60" cy="60" r="54" fill="none" stroke="#dbe1ea" strokeWidth="8" />
                  <circle
                    cx="60" cy="60" r="54"
                    fill="none" stroke="#2563eb" strokeWidth="8"
                    strokeDasharray={`${(overallScore / 100) * 339.3} 339.3`}
                    strokeLinecap="round"
                    transform="rotate(-90 60 60)"
                  />
                  <text x="60" y="60" textAnchor="middle" dominantBaseline="central" fontSize="28" fontWeight="700" fill="#172033">
                    {overallScore}
                  </text>
                </svg>
              </div>
              <p>{reviewMode === 'thesis' ? '综合评分' : 'Overall Score'}</p>
            </div>
            <div className="review-radar">
              <div className="review-radar-title">各评审维度评分</div>
              <div className="review-radar-content">
                <RadarChart results={scoredResults} />
                <ul className="review-radar-legend">
                  {scoredResults.map((result) => (
                    <li key={result.dimensionId}><span>{dimLabel(result.dimensionId, reviewMode)}</span><b>{result.score}</b></li>
                  ))}
                </ul>
              </div>
            </div>
          </div>

          {/* Report Summary — stable product-facing report layout */}
          {reportSummary && <ReportSummaryPanel report={reportSummary} mode={reviewMode} />}

          {/* Legacy summary fallback for older history records */}
          {!reportSummary && overallSummary && <OverallSummaryPanel summary={overallSummary} mode={reviewMode} />}

          {/* 置信度统计保留在导出报告中，主界面只展示可执行意见，避免内部审计字段干扰阅读。 */}
          {categorizedFindings.length > 0 && <CategorizedFindingsPanel categories={categorizedFindings} mode={reviewMode} />}

          {/* Key Findings Panel — top deficiencies & suggestions */}
          {!reportSummary && (topIssues.length > 0
            ? <TopIssuesPanel issues={topIssues} />
            : keyFindings && <KeyFindingsPanel findings={keyFindings} />)}


          <div className="review-dim-results">
            {displayDimensions.map((r, index) => (
              <details key={r.dimensionId} className="review-dim-card" open={index === 0}>
                <summary className="review-dim-header">
                  <div className="review-dim-header-left">
                    <strong>{r.label || dimLabel(r.dimensionId, reviewMode)}{r.diagnosticOnly ? ' · 专项扫描' : ''}</strong>
                  </div>
                  <div className="review-dim-header-right">
                    <span className="review-dim-confidence">
                      {dimensionConfidence(r) === null ? '置信度待确认' : `置信度 ${(dimensionConfidence(r)! * 100).toFixed(0)}%`}
                    </span>
                    <span className="review-dim-score-badge"
                    data-color={r.score >= 80 ? 'high' : r.score >= 60 ? 'mid' : 'low'}>
                      {r.diagnosticOnly ? '不计分' : `${r.score}/100`}
                    </span>
                  </div>
                </summary>
                <div className="review-dim-body">
                  <p className="review-dim-summary">{r.summary}</p>
                  <DimensionFindingColumns dimension={r} confidenceFilter={confidenceFilter} />
                </div>
              </details>
            ))}
          </div>
        </div>
        </>
      )}
    </div>
  )
}

function PdfSummaryReport({
  fileName,
  overallScore,
  report,
  overallSummary,
  fallbackIssues,
  confidenceSummary,
  mode,
}: {
  fileName: string
  overallScore: number
  report: ReportSummary | null
  overallSummary: any
  fallbackIssues: TopIssue[]
  confidenceSummary: ConfidenceSummary | null
  mode: ReviewMode
}) {
  let actions: PriorityAction[] = [...(report?.priorityActions || [])]
  if (actions.length === 0 && fallbackIssues.length > 0) {
    actions = fallbackIssues.slice(0, 5).map((issue) => ({
      issue: issue.text,
      suggestion: issue.suggestion,
      dimensionId: issue.dimension,
      severity: issue.severity,
      confidence: issue.evidence_confidence,
      priorityScore: issue.priority_score,
      evidence: issue.evidence,
    }))
  }
  if (actions.length === 0 && report) {
    const weaknesses = report.weaknesses || []
    const suggestions = report.suggestions || []
    actions = weaknesses.slice(0, 5).map((weakness, index) => ({
      issue: itemText(weakness),
      suggestion: suggestions[index] ? itemText(suggestions[index]) : '',
      dimensionId: weakness.dimensionId || weakness.dimension,
      confidence: weakness.confidence,
      evidence: weakness.evidence,
    }))
  }
  const overallComment = report?.overallComment
    || overallSummary?.overallAssessment
    || '暂无总体评价。'
  const candidateFindings = (report?.dimensions || [])
    .filter((dimension) => !dimension.diagnosticOnly && !['deep_dive', 'patch'].includes(dimension.dimensionId))
    .flatMap((dimension) => {
      const verified = new Set((dimension.findings || []).map((finding) => finding.candidate_id))
      return (dimension.allFindings || [])
        .filter((finding) => !verified.has(finding.candidate_id))
        .map((finding) => ({ ...finding, dimensionId: dimension.dimensionId }))
    })

  return (
    <article className="review-pdf-summary">
      <header className="review-pdf-summary-header">
        <div>
          <p className="review-pdf-kicker">CLAW AI · AUTO REVIEW</p>
          <h1>自动评审修改意见摘要</h1>
          <p className="review-pdf-file">{fileName || '未命名论文'}</p>
        </div>
        <div className="review-pdf-score">
          <strong>{overallScore}</strong>
          <span>/ 100</span>
        </div>
      </header>

      <section className="review-pdf-overall">
        <h2>总体评价</h2>
        <p>{overallComment}</p>
      </section>

      <section>
        <div className="review-pdf-section-title">
          <h2>主要不足与修改建议</h2>
          <span>按修改优先级排序</span>
        </div>
        {actions.length > 0 ? (
          <ol className="review-pdf-actions">
            {actions.slice(0, 5).map((action, index) => {
              const dimension = action.dimensionId ? dimLabel(action.dimensionId, mode) : ''
              return (
                <li key={index}>
                  <div className="review-pdf-action-heading">
                    <strong>{index + 1}</strong>
                    {dimension && <span>{dimension}</span>}
                    {typeof action.confidence === 'number' && (
                      <span>置信度 {(action.confidence * 100).toFixed(0)}%</span>
                    )}
                  </div>
                  <div className="review-pdf-issue">
                    <b>不足</b>
                    <p>{action.issue || '待补充问题描述'}</p>
                  </div>
                  <div className="review-pdf-suggestion">
                    <b>建议</b>
                    <p>{action.suggestion || '建议针对上述问题补充证据并完成对应修改。'}</p>
                  </div>
                  {action.evidence && (
                    <p className="review-pdf-evidence">证据定位：{action.evidence}</p>
                  )}
                  {(typeof action.claimImpact === 'number' || action.counterfactual) && (
                    <p className="review-pdf-evidence">
                      结论影响：{action.counterfactual || '需结合证据判断'}
                      {typeof action.claimImpact === 'number' ? `（影响度 ${(action.claimImpact * 100).toFixed(0)}%）` : ''}
                    </p>
                  )}
                </li>
              )
            })}
          </ol>
        ) : (
          <p className="review-pdf-empty">本轮未发现达到置信度阈值的主要不足。</p>
        )}
      </section>

      {(report?.modificationTasks || []).length > 0 && (
        <section>
          <h2>修改任务与验收标准</h2>
          <ol className="review-pdf-actions">
            {(report?.modificationTasks || []).map((task) => (
              <li key={task.id}>
                <div className="review-pdf-action-heading"><strong>{task.id}</strong>{task.needsExperiment && <span>需要补实验</span>}</div>
                <div className="review-pdf-issue"><b>任务</b><p>{task.title}</p></div>
                <div className="review-pdf-suggestion"><b>动作</b><p>{task.action}</p></div>
                <p className="review-pdf-evidence">位置：{task.location}</p>
                <p className="review-pdf-evidence">验收：{task.acceptanceCriteria}</p>
              </li>
            ))}
          </ol>
        </section>
      )}

      {candidateFindings.length > 0 && (
        <section className="review-pdf-candidates">
          <h2>候选意见（未纳入正式结论）</h2>
          <p>以下意见按置信度保留，供人工复核，不计入总体评分。</p>
          <ul>
            {candidateFindings.slice(0, 20).map((finding, index) => (
              <li key={finding.candidate_id || index}>
                <b>{dimLabel(finding.dimensionId || finding.dimension, mode)} · 置信度 {(Number(finding.evidence_confidence || 0) * 100).toFixed(0)}%</b>
                <span>{finding.text}</span>
                {finding.reason && <small>未纳入正式结论：{finding.reason}</small>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {confidenceSummary && (
        <section className="review-pdf-audit-note">
          <h2>证据审计摘要</h2>
          <p>总体置信度 {(confidenceSummary.overall * 100).toFixed(0)}%；支持/不确定/反驳 {confidenceSummary.verdicts.supported}/{confidenceSummary.verdicts.uncertain}/{confidenceSummary.verdicts.contradicted}；证据单元 {confidenceSummary.evidenceUnits}。</p>
        </section>
      )}

      <footer>
        主体部分仅展示通过证据门控的正式意见；候选意见单独列在附录，不计入综合评分。
      </footer>
    </article>
  )
}

function ElapsedTimer() {
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    const t0 = Date.now()
    const id = setInterval(() => setElapsed(Date.now() - t0), 200)
    return () => clearInterval(id)
  }, [])
  const s = Math.floor(elapsed / 1000)
  return (
    <p className="review-elapsed">
      已耗时 {s >= 60 ? `${Math.floor(s / 60)}分${s % 60}秒` : `${s}秒`}
    </p>
  )
}

// =============================================================================
// Key Findings Panel — top ~5 deficiencies & suggestions
// =============================================================================

const SEVERITY_CONFIG: Record<Severity, { label: string; icon: string }> = {
  critical: { label: '严重', icon: '🔴' },
  major: { label: '主要', icon: '🟠' },
  minor: { label: '一般', icon: '🟡' },
}

function SeverityBadge({ severity }: { severity?: Severity }) {
  if (!severity || severity === 'minor') return null
  const safeSeverity: Severity = severity in SEVERITY_CONFIG ? severity : 'major'
  const cfg = SEVERITY_CONFIG[safeSeverity]
  return <span className={`review-severity-badge severity-${safeSeverity}`} title={cfg.label}>{cfg.icon} {cfg.label}</span>
}

function TopIssuesPanel({ issues }: { issues: TopIssue[] }) {
  return (
    <div className="review-keyfindings">
      <div className="review-keyfindings-header">
        <h3>🎯 核心问题（{issues.length}）</h3>
        <span className="review-keyfindings-hint">Qwen 多专家发现、证据验证与定向 debate 后排序</span>
      </div>
      <div className="review-keyfindings-body">
        <ul className="review-keyfindings-list">
          {issues.map((issue, index) => (
            <li key={issue.candidate_id || index} className="review-keyfindings-item weakness-item">
              <div className="review-keyfindings-item-top">
                <strong>#{index + 1}</strong>
                <SeverityBadge severity={issue.severity} />
                <span className="review-keyfindings-dimtag">{issue.dimension}</span>
                <span className="review-keyfindings-dimtag">优先级 {issue.priority_score.toFixed(1)}</span>
                <span className="review-keyfindings-dimtag">
                  证据置信度 {(issue.evidence_confidence * 100).toFixed(0)}% · {confidenceLabel(issue.evidence_confidence)}
                </span>
                {issue.verdict && <span className="review-keyfindings-dimtag">{verdictLabel(issue.verdict)}</span>}
                {issue.debate && Object.keys(issue.debate).length > 0 && (
                  <span className="review-keyfindings-dimtag">
                    混合 Agent × {typeof issue.debate.agent_count === 'number' ? issue.debate.agent_count : 4}
                    {typeof issue.debate.agreement_score === 'number'
                      ? ` · 一致度 ${(issue.debate.agreement_score * 100).toFixed(0)}%`
                      : ''}
                  </span>
                )}
              </div>
              <span className="review-keyfindings-text">{issue.text}</span>
              {issue.evidence && <small>证据：{issue.evidence}</small>}
              {issue.counterfactual && <small>结论影响：{issue.counterfactual}</small>}
              {issue.suggestion && <small>建议：{issue.suggestion}</small>}
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}

function DimensionFindingColumns({ dimension, confidenceFilter }: { dimension: ReportDimension; confidenceFilter: ConfidenceFilter }) {
  const sourceFindings = dimension.allFindings?.length ? dimension.allFindings : (dimension.findings || [])
  const confidenceLevel = (value: number) => value >= 0.78 ? 'high' : value >= 0.55 ? 'medium' : 'low'
  const legacyWeaknesses = dimension.candidateWeaknesses || []
  const legacySuggestions = dimension.candidateSuggestions || []
  const fallbackCandidates: TopIssue[] = confidenceFilter === 'low' && sourceFindings.length === 0
    ? legacyWeaknesses.map((item, index) => ({
        text: itemText(item),
        suggestion: legacySuggestions[index] ? itemText(legacySuggestions[index]) : '',
        evidence_confidence: 0,
        evidence: '',
        dimension: dimension.dimensionId,
        candidate_id: `legacy-w-${itemText(item)}`,
        severity: 'minor' as Severity,
        priority_score: 0,
      })).filter((item) => item.text.trim())
    : []
  const pairedFindings = confidenceFilter === 'formal'
    ? (dimension.findings || [])
    : [...sourceFindings, ...fallbackCandidates].filter((finding) => (
        confidenceFilter === 'all' || confidenceLevel(Number(finding.evidence_confidence || 0)) === confidenceFilter
      ))
  const filteredWeaknesses = sourceFindings.length
    ? pairedFindings.map((finding) => ({ text: finding.text || '', confidence: finding.evidence_confidence, evidence: finding.evidence }))
    : (dimension.weaknesses || [])
  const filteredSuggestions = sourceFindings.length
    ? pairedFindings.filter((finding) => finding.suggestion).map((finding) => ({ text: finding.suggestion || '', confidence: finding.evidence_confidence, evidence: finding.evidence }))
    : (dimension.suggestions || [])
  const groups = [
    {
      key: 'strengths',
      title: '✅ 优点',
      cls: 'emphasis-strength',
      items: dimension.strengths || [],
      empty: '暂无明确优点摘录。',
    },
    ...(!pairedFindings.length ? [{
      key: 'weaknesses',
      title: '⚠️ 不足',
      cls: 'emphasis-weakness',
      items: filteredWeaknesses,
      empty: dimension.generatedWeaknessCount
        ? `生成过 ${dimension.generatedWeaknessCount} 条候选不足，但本轮证据/置信度门控未保留为高置信意见。`
        : '本维度未发现高置信不足。',
    },
    {
      key: 'suggestions',
      title: '💡 建议',
      cls: 'emphasis-suggestion',
      items: filteredSuggestions,
      empty: dimension.generatedSuggestionCount
        ? `生成过 ${dimension.generatedSuggestionCount} 条候选建议，但本轮证据/置信度门控未保留为高置信意见。`
        : '本维度暂无单独建议；可参考上方“优先修改项”。',
    }] : []),
  ]

  return (
    <>
      {(typeof dimension.filteredLowConfidenceCount === 'number' && dimension.filteredLowConfidenceCount > 0) && (
        <div className="review-filter-note">
          {confidenceFilter === 'formal'
            ? '当前筛选：正式意见；仅展示通过证据门控的不足与建议。'
            : confidenceFilter === 'all'
            ? `已过滤 ${dimension.filteredLowConfidenceCount} 条未通过精度门控的候选；选择“中”或“低”可查看候选意见。`
            : `当前筛选：${confidenceFilter === 'high' ? '高' : confidenceFilter === 'medium' ? '中' : '低'}置信度；候选意见不计入正式评分。`}
        </div>
      )}
      {pairedFindings.length > 0 && (
        <div className="review-keyfindings review-bound-findings">
          <div className="review-keyfindings-header">
            <h4>⚠️ 不足与对应建议</h4>
            <span className="review-keyfindings-hint">问题、修改动作与证据保持绑定</span>
          </div>
          <div className="review-keyfindings-body">
            <ol className="review-priority-list">
              {pairedFindings.map((finding, index) => (
                <li key={finding.candidate_id || index} className="review-keyfindings-item weakness-item">
                  <div className="review-keyfindings-item-top">
                    <SeverityBadge severity={finding.severity || 'major'} />
                    <span className="review-keyfindings-dimtag">
                      置信度 {(finding.evidence_confidence * 100).toFixed(0)}%
                    </span>
                    {finding.debate && Object.keys(finding.debate).length > 0 && (
                      <span className="review-keyfindings-dimtag">
                        混合 Agent × {typeof finding.debate.agent_count === 'number' ? finding.debate.agent_count : 4}
                        {typeof finding.debate.agreement_score === 'number'
                          ? ` · 一致度 ${(finding.debate.agreement_score * 100).toFixed(0)}%`
                          : ''}
                      </span>
                    )}
                  </div>
                  <span className="review-keyfindings-text">{finding.text}</span>
                  {finding.suggestion && <small>建议：{finding.suggestion}</small>}
                  {finding.evidence && <small>证据：{finding.evidence}</small>}
                  {finding.counterfactual && <small>结论影响：{finding.counterfactual}</small>}
                </li>
              ))}
            </ol>
          </div>
        </div>
      )}
      <div className="review-dim-columns">
      {groups.map((group) => (
        <div key={group.key} className={`review-dim-col ${group.cls}`}>
          <h4>{group.title}</h4>
          {hasReportItems(group.items) ? (
            <ul>
              {group.items
                .filter((item) => itemText(item).trim())
                .map((item, index) => (
                  <li key={index}>
                    {itemText(item)}
                    {(typeof item.confidence === 'number' || item.evidence) && (
                      <small className="review-item-meta">
                        {typeof item.confidence === 'number' && `置信度 ${(item.confidence * 100).toFixed(0)}%`}
                        {item.evidence && ` · 证据：${item.evidence}`}
                      </small>
                    )}
                  </li>
                ))}
            </ul>
          ) : (
            <p className="review-dim-empty">{group.empty}</p>
          )}
        </div>
      ))}
      </div>
    </>
  )
}

function ReportSummaryPanel({ report, mode }: { report: ReportSummary; mode: ReviewMode }) {
  const strengths = report.strengths || []
  const weaknesses = report.weaknesses || []
  const suggestions = report.suggestions || []
  const actions = report.priorityActions || []

  const renderList = (items: ReportTextItem[]) => (
    <ul>
      {items
        .filter((item) => itemText(item).trim())
        .map((item, index) => {
        const dim = itemDimension(item, mode)
        return (
          <li key={index}>
            {dim && <small className="review-inline-dim">【{dim}】</small>}
            {itemText(item)}
            {(typeof item.confidence === 'number' || item.evidence) && (
              <small className="review-item-meta">
                {typeof item.confidence === 'number' && `置信度 ${(item.confidence * 100).toFixed(0)}%`}
                {item.evidence && ` · 证据：${item.evidence}`}
              </small>
            )}
          </li>
        )
      })}
    </ul>
  )

  return (
    <div className="review-report-summary">
      <div className="review-keyfindings">
        <div className="review-keyfindings-header">
          <h3>📌 总评</h3>
          {typeof report.overallScore === 'number' && (
            <span className="review-keyfindings-hint">综合评分 {report.overallScore}/100</span>
          )}
        </div>
        <div className="review-keyfindings-body">
          <p className="review-dim-summary">{report.overallComment || '暂无总评'}</p>
        </div>
      </div>

      {(hasReportItems(strengths) || hasReportItems(weaknesses) || hasReportItems(suggestions)) && (
        <div className="review-dim-columns review-report-triplet">
          {hasReportItems(strengths) && (
            <div className="review-dim-col emphasis-strength">
              <h4>✅ 优点</h4>
              {renderList(strengths)}
            </div>
          )}
          {hasReportItems(weaknesses) && (
            <div className="review-dim-col emphasis-weakness">
              <h4>⚠️ 不足</h4>
              {renderList(weaknesses)}
            </div>
          )}
          {hasReportItems(suggestions) && (
            <div className="review-dim-col emphasis-suggestion">
              <h4>💡 建议</h4>
              {renderList(suggestions)}
            </div>
          )}
        </div>
      )}

      {actions.length > 0 && (
        <div className="review-keyfindings review-priority-actions">
          <div className="review-keyfindings-header">
            <h3>🎯 优先修改项</h3>
            <span className="review-keyfindings-hint">按问题严重度、证据置信度和优先级综合排序</span>
          </div>
          <div className="review-keyfindings-body">
            <ol className="review-priority-list">
              {actions.map((action, index) => {
                const dim = action.dimensionId ? dimLabel(action.dimensionId, mode) : ''
                return (
                  <li key={index} className="review-keyfindings-item weakness-item">
                    <div className="review-keyfindings-item-top">
                      <SeverityBadge severity={(action.severity as Severity) || 'major'} />
                      {dim && <span className="review-keyfindings-dimtag">{dim}</span>}
                      {typeof action.confidence === 'number' && (
                        <span className="review-keyfindings-dimtag">置信度 {(action.confidence * 100).toFixed(0)}%</span>
                      )}
                      {typeof action.priorityScore === 'number' && (
                        <span className="review-keyfindings-dimtag">优先级 {action.priorityScore.toFixed(2)}</span>
                      )}
                    </div>
                    <span className="review-keyfindings-text">{action.issue || action.suggestion || '待补充问题描述'}</span>
                    {action.suggestion && <small>建议：{action.suggestion}</small>}
                    {action.evidence && <small>证据：{action.evidence}</small>}
                  </li>
                )
              })}
            </ol>
          </div>
        </div>
      )}
      {(report.modificationTasks || []).length > 0 && (
        <div className="review-keyfindings review-priority-actions">
          <div className="review-keyfindings-header">
            <h3>🛠 修改任务</h3>
            <span className="review-keyfindings-hint">从正式意见生成，可按验收标准逐项完成</span>
          </div>
          <div className="review-keyfindings-body">
            <ol className="review-priority-list">
              {(report.modificationTasks || []).map((task) => (
                <li key={task.id} className="review-keyfindings-item suggestion-item">
                  <div className="review-keyfindings-item-top">
                    <span className="review-keyfindings-dimtag">{task.id}</span>
                    {task.needsExperiment && <span className="review-keyfindings-dimtag">需要补实验</span>}
                  </div>
                  <span className="review-keyfindings-text">{task.title}</span>
                  <small>位置：{task.location}</small>
                  <small>动作：{task.action}</small>
                  <small>验收：{task.acceptanceCriteria}</small>
                </li>
              ))}
            </ol>
          </div>
        </div>
      )}
    </div>
  )
}

function OverallSummaryPanel({ summary, mode }: { summary: any; mode: ReviewMode }) {
  // Thesis mode: 3 reviewer reports
  if (summary.reviewers) {
    const { reviewers, comparativeAnalysis, finalRecommendation } = summary
    return (
      <div className="review-dim-results">
        <h3 style={{ margin: '16px 0 8px', fontSize: '1.1rem' }}>{mode === 'thesis' ? '📋 综合审稿意见' : '📋 Comprehensive Review'}</h3>
        {reviewers.map((r: any, i: number) => (
          <details key={i} className="review-dim-card" open={i === 0}>
            <summary className="review-dim-header">
              <div className="review-dim-header-left"><strong>{r.expertise}</strong></div>
            </summary>
            <div className="review-dim-body">
              <p className="review-dim-summary"><strong>{mode === 'thesis' ? '总体评价：' : 'Overall: '}</strong>{r.overallEvaluation}</p>
              {r.highlights && r.highlights.length > 0 && (
                <div className="review-dim-col emphasis-strength">
                  <h4>{mode === 'thesis' ? '✅ 优点' : '✅ Strengths'}</h4>
                  <ul>{r.highlights.map((h: string, j: number) => <li key={j}>{h}</li>)}</ul>
                </div>
              )}
              {r.keyIssues && r.keyIssues.length > 0 && (
                <div className="review-dim-col emphasis-weakness" style={{ marginTop: 10 }}>
                  <h4>{mode === 'thesis' ? '⚠️ 不足' : '⚠️ Weaknesses'}</h4>
                  <ul>{r.keyIssues.map((k: string, j: number) => <li key={j}>{k}</li>)}</ul>
                </div>
              )}
              {r.improvementAdvice && r.improvementAdvice.length > 0 && (
                <div className="review-dim-col emphasis-suggestion" style={{ marginTop: 10 }}>
                  <h4>{mode === 'thesis' ? '💡 修改建议' : '💡 Suggestions'}</h4>
                  <ul>{r.improvementAdvice.map((a: string, j: number) => <li key={j}>{a}</li>)}</ul>
                </div>
              )}
              <div className="review-keyfindings-item-top" style={{ marginTop: 10 }}>
                <span className="review-keyfindings-dimtag">{r.overallVerdict}</span>
                <span className="review-keyfindings-dimtag">{r.recommendation}</span>
              </div>
            </div>
          </details>
        ))}
        {comparativeAnalysis && (
          <div className="review-keyfindings" style={{ marginTop: 12 }}>
            <div className="review-keyfindings-header">
              <h3>{mode === 'thesis' ? '🔄 对比分析' : '🔄 Comparative Analysis'}</h3>
            </div>
            <div className="review-keyfindings-body">
              <p><strong>{mode === 'thesis' ? '共识：' : 'Agreements: '}</strong>{comparativeAnalysis.agreements || comparativeAnalysis.bestAspects || ''}</p>
              <p><strong>{mode === 'thesis' ? '分歧：' : 'Disagreements: '}</strong>{comparativeAnalysis.disagreements || comparativeAnalysis.weakestAspects || ''}</p>
              <p><strong>{mode === 'thesis' ? '交叉分析：' : 'Cross-dimension: '}</strong>{comparativeAnalysis.crossDimensionInsights || comparativeAnalysis.crossDimensionTradeoffs || ''}</p>
            </div>
          </div>
        )}
        {finalRecommendation && (
          <div className="review-keyfindings" style={{ marginTop: 12 }}>
            <div className="review-keyfindings-header">
              <h3>{mode === 'thesis' ? '🎯 最终结论' : '🎯 Final Recommendation'}</h3>
            </div>
            <div className="review-keyfindings-body">
              {finalRecommendation.verdict && (
                <div className="review-keyfindings-item-top">
                  <span className="review-keyfindings-dimtag">{finalRecommendation.verdict}</span>
                </div>
              )}
              <p className="review-dim-summary">{finalRecommendation.summary || finalRecommendation.description || ''}</p>
            </div>
          </div>
        )}
      </div>
    )
  }

  // Paper mode: single comprehensive summary
  return (
    <div className="review-dim-results">
      <h3 style={{ margin: '16px 0 8px', fontSize: '1.1rem' }}>{mode === 'thesis' ? '📋 综合评审' : '📋 Executive Summary'}</h3>
      <div className="review-dim-card" style={{ padding: '14px 18px' }}>
        <p className="review-dim-summary" style={{ fontSize: '0.95rem', lineHeight: 1.6 }}>{summary.overallAssessment}</p>
        {summary.executiveSummary && (
          <p className="review-dim-summary" style={{ marginTop: 8, fontStyle: 'italic', color: 'var(--muted)' }}>
            {mode === 'thesis' ? '📌 ' : '📌 '}{summary.executiveSummary}
          </p>
        )}
      </div>
      <div className="review-dim-columns" style={{ marginTop: 8 }}>
        {summary.detailedStrengths && summary.detailedStrengths.length > 0 && (
          <div className="review-dim-col emphasis-strength">
            <h4>{mode === 'thesis' ? '✅ 综合优点' : '✅ Strengths'}</h4>
            <ul>{summary.detailedStrengths.map((s: string, i: number) => <li key={i}>{s}</li>)}</ul>
          </div>
        )}
        {summary.detailedWeaknesses && summary.detailedWeaknesses.length > 0 && (
          <div className="review-dim-col emphasis-weakness">
            <h4>{mode === 'thesis' ? '⚠️ 综合不足' : '⚠️ Weaknesses'}</h4>
            <ul>{summary.detailedWeaknesses.map((w: string, i: number) => <li key={i}>{w}</li>)}</ul>
          </div>
        )}
        {summary.detailedSuggestions && summary.detailedSuggestions.length > 0 && (
          <div className="review-dim-col emphasis-suggestion">
            <h4>{mode === 'thesis' ? '💡 综合建议' : '💡 Suggestions'}</h4>
            <ul>{summary.detailedSuggestions.map((s: string, i: number) => <li key={i}>{s}</li>)}</ul>
          </div>
        )}
      </div>
      {summary.comparativeAnalysis && (
        <div className="review-keyfindings" style={{ marginTop: 12 }}>
          <div className="review-keyfindings-header">
            <h3>{mode === 'thesis' ? '📊 维度对比分析' : '📊 Cross-Dimension Analysis'}</h3>
          </div>
          <div className="review-keyfindings-body">
            <div className="review-keyfindings-item-top">
              {summary.comparativeAnalysis.dimensionScoreRange && (
                <span className="review-keyfindings-dimtag">{mode === 'thesis' ? '分数范围：' : 'Score range: '}{summary.comparativeAnalysis.dimensionScoreRange}</span>
              )}
              {summary.recommendation && (
                <span className="review-keyfindings-dimtag">{mode === 'thesis' ? '推荐：' : 'Recommendation: '}{summary.recommendation}</span>
              )}
              {summary.confidence && (
                <span className="review-keyfindings-dimtag">{mode === 'thesis' ? '置信度：' : 'Confidence: '}{summary.confidence}</span>
              )}
            </div>
            <p style={{ marginTop: 8 }}><strong>{mode === 'thesis' ? '最强方面：' : 'Best aspects: '}</strong>{summary.comparativeAnalysis.bestAspects || ''}</p>
            <p><strong>{mode === 'thesis' ? '薄弱方面：' : 'Weakest aspects: '}</strong>{summary.comparativeAnalysis.weakestAspects || ''}</p>
            <p><strong>{mode === 'thesis' ? '交叉权衡：' : 'Tradeoffs: '}</strong>{summary.comparativeAnalysis.crossDimensionTradeoffs || summary.comparativeAnalysis.crossDimensionInsights || ''}</p>
          </div>
        </div>
      )}
    </div>
  )
}

const confidenceLabel = (value: number) => value >= 0.75 ? '高' : value >= 0.5 ? '中' : '低'
const verdictLabel = (value: string) => ({ supported: '证据支持', uncertain: '尚不确定', contradicted: '证据反驳' }[value] || value)

function dimensionConfidence(dimension: ReportDimension): number | null {
  const findings = (dimension.allFindings?.length ? dimension.allFindings : dimension.findings || [])
    .map((finding) => Number(finding.evidence_confidence))
    .filter((value) => Number.isFinite(value) && value > 0)
  if (findings.length > 0) return findings.reduce((sum, value) => sum + value, 0) / findings.length
  const itemConfidences = [...(dimension.strengths || []), ...(dimension.weaknesses || []), ...(dimension.suggestions || [])]
    .map((item) => Number(item.confidence))
    .filter((value) => Number.isFinite(value) && value > 0)
  if (itemConfidences.length === 0) return null
  return itemConfidences.reduce((sum, value) => sum + value, 0) / itemConfidences.length
}

function CategorizedFindingsPanel({ categories, mode }: { categories: CategorizedFinding[]; mode: ReviewMode }) {
  const cl = (id: string) => dimLabel(id, mode)
  const hasCategorizedItems = (items: CategorizedItem[]) => items.some((item) => item.text.trim())
  return (
    <div className="review-dim-results">
      {categories.map((category) => (
        <details key={category.id} className="review-dim-card" open>
          <summary className="review-dim-header">
            <div className="review-dim-header-left"><strong>{category.label}</strong></div>
            <div className="review-keyfindings-item-top">
              <span className="review-keyfindings-dimtag">{mode === 'thesis' ? `置信度 ${(category.confidence * 100).toFixed(0)}%` : `confidence ${(category.confidence * 100).toFixed(0)}%`} · {confidenceLabel(category.confidence)}</span>
              <span className="review-dim-score-badge" data-color={category.score >= 80 ? 'high' : category.score >= 60 ? 'mid' : 'low'}>{category.score}/100</span>
            </div>
          </summary>
          <div className="review-dim-body">
            {(category.weaknesses.length > 0 || category.suggestions.length > 0)
              ? category.summaries.map((item, index) => <p key={index} className="review-dim-summary"><strong>{cl(item.dimension)}: </strong>{item.text}</p>)
              : <p className="review-dim-summary">本分类当前仅保留有证据支持的优点，未保留可确认的不足或建议。</p>}
            <div className="review-dim-columns">
              {hasCategorizedItems(category.strengths) && (
                <div className="review-dim-col emphasis-strength"><h4>{mode === 'thesis' ? '✅ 优点' : '✅ Strengths'}</h4><ul>{category.strengths.filter((item) => item.text.trim()).map((item, i) => <li key={i}>{item.text} <small>({cl(item.dimension)})</small></li>)}</ul></div>
              )}
              {hasCategorizedItems(category.weaknesses) && (
                <div className="review-dim-col emphasis-weakness"><h4>{mode === 'thesis' ? '⚠️ 不足' : '⚠️ Weaknesses'}</h4><ul>{category.weaknesses.filter((item) => item.text.trim()).map((item, i) => <li key={i}>{item.text} <small>({cl(item.dimension)})</small></li>)}</ul></div>
              )}
              {hasCategorizedItems(category.suggestions) && (
                <div className="review-dim-col emphasis-suggestion"><h4>{mode === 'thesis' ? '💡 建议' : '💡 Suggestions'}</h4><ul>{category.suggestions.filter((item) => item.text.trim()).map((item, i) => <li key={i}>{item.text} <small>({cl(item.dimension)})</small></li>)}</ul></div>
              )}
            </div>
          </div>
        </details>
      ))}
    </div>
  )
}

function KeyFindingsPanel({ findings }: { findings: KeyFindings }) {
  const { weaknesses, suggestions } = findings
  const hasItems = (weaknesses?.length || 0) > 0 || (suggestions?.length || 0) > 0
  if (!hasItems) return null

  return (
    <div className="review-keyfindings">
      <div className="review-keyfindings-header">
        <h3>📋 核心评审意见</h3>
        <span className="review-keyfindings-hint">综合各维度提炼的 ~5 条关键不足与修改建议</span>
      </div>
      <div className="review-keyfindings-body">
        {weaknesses?.length > 0 && (
          <div className="review-keyfindings-section">
            <h4 className="review-keyfindings-section-title weaknesses-title">⚠️ 主要不足</h4>
            <ul className="review-keyfindings-list">
              {weaknesses.map((w, i) => (
                <li key={i} className="review-keyfindings-item weakness-item">
                  <div className="review-keyfindings-item-top">
                    <SeverityBadge severity={w.severity} />
                    <span className="review-keyfindings-dimtag">{w.dimensionId}</span>
                  </div>
                  <span className="review-keyfindings-text">{w.text}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {suggestions?.length > 0 && (
          <div className="review-keyfindings-section">
            <h4 className="review-keyfindings-section-title suggestions-title">💡 修改建议</h4>
            <ul className="review-keyfindings-list">
              {suggestions.map((s, i) => (
                <li key={i} className="review-keyfindings-item suggestion-item">
                  <span className="review-keyfindings-text">{s.text}</span>
                  <span className="review-keyfindings-dimtag">{s.dimensionId}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  )
}
