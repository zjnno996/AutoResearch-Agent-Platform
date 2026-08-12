import { useEffect, useMemo, useRef, useState } from 'react'
import LiteraturePanel from '../components/LiteraturePanel'
import type { PaperInfo } from '../components/LiteraturePanel'
import { downloadMarkdown, printContentAsPdf } from '../export'
import { useAuth } from '../contexts/AuthContext'

type ProjectStatus = 'running' | 'queued' | 'completed' | 'interrupted' | 'failed' | 'new'
type RestartFrom = 'topic' | 'questions' | 'search' | 'literature' | 'evidence' | 'synthesis' | 'ideas' | 'experiment' | 'code' | 'run' | 'analysis' | 'writing' | 'finalization' | 'export'

type ProjectInfo = {
  projectId: string
  status: ProjectStatus
  lastCompletedStage: number
  lastCompletedName: string
  firstStage: number
  totalStages: number
  timestamp: string
  topic: string
  configPath: string
  intervention?: string
  runMode?: RunMode
}

type Artifact = {
  id: string
  repoId: string
  projectId: string
  filename: string
  producedBy: string
  timestamp: number
  size: string
  status: 'fresh' | 'stale' | 'error'
  content?: string
  stage?: number
}

type ChatMessage = {
  id: string
  role: 'user' | 'system'
  content: string
  targetLayer?: string
  projectId?: string
  timestamp: number
}

type LiteratureSchedule = {
  id: string
  name: string
  topic: string
  keywords: string[]
  sources: string[]
  intervalHours: number
  lookbackDays: number
  enabled: boolean
  status: 'idle' | 'running' | 'paused'
  lastRunAt: number
  nextRunAt: number
  lastProjectId?: string
  lastNewPaperCount?: number
  lastError?: string
  runCount: number
  history?: Array<{ projectId: string; completedAt: number; newPaperCount: number }>
}

type WSMessage =
  | { type: 'artifact_produced'; payload: Artifact }
  | { type: 'project_list'; payload: ProjectInfo[] }
  | { type: 'chat_message'; payload: ChatMessage }
  | { type: 'literature_list'; payload: { projectId: string; papers: PaperInfo[] } }
  | { type: 'literature_schedule_list'; payload: LiteratureSchedule[] }
  | { type: 'auth_result'; payload: { ok: boolean; user?: { id: string; username: string }; token?: string; error?: string } }
  | { type: 'system'; payload: { message: string } }

type ReferencePdfUpload = { name: string; contentBase64: string }
type RunMode = 'idea_gate' | 'full_chain' | 'literature_watch'
type ExperimentProvenance = {
  executed?: boolean
  real_code_execution?: boolean
  experiment_scope?: string
  implementation?: string
  scientific_claims_allowed?: boolean
  claim_status?: string
  display_status_zh?: string
  command?: string
  returncode?: number | null
}
type ExpPlanDiagnostics = {
  status?: string
  degraded?: boolean
  parse_strategy?: string
  fallback_reason?: string
  benchmark_agent_validation_passed?: boolean | null
  benchmark_agent_errors?: string[]
  benchmark_agent_warnings?: string[]
  user_facing_status_zh?: string
}
type ResearchReadiness = {
  readiness_level?: string
  readiness_score?: number
  writing_policy?: string
  user_facing_status_zh?: string
  scientific_claims_allowed?: boolean
  limited_claims_allowed?: boolean
  evidence?: Record<string, unknown>
  scores?: Record<string, number>
  recommended_actions?: string[]
  execution_control_decision?: string
  forced_proceed_after_max_pivots?: boolean
  forced_proceed_reason?: string
}
type ClaimIntegrityReport = {
  status?: 'passed' | 'warning' | 'blocked'
  integrity_score?: number
  writing_policy?: string
  user_facing_status_zh?: string
  has_limitations_section?: boolean
  violations?: Array<{ severity?: string; type?: string; message_zh?: string }>
  recommended_actions?: string[]
}

const WS_PROTO = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
const AGENT_WS = `${WS_PROTO}//${window.location.host}/ws/agents`
const RESTART_OPTIONS: Array<{ value: RestartFrom; label: string }> = [
  { value: 'topic', label: '研究主题与目标' },
  { value: 'questions', label: '研究问题拆解' },
  { value: 'search', label: '检索策略' },
  { value: 'literature', label: '文献收集与筛选' },
  { value: 'evidence', label: '证据卡片抽取' },
  { value: 'synthesis', label: '知识综述' },
  { value: 'ideas', label: '研究想法生成' },
  { value: 'experiment', label: '实验方案设计' },
  { value: 'code', label: '代码检索与生成' },
  { value: 'run', label: '实验执行' },
  { value: 'analysis', label: '结果分析与决策' },
  { value: 'writing', label: '论文写作' },
  { value: 'finalization', label: '质量检查与最终导出' },
  { value: 'export', label: '仅重新导出与归档' },
]
const IDEA_COUNT_OPTIONS = [3, 5, 8]
const DEFAULT_QWEN3_MODEL = 'Qwen3.5-122B-A10B-FP8'
const MODEL_OPTIONS = [
  { value: DEFAULT_QWEN3_MODEL, label: 'Qwen3.5 122B 本地' },
]
const PIPELINE_PHASES = [
  { key: 'L1', label: '调研与 Idea', start: 1, end: 8 },
  { key: 'L2', label: '实验设计', start: 9, end: 9 },
  { key: 'L3', label: '代码生成', start: 10, end: 13 },
  { key: 'L4', label: '实验与分析', start: 14, end: 18 },
  { key: 'L5', label: '写作与交付', start: 19, end: 26 },
]

function formatTime(ts: number) {
  return new Date(ts).toLocaleString('zh-CN', { hour12: false })
}

function pickLatestArtifact(artifacts: Artifact[], filenames: string[]) {
  return artifacts.find((artifact) => filenames.includes(artifact.filename)) ?? null
}

function parseExperimentProvenance(artifact: Artifact | null): ExperimentProvenance | null {
  if (!artifact?.content) return null
  try {
    return JSON.parse(artifact.content) as ExperimentProvenance
  } catch {
    return null
  }
}

function parseExpPlanDiagnostics(artifact: Artifact | null): ExpPlanDiagnostics | null {
  if (!artifact?.content) return null
  try {
    return JSON.parse(artifact.content) as ExpPlanDiagnostics
  } catch {
    return null
  }
}

function parseResearchReadiness(artifact: Artifact | null): ResearchReadiness | null {
  if (!artifact?.content) return null
  try {
    return JSON.parse(artifact.content) as ResearchReadiness
  } catch {
    return null
  }
}

function parseClaimIntegrityReport(artifact: Artifact | null): ClaimIntegrityReport | null {
  if (!artifact?.content) return null
  try {
    return JSON.parse(artifact.content) as ClaimIntegrityReport
  } catch {
    return null
  }
}

function readinessLabel(readiness: ResearchReadiness | null) {
  if (!readiness) return '等待科研就绪度评估'
  if (readiness.readiness_level === 'scientific_ready') return '科研证据就绪'
  if (readiness.readiness_level === 'limited_evidence') return '仅限当前小规模证据'
  if (readiness.readiness_level === 'engineering_smoke_only') return '仅工程 Smoke 报告'
  return '禁止实验结论'
}

function experimentScopeLabel(provenance: ExperimentProvenance | null) {
  if (!provenance) return '等待执行'
  if (!provenance.executed) return '未成功执行'
  if (provenance.experiment_scope === 'pipeline_smoke_test') return '工程 Smoke'
  if (provenance.experiment_scope === 'lightweight_real_benchmark') return '轻量真实基准'
  return '候选领域实验'
}

function claimBoundaryLabel(provenance: ExperimentProvenance | null) {
  if (!provenance) return '暂无结论边界'
  if (provenance.claim_status === 'limited_small_benchmark') return '仅支持小规模初步结论'
  return provenance.scientific_claims_allowed ? '可作为科研证据' : '不可直接当科研结论'
}

function readFileAsBase64(file: File): Promise<ReferencePdfUpload> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = typeof reader.result === 'string' ? reader.result : ''
      const [, contentBase64 = ''] = result.split(',', 2)
      resolve({ name: file.name, contentBase64 })
    }
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(file)
  })
}

function appendUniqueChatMessage(messages: ChatMessage[], incoming: ChatMessage) {
  const exists = messages.some((message) => message.id === incoming.id)
  if (exists) return messages
  return [...messages, incoming].sort((a, b) => a.timestamp - b.timestamp)
}

function exportContextCardAsMarkdown(elementId: string, filename: string) {
  const el = document.getElementById(elementId)
  if (el) {
    const text = el.textContent || el.innerText
    downloadMarkdown(text, filename)
  }
}

function exportContextCardAsPdf(elementId: string, title: string) {
  const el = document.getElementById(elementId)
  if (el) {
    const text = el.textContent || el.innerText
    printContentAsPdf(text, title)
  }
}

function artifactDownloadUrl(projectId: string | null, filename: string) {
  if (!projectId) return '#'
  return `/download/${encodeURIComponent(projectId)}/${encodeURIComponent(filename)}`
}

export default function ResearchLab() {
  const { token: authToken, logout: handleLogout } = useAuth()

  const [projects, setProjects] = useState<ProjectInfo[]>([])
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([])
  const [literaturePapers, setLiteraturePapers] = useState<PaperInfo[]>([])
  const [literatureLoading, setLiteratureLoading] = useState(false)
  const [literatureSchedules, setLiteratureSchedules] = useState<LiteratureSchedule[]>([])
  const [watchName, setWatchName] = useState('')
  const [watchTopic, setWatchTopic] = useState('')
  const [watchKeywords, setWatchKeywords] = useState('')
  const [watchIntervalHours, setWatchIntervalHours] = useState(24)
  const [watchLookbackDays, setWatchLookbackDays] = useState(30)
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null)
  const [connected, setConnected] = useState(false)
  const [topic, setTopic] = useState('')
  const [referencePapers, setReferencePapers] = useState('')
  const [mode, setMode] = useState<'auto' | 'upload' | 'hybrid'>('hybrid')
  const [runMode, setRunMode] = useState<RunMode>('idea_gate')
  const [modelName, setModelName] = useState(DEFAULT_QWEN3_MODEL)
  const [ideaCount, setIdeaCount] = useState(5)
  const [chatModelName, setChatModelName] = useState(DEFAULT_QWEN3_MODEL)
  const [chatInput, setChatInput] = useState('')
  const [referenceFiles, setReferenceFiles] = useState<ReferencePdfUpload[]>([])
  const [submitError, setSubmitError] = useState('')
  const [awaitingReplyForProject, setAwaitingReplyForProject] = useState<string | null>(null)
  const [restartFrom, setRestartFrom] = useState<RestartFrom>('ideas')
  const wsRef = useRef<WebSocket | null>(null)
  const selectedProjectIdRef = useRef<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const chatEndRef = useRef<HTMLDivElement | null>(null)
  const pendingReplyTimerRef = useRef<number | null>(null)

  useEffect(() => {
    selectedProjectIdRef.current = selectedProjectId
  }, [selectedProjectId])

  useEffect(() => {
    if (!authToken) return
    let ws: WebSocket | null = null
    let timer: ReturnType<typeof setTimeout> | null = null

    const connect = () => {
      ws = new WebSocket(AGENT_WS)
      wsRef.current = ws
      ws.onopen = () => {
        ws?.send(JSON.stringify({ command: 'auth', token: authToken }))
      }

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data) as WSMessage

          if (msg.type === 'auth_result') {
            if (msg.payload.ok && msg.payload.user) {
              setConnected(true)
              // Auth 成功后拉项目列表
              ws?.send(JSON.stringify({ command: 'list_projects' }))
              ws?.send(JSON.stringify({ command: 'list_literature_schedules' }))
            } else if (msg.payload.ok && !msg.payload.user) {
              setConnected(true)
              ws?.send(JSON.stringify({ command: 'list_projects' }))
            } else {
              setConnected(false)
              handleLogout()
              return
            }
            return
          }

          if (msg.type === 'project_list') {
            const visibleProjectIds = new Set(msg.payload.map((project) => project.projectId))
            setProjects(msg.payload)
            setArtifacts((prev) => prev.filter((artifact) => visibleProjectIds.has(artifact.projectId)))
            setChatMessages((prev) => prev.filter((message) => !message.projectId || visibleProjectIds.has(message.projectId)))
            if (selectedProjectIdRef.current && !visibleProjectIds.has(selectedProjectIdRef.current)) {
              setSelectedProjectId(null)
              setLiteraturePapers([])
            }
          } else if (msg.type === 'artifact_produced') {
            setArtifacts((prev) => {
              const key = `${msg.payload.projectId}:${msg.payload.stage ?? 0}:${msg.payload.filename}:${msg.payload.producedBy}`
              const idx = prev.findIndex((item) => `${item.projectId}:${item.stage ?? 0}:${item.filename}:${item.producedBy}` === key)
              if (idx >= 0) {
                const existing = prev[idx]
                if (msg.payload.content && msg.payload.content.length > (existing.content?.length || 0)) {
                  const updated = [...prev]
                  updated[idx] = { ...existing, content: msg.payload.content, status: msg.payload.status }
                  return updated
                }
                return prev
              }
              return [msg.payload, ...prev]
            })
            if (
              msg.payload.projectId === selectedProjectIdRef.current &&
              ['candidates.jsonl', 'shortlist.jsonl'].includes(msg.payload.filename)
            ) {
              setLiteratureLoading(true)
              ws?.send(JSON.stringify({
                command: 'list_project_literature',
                projectId: msg.payload.projectId,
              }))
            }
          } else if (msg.type === 'literature_list') {
            setLiteraturePapers(msg.payload.papers)
            setLiteratureLoading(false)
          } else if (msg.type === 'literature_schedule_list') {
            setLiteratureSchedules(msg.payload)
          } else if (msg.type === 'chat_message') {
            if (msg.payload.projectId && msg.payload.role === 'system') {
              setAwaitingReplyForProject((current) => (
                current === msg.payload.projectId ? null : current
              ))
              if (pendingReplyTimerRef.current) {
                window.clearTimeout(pendingReplyTimerRef.current)
                pendingReplyTimerRef.current = null
              }
            }
            setChatMessages((prev) => appendUniqueChatMessage(prev, msg.payload))
          }
        } catch {
          // ignore malformed messages
        }
      }

      ws.onclose = () => {
        setConnected(false)
        timer = setTimeout(connect, 3000)
      }

      ws.onerror = () => ws?.close()
    }

    connect()
    return () => {
      if (timer) clearTimeout(timer)
      ws?.close()
    }
  }, [authToken, handleLogout])

  useEffect(() => {
    if (!selectedProjectId && projects.length > 0) {
      setSelectedProjectId(projects[0].projectId)
    }
  }, [projects, selectedProjectId])

  useEffect(() => {
    const ws = wsRef.current
    if (!selectedProjectId || !ws || ws.readyState !== WebSocket.OPEN) return
    ws.send(JSON.stringify({ command: 'list_project_artifacts', projectId: selectedProjectId }))
    ws.send(JSON.stringify({ command: 'list_project_chat', projectId: selectedProjectId }))
    setLiteratureLoading(true)
    ws.send(JSON.stringify({ command: 'list_project_literature', projectId: selectedProjectId }))
  }, [selectedProjectId, connected, projects.length])

  const selectedProject = useMemo(
    () => projects.find((project) => project.projectId === selectedProjectId) ?? null,
    [projects, selectedProjectId],
  )

  const selectedArtifacts = useMemo(
    () => artifacts
      .filter((artifact) => artifact.projectId === selectedProjectId)
      .sort((a, b) => (b.stage ?? 0) - (a.stage ?? 0) || b.timestamp - a.timestamp),
    [artifacts, selectedProjectId],
  )

  const selectedChat = useMemo(
    () => chatMessages.filter((message) => !message.projectId || message.projectId === selectedProjectId),
    [chatMessages, selectedProjectId],
  )

  const latestSynthesisArtifact = useMemo(
    () => pickLatestArtifact(selectedArtifacts, ['synthesis.md', 'shortlist.jsonl', 'candidates.jsonl', 'reference_paper_text.md', 'web_context.md']),
    [selectedArtifacts],
  )
  const latestIdeaArtifact = useMemo(
    () => pickLatestArtifact(selectedArtifacts, ['core_ideas.md', 'hypotheses.md']),
    [selectedArtifacts],
  )
  const latestFinalPaperArtifact = useMemo(
    () => pickLatestArtifact(selectedArtifacts, ['paper_final.md', 'paper_final_verified.md']),
    [selectedArtifacts],
  )
  const latestQualityArtifact = useMemo(
    () => pickLatestArtifact(selectedArtifacts, ['quality_report.json']),
    [selectedArtifacts],
  )
  const latestVerifyArtifact = useMemo(
    () => pickLatestArtifact(selectedArtifacts, ['verification_report.json']),
    [selectedArtifacts],
  )
  const latestManifestArtifact = useMemo(
    () => pickLatestArtifact(selectedArtifacts, ['manifest.json']),
    [selectedArtifacts],
  )
  const latestExperimentProvenanceArtifact = useMemo(
    () => pickLatestArtifact(selectedArtifacts, ['experiment_provenance.json']),
    [selectedArtifacts],
  )
  const latestExpPlanDiagnosticsArtifact = useMemo(
    () => pickLatestArtifact(selectedArtifacts, ['exp_plan_diagnostics.json']),
    [selectedArtifacts],
  )
  const latestResearchReadinessArtifact = useMemo(
    () => pickLatestArtifact(selectedArtifacts, ['research_readiness.json']),
    [selectedArtifacts],
  )
  const latestClaimIntegrityArtifact = useMemo(
    () => pickLatestArtifact(selectedArtifacts, ['claim_integrity_report.json']),
    [selectedArtifacts],
  )
  const latestFinalClaimIntegrityArtifact = useMemo(
    () => pickLatestArtifact(selectedArtifacts, ['final_claim_integrity_report.json']),
    [selectedArtifacts],
  )
  const latestPipelineSummaryArtifact = useMemo(
    () => pickLatestArtifact(selectedArtifacts, ['pipeline_summary.json']),
    [selectedArtifacts],
  )
  const experimentProvenance = useMemo(
    () => parseExperimentProvenance(latestExperimentProvenanceArtifact),
    [latestExperimentProvenanceArtifact],
  )
  const expPlanDiagnostics = useMemo(
    () => parseExpPlanDiagnostics(latestExpPlanDiagnosticsArtifact),
    [latestExpPlanDiagnosticsArtifact],
  )
  const researchReadiness = useMemo(
    () => parseResearchReadiness(latestResearchReadinessArtifact),
    [latestResearchReadinessArtifact],
  )
  const claimIntegrity = useMemo(
    () => parseClaimIntegrityReport(latestClaimIntegrityArtifact),
    [latestClaimIntegrityArtifact],
  )

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [selectedChat.length, selectedProjectId])

  const submitProject = () => {
    const ws = wsRef.current
    const cleanTopic = topic.trim()
    if (!ws || ws.readyState !== WebSocket.OPEN || !cleanTopic) return
    if (mode === 'upload' && !referencePapers.trim() && referenceFiles.length === 0) {
      setSubmitError('上传优先模式至少需要一篇参考文献或一个 PDF 文件。')
      return
    }
    setSubmitError('')

    const outboundReferencePapers = mode === 'auto' ? undefined : (referencePapers.trim() || undefined)
    const outboundReferenceFiles = mode === 'auto' ? undefined : (referenceFiles.length > 0 ? referenceFiles : undefined)

    ws.send(JSON.stringify({
      command: 'quick_submit',
      topic: cleanTopic,
      mode: 'lab',
      submissionMode: mode,
      runMode,
      modelName,
      ideaCount,
      referencePapers: outboundReferencePapers,
      referenceFiles: outboundReferenceFiles,
    }))

    setTopic('')
    setReferencePapers('')
    setReferenceFiles([])
  }

  const createLiteratureSchedule = () => {
    const ws = wsRef.current
    const cleanTopic = watchTopic.trim()
    if (!ws || ws.readyState !== WebSocket.OPEN || !cleanTopic) return
    ws.send(JSON.stringify({
      command: 'create_literature_schedule',
      name: watchName.trim() || cleanTopic,
      topic: cleanTopic,
      keywords: watchKeywords,
      intervalHours: watchIntervalHours,
      lookbackDays: watchLookbackDays,
      sources: ['arxiv', 'openalex', 'semantic_scholar'],
      runImmediately: true,
    }))
    setWatchName('')
    setWatchTopic('')
    setWatchKeywords('')
  }

  const scheduleCommand = (command: string, schedule: LiteratureSchedule, enabled?: boolean) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    ws.send(JSON.stringify({ command, scheduleId: schedule.id, enabled }))
  }

  const submitChat = () => {
    const ws = wsRef.current
    const content = chatInput.trim()
    if (!ws || ws.readyState !== WebSocket.OPEN || !selectedProjectId || !content) return
    const clientMessageId = `user-${selectedProjectId}-${Date.now()}`
    setChatMessages((prev) => appendUniqueChatMessage(prev, {
      id: clientMessageId,
      role: 'user',
      content,
      projectId: selectedProjectId,
      timestamp: Date.now(),
    }))
    setAwaitingReplyForProject(selectedProjectId)
    if (pendingReplyTimerRef.current) {
      window.clearTimeout(pendingReplyTimerRef.current)
    }
    pendingReplyTimerRef.current = window.setTimeout(() => {
      setAwaitingReplyForProject((current) => (current === selectedProjectId ? null : current))
      setChatMessages((prev) => appendUniqueChatMessage(prev, {
        id: `timeout-${selectedProjectId}-${Date.now()}`,
        role: 'system',
        content: '这次回复超时了。现在网页对话已经切成更短超时模式了，如果还经常出现，通常是接口波动或者当前问题上下文过长。你可以重新发送一次，或者把问题问得更聚焦一点。',
        projectId: selectedProjectId,
        timestamp: Date.now(),
      }))
    }, 55000)
    ws.send(JSON.stringify({
      command: 'project_chat',
      projectId: selectedProjectId,
      content,
      clientMessageId,
      chatModelName,
    }))
    setChatInput('')
  }

  const onChatKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      submitChat()
    }
  }

  const onPickFiles = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []).filter(
      (file) => file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf'),
    )
    const uploads = await Promise.all(files.map(readFileAsBase64))
    setReferenceFiles((prev) => {
      const existing = new Set(prev.map((item) => `${item.name}:${item.contentBase64.length}`))
      const merged = [...prev]
      for (const upload of uploads) {
        const key = `${upload.name}:${upload.contentBase64.length}`
        if (!existing.has(key)) {
          merged.push(upload)
          existing.add(key)
        }
      }
      return merged
    })
    event.target.value = ''
  }

  const sendProjectCommand = (command: 'pause_project' | 'resume_project' | 'restart_project' | 'delete_project') => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN || !selectedProjectId) return
    if (command === 'restart_project') {
      const label = RESTART_OPTIONS.find((option) => option.value === restartFrom)?.label || ''
      if (!window.confirm(`确认将项目回退到「${label}」并重新运行？\n所选内容及其后续产物会重新生成。`)) return
      ws.send(JSON.stringify({ command, projectId: selectedProjectId, restartFrom }))
    } else {
      ws.send(JSON.stringify({ command, projectId: selectedProjectId }))
    }
    if (command === 'delete_project') {
      setSelectedProjectId(null)
      setArtifacts((prev) => prev.filter((artifact) => artifact.projectId !== selectedProjectId))
      setChatMessages((prev) => prev.filter((message) => message.projectId !== selectedProjectId))
      setLiteraturePapers([])
    }
  }

  const quickPrompts = [
    '详细总结文献结论',
    '深入细化核心 idea',
    '设计最小验证方案',
    '分析相似工作区别',
    '提出改进方向',
    '分析实验风险',
  ]
  const quickPromptText: Record<string, string> = {
    '详细总结文献结论': '请详细总结当前项目最重要的文献结论，包括每篇关键论文的方法、发现和局限性，以及这些文献之间的关联和冲突。要求引用具体论文。',
    '深入细化核心 idea': '请把当前核心 idea 展开成一个详细的技术方案，包括具体架构设计、训练策略、预期困难和备选方案。越具体越好。',
    '设计最小验证方案': '如果要做最小可行验证来检验核心假设，请设计一个完整的实验方案：使用什么数据集、什么模型、评估什么指标、预期什么结果、多长时间能完成。',
    '分析相似工作区别': '有哪些与本研究最相近的已有工作？我们需要在方法上、实验上或目标上如何与它们区分？请逐一对每个相近工作进行详细对比分析。',
    '提出改进方向': '基于现有项目资料，有哪些值得探索的改进方向或变体？请给出 2-3 个具体建议，每个建议包括动机、方法概述和预期收益。',
    '分析实验风险': '当前实验方案有哪些潜在风险？包括技术风险、计算资源风险、方法可行性风险。请逐一分析并提出缓解策略。',
  }
  const isAwaitingReply = awaitingReplyForProject === selectedProjectId
  const needsIdeaConfirmation = selectedProject?.status === 'interrupted' &&
    typeof selectedProject.intervention === 'string' &&
    selectedProject.intervention.startsWith('idea_review')

  const sendConfirmIdea = () => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN || !selectedProjectId) return
    ws.send(JSON.stringify({ command: 'confirm_project_ideas', projectId: selectedProjectId }))
  }

  return (
    <div className="web-layout">
      <aside className="sidebar">
        <section className="panel">
          <div className="panel-heading">
            <h2>新项目</h2>
            <span className={`connection-pill ${connected ? 'on' : 'off'}`}>
              {connected ? '在线' : '离线'}
            </span>
          </div>
          <div className="field-group">
            <label>主题</label>
            <textarea
              rows={3}
              placeholder="研究主题"
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
            />
          </div>

          <div className="field-group">
            <label>模式</label>
            <div className="mode-grid">
              <button className={mode === 'auto' ? 'active' : ''} onClick={() => setMode('auto')}>检索</button>
              <button className={mode === 'upload' ? 'active' : ''} onClick={() => setMode('upload')}>上传</button>
              <button className={mode === 'hybrid' ? 'active' : ''} onClick={() => setMode('hybrid')}>混合</button>
            </div>
          </div>

          <div className="field-group">
            <label>模型</label>
            <select className="model-select" value={modelName} onChange={(e) => setModelName(e.target.value)}>
              {MODEL_OPTIONS.map((model) => (
                <option key={model.value} value={model.value}>{model.label}</option>
              ))}
            </select>
          </div>

          <div className="field-group">
            <label>Idea 数量</label>
            <div className="idea-count-grid" role="group" aria-label="Idea 数量">
              {IDEA_COUNT_OPTIONS.map((count) => (
                <button
                  key={count}
                  type="button"
                  className={ideaCount === count ? 'active' : ''}
                  onClick={() => setIdeaCount(count)}
                >
                  {count}
                </button>
              ))}
            </div>
          </div>

          <div className="field-group">
            <label>运行策略</label>
            <div className="mode-grid">
              <button
                type="button"
                className={runMode === 'idea_gate' ? 'active' : ''}
                onClick={() => setRunMode('idea_gate')}
              >
                先出 Idea
              </button>
              <button
                type="button"
                className={runMode === 'full_chain' ? 'active' : ''}
                onClick={() => setRunMode('full_chain')}
              >
                全链路 IEEE 论文
              </button>
            </div>
            <p className="field-hint">
              {runMode === 'idea_gate'
                ? '生成 S8 Idea 后暂停，确认后进入 L2 实验设计。'
                : '自动完成实验设计、代码生成、真实执行、结果分析，并交付完整 IEEE 双栏论文与 PDF。'}
            </p>
          </div>

          <div className="field-group">
            <label>参考文献</label>
            <textarea
              rows={3}
              placeholder="标题 / DOI / arXiv"
              value={referencePapers}
              onChange={(e) => setReferencePapers(e.target.value)}
            />
          </div>

          <div className="field-group">
            <label>PDF</label>
            <div className="upload-row">
              <button type="button" onClick={() => fileInputRef.current?.click()}>上传</button>
              <input ref={fileInputRef} type="file" accept=".pdf,application/pdf" multiple hidden onChange={onPickFiles} />
              <span>{referenceFiles.length > 0 ? `${referenceFiles.length} 个` : '未选择'}</span>
            </div>
            {referenceFiles.length > 0 && (
              <ul className="upload-list">
                {referenceFiles.map((file) => (
                  <li key={`${file.name}:${file.contentBase64.length}`}>{file.name}</li>
                ))}
              </ul>
            )}
          </div>

          <button className="primary-button" onClick={submitProject} disabled={!connected || !topic.trim()}>
            运行
          </button>
          {submitError && <p className="submit-error">{submitError}</p>}
        </section>

        <section className="panel literature-watch-panel">
          <div className="panel-heading">
            <h2>定期文献追踪</h2>
            <span>{literatureSchedules.length}</span>
          </div>
          <p className="field-hint">定期任务只运行文献检索、筛选和中文综述，不会自动占用资源跑实验。</p>
          <div className="field-group">
            <label>任务名称</label>
            <input value={watchName} onChange={(e) => setWatchName(e.target.value)} placeholder="例如：IMU 感知周报" />
          </div>
          <div className="field-group">
            <label>追踪主题</label>
            <textarea rows={2} value={watchTopic} onChange={(e) => setWatchTopic(e.target.value)} placeholder="需要持续追踪的研究方向" />
          </div>
          <div className="field-group">
            <label>关键词</label>
            <input value={watchKeywords} onChange={(e) => setWatchKeywords(e.target.value)} placeholder="逗号分隔，可中英文混合" />
          </div>
          <div className="schedule-config-row">
            <label>
              周期
              <select value={watchIntervalHours} onChange={(e) => setWatchIntervalHours(Number(e.target.value))}>
                <option value={24}>每天</option>
                <option value={72}>每 3 天</option>
                <option value={168}>每周</option>
                <option value={336}>每两周</option>
              </select>
            </label>
            <label>
              检索范围
              <select value={watchLookbackDays} onChange={(e) => setWatchLookbackDays(Number(e.target.value))}>
                <option value={7}>近 7 天</option>
                <option value={30}>近 30 天</option>
                <option value={90}>近 90 天</option>
              </select>
            </label>
          </div>
          <button className="primary-button" onClick={createLiteratureSchedule} disabled={!connected || !watchTopic.trim()}>
            创建并立即执行
          </button>

          <div className="literature-watch-list">
            {literatureSchedules.map((schedule) => (
              <article key={schedule.id} className="literature-watch-item">
                <div className="literature-watch-title">
                  <strong>{schedule.name}</strong>
                  <span className={`watch-status ${schedule.status}`}>{schedule.status === 'running' ? '运行中' : schedule.enabled ? '已启用' : '已暂停'}</span>
                </div>
                <p>{schedule.topic}</p>
                <small>
                  {schedule.intervalHours === 24 ? '每天' : schedule.intervalHours === 168 ? '每周' : `每 ${schedule.intervalHours} 小时`}
                  {' · '}新增 {schedule.lastNewPaperCount ?? 0} 篇
                  {schedule.nextRunAt > 0 && ` · 下次 ${formatTime(schedule.nextRunAt)}`}
                </small>
                {schedule.lastError && <p className="literature-watch-error">{schedule.lastError}</p>}
                <div className="literature-watch-actions">
                  <button type="button" onClick={() => scheduleCommand('run_literature_schedule', schedule)} disabled={schedule.status === 'running'}>立即执行</button>
                  <button type="button" onClick={() => scheduleCommand('toggle_literature_schedule', schedule, !schedule.enabled)}>{schedule.enabled ? '暂停' : '启用'}</button>
                  {schedule.lastProjectId && <button type="button" onClick={() => setSelectedProjectId(schedule.lastProjectId || null)}>查看综述</button>}
                  <button type="button" className="danger-text" onClick={() => scheduleCommand('delete_literature_schedule', schedule)}>删除</button>
                </div>
              </article>
            ))}
          </div>
        </section>

        <section className="panel project-list-panel">
          <div className="panel-heading">
            <h2>项目</h2>
            <span>{projects.length}</span>
          </div>
          <div className="project-list">
            {projects.map((project) => (
              <button
                key={project.projectId}
                className={`project-item ${project.projectId === selectedProjectId ? 'selected' : ''}`}
                onClick={() => setSelectedProjectId(project.projectId)}
              >
                <div className="project-item-top">
                  <strong>{project.projectId}</strong>
                  <span className={`status-dot status-${project.status}`}>{project.status}</span>
                </div>
                <p>{project.topic || '未记录主题'}</p>
                <div className="project-item-meta">
                  <span>{project.lastCompletedName || '未开始'}</span>
                  <span>Stage {project.lastCompletedStage || 0}</span>
                  <span>{project.runMode === 'literature_watch' ? '文献追踪' : project.runMode === 'idea_gate' ? 'Idea 确认' : 'IEEE 全链路'}</span>
                </div>
              </button>
            ))}
          </div>
        </section>
      </aside>

      <section className="workspace">
        {selectedProject ? (
          <>
            <div className="workspace-header compact-chat-header">
              <div>
                <h2>{selectedProject.topic || selectedProject.projectId}</h2>
              </div>
              <div className="workspace-badges">
                <span className={`status-pill status-${selectedProject.status}`}>{selectedProject.status}</span>
                <span className="meta-pill">{selectedProject.lastCompletedName || '未开始'}</span>
                <span className="meta-pill">{selectedProject.runMode === 'literature_watch' ? '增量文献综述' : selectedProject.runMode === 'idea_gate' ? '先出 Idea' : '完整 IEEE 论文'}</span>
              </div>
            </div>
            <div className="research-pipeline-progress">
              <div className="research-pipeline-progress-head">
                <strong>全链路进度</strong>
                <span>
                  S{Math.min(selectedProject.lastCompletedStage || 0, 26)}/26 ·
                  {Math.round(Math.min(selectedProject.lastCompletedStage || 0, 26) / 26 * 100)}%
                </span>
              </div>
              <div className="research-pipeline-bar">
                <span style={{ width: `${Math.min(selectedProject.lastCompletedStage || 0, 26) / 26 * 100}%` }} />
              </div>
              <div className="research-pipeline-phases">
                {PIPELINE_PHASES.map((phase) => {
                  const done = selectedProject.lastCompletedStage >= phase.end
                  const active = selectedProject.lastCompletedStage >= phase.start - 1
                    && selectedProject.lastCompletedStage < phase.end
                  return (
                    <div key={phase.key} className={done ? 'done' : active ? 'active' : ''}>
                      <b>{done ? '✓' : phase.key}</b>
                      <span>{phase.label}</span>
                      <small>S{phase.start}–S{phase.end}</small>
                    </div>
                  )
                })}
              </div>
            </div>

            <div className="workspace-body chat-first-layout">
              <section className="chat-shell">
                <div className="chat-shell-header">
                  <div>
                    <strong>对话</strong>
                  </div>
                  <div className="chat-header-controls">
                    <select className="model-select chat-model-select" value={chatModelName} onChange={(e) => setChatModelName(e.target.value)}>
                      {MODEL_OPTIONS.map((model) => (
                        <option key={model.value} value={model.value}>{model.label}</option>
                      ))}
                    </select>
                    <span className="meta-pill">Stage {selectedProject.lastCompletedStage || 0}</span>
                  </div>
                </div>

                <div className="quick-actions quick-actions-chat">
                  {quickPrompts.map((prompt) => (
                    <button key={prompt} onClick={() => setChatInput(quickPromptText[prompt])}>
                      {prompt}
                    </button>
                  ))}
                </div>

                <div className="chat-history chat-history-chatlike">
                  {selectedChat.length === 0 ? (
                    <>
                      <article className="chat-bubble system welcome-bubble">
                        <span>助手</span>
                        <p>问我这个项目的文献、综述或 idea。</p>
                      </article>
                    </>
                  ) : (
                    selectedChat.map((message) => (
                      <article key={message.id} className={`chat-bubble ${message.role}`}>
                        <span>{message.role === 'user' ? '你' : '助手'}</span>
                        <p>{message.content}</p>
                        <time>{formatTime(message.timestamp)}</time>
                      </article>
                    ))
                  )}
                  {isAwaitingReply && (
                    <article className="chat-bubble system pending-bubble">
                      <span>助手</span>
                      <p>思考中...</p>
                    </article>
                  )}
                  <div ref={chatEndRef} />
                </div>

                <div className="chat-compose chat-compose-chatlike">
                  <textarea
                    rows={3}
                    placeholder="问这个项目..."
                    value={chatInput}
                    onChange={(e) => setChatInput(e.target.value)}
                    onKeyDown={onChatKeyDown}
                  />
                  <div className="chat-compose-actions">
                    <span>Enter 发送</span>
                    <button className="primary-button chat-send-button" onClick={submitChat} disabled={!selectedProjectId || !chatInput.trim() || isAwaitingReply}>
                      {isAwaitingReply ? '等待回复…' : '发送'}
                    </button>
                  </div>
                </div>
              </section>

              <aside className="context-rail">
                <section className="artifact-section focus-card context-card">
                  <div className="panel-heading">
                    <h3>控制</h3>
                    <span>{selectedArtifacts.length}</span>
                  </div>
                  <div className="workspace-actions workspace-actions-compact">
                    <button onClick={() => sendProjectCommand('pause_project')} disabled={selectedProject.status !== 'running'}>
                      暂停
                    </button>
                    <button onClick={() => sendProjectCommand('resume_project')} disabled={selectedProject.status === 'running'}>
                      恢复
                    </button>
                    <div className="restart-control">
                      <select
                        value={restartFrom}
                        onChange={(event) => setRestartFrom(event.target.value as RestartFrom)}
                        aria-label="选择回退内容"
                      >
                        {RESTART_OPTIONS.map((option) => (
                          <option key={option.value} value={option.value}>{option.label}</option>
                        ))}
                      </select>
                      <button onClick={() => sendProjectCommand('restart_project')}>
                        回退重跑
                      </button>
                    </div>
                    <button className="danger-button" onClick={() => sendProjectCommand('delete_project')}>
                      删除
                    </button>
                  </div>
                </section>

                <section className="artifact-section focus-card context-card">
                  <div className="panel-heading">
                    <h3>实验状态</h3>
                    <span className={experimentProvenance?.executed ? 'status-completed' : 'status-new'}>
                      {experimentScopeLabel(experimentProvenance)}
                    </span>
                  </div>
                  <div className="experiment-status-grid">
                    <span className={experimentProvenance?.real_code_execution ? 'evidence-pill evidence-ok' : 'evidence-pill evidence-wait'}>
                      {experimentProvenance?.real_code_execution ? '代码已实际运行' : '代码待运行'}
                    </span>
                    <span className={experimentProvenance?.scientific_claims_allowed ? 'evidence-pill evidence-ok' : 'evidence-pill evidence-warn'}>
                      {claimBoundaryLabel(experimentProvenance)}
                    </span>
                    {expPlanDiagnostics && (
                      <span className={expPlanDiagnostics.degraded ? 'evidence-pill evidence-warn' : 'evidence-pill evidence-ok'}>
                        {expPlanDiagnostics.degraded ? '实验计划降级' : '实验计划正常'}
                      </span>
                    )}
                    {researchReadiness && (
                      <span className={researchReadiness.scientific_claims_allowed ? 'evidence-pill evidence-ok' : 'evidence-pill evidence-warn'}>
                        {readinessLabel(researchReadiness)} · {researchReadiness.readiness_score ?? 0}/100
                      </span>
                    )}
                  </div>
                  <pre>
                    {experimentProvenance?.display_status_zh
                      || 'S14 执行后会在这里说明代码是否真实运行，以及结果可用于 Smoke 还是科研结论。'}
                  </pre>
                  {expPlanDiagnostics?.user_facing_status_zh && (
                    <pre>{expPlanDiagnostics.user_facing_status_zh}</pre>
                  )}
                  {researchReadiness?.user_facing_status_zh && (
                    <pre>{researchReadiness.user_facing_status_zh}</pre>
                  )}
                  {researchReadiness?.recommended_actions && researchReadiness.recommended_actions.length > 0 && (
                    <div className="evidence-warning">
                      下一步优先补充：{researchReadiness.recommended_actions.slice(0, 3).join('；')}
                    </div>
                  )}
                  {researchReadiness?.forced_proceed_after_max_pivots && (
                    <div className="evidence-warning">
                      已达到自动迭代上限并继续生成受限报告；科研判断仍为 REFINE/PIVOT，不能视为实验已通过。
                    </div>
                  )}
                  {expPlanDiagnostics?.benchmark_agent_errors && expPlanDiagnostics.benchmark_agent_errors.length > 0 && (
                    <div className="evidence-warning">
                      BenchmarkAgent 校验未通过：{expPlanDiagnostics.benchmark_agent_errors.slice(0, 2).join('；')}
                    </div>
                  )}
                  {(experimentProvenance || expPlanDiagnostics || researchReadiness) && (
                    <div className="final-artifact-meta">
                      {experimentProvenance && (
                        <details>
                          <summary>执行与用途明细</summary>
                          <pre>{JSON.stringify(experimentProvenance, null, 2)}</pre>
                        </details>
                      )}
                      {expPlanDiagnostics && (
                        <details>
                          <summary>实验计划诊断</summary>
                          <pre>{JSON.stringify(expPlanDiagnostics, null, 2)}</pre>
                        </details>
                      )}
                      {researchReadiness && (
                        <details>
                          <summary>科研就绪度与写作边界</summary>
                          <pre>{JSON.stringify(researchReadiness, null, 2)}</pre>
                        </details>
                      )}
                      <div className="context-card-export">
                        <a className="export-btn" href={artifactDownloadUrl(selectedProjectId, 'experiment_provenance.json')} target="_blank" rel="noreferrer">真实性记录</a>
                        <a className="export-btn" href={artifactDownloadUrl(selectedProjectId, 'exp_plan_diagnostics.json')} target="_blank" rel="noreferrer">计划诊断</a>
                        <a className="export-btn" href={artifactDownloadUrl(selectedProjectId, 'experiment_summary.json')} target="_blank" rel="noreferrer">实验摘要</a>
                        <a className="export-btn" href={artifactDownloadUrl(selectedProjectId, 'analysis.md')} target="_blank" rel="noreferrer">结果分析</a>
                        <a className="export-btn" href={artifactDownloadUrl(selectedProjectId, 'research_readiness.json')} target="_blank" rel="noreferrer">科研就绪度</a>
                      </div>
                    </div>
                  )}
                </section>

                <section className="artifact-section focus-card context-card">
                  <div className="panel-heading">
                    <h3>综述</h3>
                    <span>{latestSynthesisArtifact ? '已生成' : '暂无'}</span>
                  </div>
                  {latestSynthesisArtifact && (
                    <div className="context-card-export">
                      <button
                        className="export-btn"
                        onClick={() => exportContextCardAsMarkdown('synthesis-content', `${selectedProjectId}-synthesis.md`)}
                        title="下载 Markdown"
                      >⬇ MD</button>
                      <button
                        className="export-btn"
                        onClick={() => exportContextCardAsPdf('synthesis-content', `综述 ${selectedProjectId}`)}
                        title="导出 PDF"
                      >📄 PDF</button>
                    </div>
                  )}
                  <pre id="synthesis-content">{latestSynthesisArtifact?.content || '暂无综述。'}</pre>
                </section>

                <section className="artifact-section focus-card highlight-card context-card">
                  <div className="panel-heading">
                    <h3>Idea</h3>
                    <span>{latestIdeaArtifact ? '已生成' : '等待生成'}</span>
                  </div>
                  {latestIdeaArtifact && (
                    <div className="context-card-export">
                      <button
                        className="export-btn"
                        onClick={() => exportContextCardAsMarkdown('idea-content', `${selectedProjectId}-idea.md`)}
                        title="下载 Markdown"
                      >⬇ MD</button>
                      <button
                        className="export-btn"
                        onClick={() => exportContextCardAsPdf('idea-content', `Idea ${selectedProjectId}`)}
                        title="导出 PDF"
                      >📄 PDF</button>
                    </div>
                  )}
                  <pre id="idea-content">{latestIdeaArtifact?.content || '暂无核心想法。'}</pre>
                  {needsIdeaConfirmation && (
                    <div className="idea-confirm-bar">
                      <div className="idea-confirm-message">Idea 已生成，请审阅后确认进入 L2 实验设计</div>
                      <button className="idea-confirm-btn" onClick={sendConfirmIdea}>
                        ✓ 确认 Idea → 继续 L2
                      </button>
                    </div>
                  )}
                  {!needsIdeaConfirmation && latestIdeaArtifact && selectedProject.runMode === 'idea_gate' && (
                    <div className="idea-confirm-bar">
                      <div className="idea-confirm-message">Idea 已生成，等待项目状态进入“待确认”后即可继续 L2。</div>
                    </div>
                  )}
                </section>

                <section className="artifact-section focus-card context-card">
                  <div className="panel-heading">
                    <h3>最终产物</h3>
                    <span>{latestFinalPaperArtifact ? '已生成' : '等待全链路'}</span>
                  </div>
                  {latestFinalPaperArtifact && (
                    <>
                      <div className="context-card-export">
                        <button
                          className="export-btn"
                          onClick={() => exportContextCardAsMarkdown('final-paper-content', `${selectedProjectId}-paper-final.md`)}
                          title="下载 Markdown"
                        >⬇ MD</button>
                        <button
                          className="export-btn"
                          onClick={() => exportContextCardAsPdf('final-paper-content', `最终稿 ${selectedProjectId}`)}
                          title="导出 PDF"
                        >📄 PDF</button>
                        <a className="export-btn" href={artifactDownloadUrl(selectedProjectId, 'paper_final.md')} target="_blank" rel="noreferrer">paper.md</a>
                        <a className="export-btn" href={artifactDownloadUrl(selectedProjectId, 'paper.pdf')} target="_blank" rel="noreferrer">paper.pdf</a>
                        <a className="export-btn" href={artifactDownloadUrl(selectedProjectId, 'paper.tex')} target="_blank" rel="noreferrer">paper.tex</a>
                        <a className="export-btn" href={artifactDownloadUrl(selectedProjectId, 'references.bib')} target="_blank" rel="noreferrer">bib</a>
                      </div>
                      <pre id="final-paper-content">{latestFinalPaperArtifact.content || '最终稿已生成，可通过上方按钮下载。'}</pre>
                    </>
                  )}
                  {!latestFinalPaperArtifact && (
                    <pre>全链路到 S25/S26 后会在这里显示最终稿和导出文件。</pre>
                  )}
                  {(latestQualityArtifact || latestClaimIntegrityArtifact || latestFinalClaimIntegrityArtifact || latestVerifyArtifact || latestManifestArtifact) && (
                    <div className="final-artifact-meta">
                      {claimIntegrity && (
                        <>
                          <div className="experiment-status-grid">
                            <span className={claimIntegrity.status === 'passed' ? 'evidence-pill evidence-ok' : 'evidence-pill evidence-warn'}>
                              结论完整性 {claimIntegrity.status === 'passed' ? '通过' : claimIntegrity.status === 'blocked' ? '未通过' : '需确认'} · {claimIntegrity.integrity_score ?? 0}/100
                            </span>
                            <span className={claimIntegrity.has_limitations_section ? 'evidence-pill evidence-ok' : 'evidence-pill evidence-warn'}>
                              {claimIntegrity.has_limitations_section ? '已说明局限性' : '缺少局限性'}
                            </span>
                          </div>
                          <pre>{claimIntegrity.user_facing_status_zh}</pre>
                          {claimIntegrity.recommended_actions && claimIntegrity.recommended_actions.length > 0 && (
                            <div className="evidence-warning">
                              优先修复：{claimIntegrity.recommended_actions.slice(0, 3).join('；')}
                            </div>
                          )}
                          <details>
                            <summary>结论与实验对齐报告</summary>
                            <pre>{latestClaimIntegrityArtifact?.content || latestClaimIntegrityArtifact?.size}</pre>
                          </details>
                          <div className="context-card-export">
                            <a className="export-btn" href={artifactDownloadUrl(selectedProjectId, 'claim_integrity_report.json')} target="_blank" rel="noreferrer">结论完整性</a>
                            {latestFinalClaimIntegrityArtifact && (
                              <a className="export-btn" href={artifactDownloadUrl(selectedProjectId, 'final_claim_integrity_report.json')} target="_blank" rel="noreferrer">最终稿复核</a>
                            )}
                            <a className="export-btn" href={artifactDownloadUrl(selectedProjectId, 'reproducibility_manifest.json')} target="_blank" rel="noreferrer">复现清单</a>
                            <a className="export-btn" href={artifactDownloadUrl(selectedProjectId, 'pipeline_summary.json')} target="_blank" rel="noreferrer">链路状态</a>
                            <a className="export-btn" href={artifactDownloadUrl(selectedProjectId, 'pdf_review.json')} target="_blank" rel="noreferrer">PDF 评审</a>
                          </div>
                        </>
                      )}
                      {latestQualityArtifact && (
                        <details>
                          <summary>质量报告</summary>
                          <pre>{latestQualityArtifact.content || latestQualityArtifact.size}</pre>
                        </details>
                      )}
                      {latestFinalClaimIntegrityArtifact && (
                        <details>
                          <summary>最终导出结论复核</summary>
                          <pre>{latestFinalClaimIntegrityArtifact.content || latestFinalClaimIntegrityArtifact.size}</pre>
                        </details>
                      )}
                      {latestPipelineSummaryArtifact && (
                        <details>
                          <summary>全链路状态与降级原因</summary>
                          <pre>{latestPipelineSummaryArtifact.content || latestPipelineSummaryArtifact.size}</pre>
                        </details>
                      )}
                      {latestVerifyArtifact && (
                        <details>
                          <summary>引用校验</summary>
                          <pre>{latestVerifyArtifact.content || latestVerifyArtifact.size}</pre>
                        </details>
                      )}
                      {latestManifestArtifact && (
                        <details>
                          <summary>导出清单</summary>
                          <pre>{latestManifestArtifact.content || latestManifestArtifact.size}</pre>
                        </details>
                      )}
                    </div>
                  )}
                </section>

                <LiteraturePanel papers={literaturePapers} loading={literatureLoading} />
              </aside>
            </div>
          </>
        ) : (
          <div className="empty-workspace">
            <h2>选择项目</h2>
          </div>
        )}
      </section>
    </div>
  )
}
