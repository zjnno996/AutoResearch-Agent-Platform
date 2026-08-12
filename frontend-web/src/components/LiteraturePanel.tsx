import { memo, useMemo } from 'react'

export type PaperInfo = {
  paper_id: string
  title: string
  authors: string[]
  year: number
  venue: string
  abstract: string
  citation_count: number
  doi: string
  url: string
  source: string
  ccf_tier?: string
}

interface Props {
  papers: PaperInfo[]
  loading: boolean
}

function extractKeywords(title: string, abstract: string): string[] {
  const text = `${title} ${abstract}`.toLowerCase()
  const techTerms = [
    'transformer', 'attention', 'reinforcement learning', 'deep learning',
    'neural network', 'cnn', 'rnn', 'lstm', 'gpt', 'llm', 'large language model',
    'diffusion', 'gan', 'generative', 'representation learning', 'self-supervised',
    'contrastive learning', 'transfer learning', 'few-shot', 'zero-shot',
    'multi-modal', 'multimodal', 'vision-language', 'graph neural network',
    'gnn', 'bayesian', 'variational', 'autoencoder', 'normalization',
    'convolution', 'embedding', 'tokenization', 'fine-tuning', 'prompt',
    'distillation', 'quantization', 'pruning', 'sparse', 'federated',
    'meta-learning', 'active learning', 'semi-supervised', 'unsupervised',
    'supervised', 'encoder', 'decoder', 'architecture', 'optimization',
    'regularization', 'dropout', 'batch norm', 'layer norm',
    'reinforce', 'policy gradient', 'q-learning', 'monte carlo',
    'simulation', 'digital twin', 'edge computing', 'cloud',
    'privacy', 'security', 'robustness', 'adversarial', 'explainability',
    'causal', 'probabilistic', 'graph', 'sequence', 'temporal',
    'embed', 'retrieval', 'ranking', 'recommendation', 'segmentation',
    'detection', 'recognition', 'classification', 'regression',
    'clustering', 'dimensionality reduction', 'pca', 'svm',
    'random forest', 'gradient boosting', 'ensemble',
  ]
  const found = new Set<string>()
  for (const term of techTerms) {
    if (text.includes(term)) {
      found.add(term)
    }
  }
  const matched = [...found]
  return matched.slice(0, 5)
}

function truncateAbstract(text: string, max: number): string {
  if (!text || text.length <= max) return text || ''
  return text.slice(0, max) + '…'
}

function PaperCard({ paper }: { paper: PaperInfo }) {
  const keywords = useMemo(() => extractKeywords(paper.title, paper.abstract), [paper])
  const authorsStr = paper.authors?.slice(0, 4).join(', ') + (paper.authors?.length > 4 ? ' ...' : '')

  return (
    <div className="literature-card">
      <div className="literature-card-title">
        {paper.title}
        {paper.ccf_tier && (
          <span className={`ccf-badge ccf-${paper.ccf_tier.toLowerCase().replace('/', '-')}`}>{paper.ccf_tier}</span>
        )}
      </div>
      <div className="literature-card-meta">
        <span className="literature-year">{paper.year || 'N/A'}</span>
        <span className="literature-venue" title={paper.venue}>{paper.venue || 'Unknown'}</span>
        {paper.citation_count > 0 && (
          <span className="literature-citations">cited {paper.citation_count}</span>
        )}
      </div>
      {authorsStr && <div className="literature-authors">{authorsStr}</div>}
      {paper.abstract && (
        <div className="literature-abstract">{truncateAbstract(paper.abstract, 180)}</div>
      )}
      {keywords.length > 0 && (
        <div className="literature-keywords">
          {keywords.map((kw) => (
            <span key={kw} className="keyword-tag">{kw}</span>
          ))}
        </div>
      )}
      {paper.doi && (
        <div className="literature-links">
          <a href={paper.url || `https://doi.org/${paper.doi}`} target="_blank" rel="noopener noreferrer">
            DOI
          </a>
        </div>
      )}
    </div>
  )
}

export default memo(function LiteraturePanel({ papers, loading }: Props) {
  if (loading) {
    return (
      <div className="artifact-section literature-section">
        <div className="panel-heading">
          <h3>文献</h3>
          <span>加载中...</span>
        </div>
      </div>
    )
  }

  if (papers.length === 0) {
    return (
      <div className="artifact-section literature-section">
        <div className="panel-heading">
          <h3>文献</h3>
          <span>暂无</span>
        </div>
        <div className="literature-empty">暂无相关文献。</div>
      </div>
    )
  }

  return (
    <div className="artifact-section literature-section">
      <div className="panel-heading">
        <h3>相关文献</h3>
        <span>{papers.length}</span>
      </div>
      <div className="literature-list">
        {papers.map((paper) => (
          <PaperCard key={paper.paper_id || paper.doi || paper.title} paper={paper} />
        ))}
      </div>
    </div>
  )
})
