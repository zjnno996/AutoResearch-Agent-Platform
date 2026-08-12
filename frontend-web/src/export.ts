export function downloadMarkdown(content: string, filename: string) {
  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
}

// ---------------------------------------------------------------------------
// Markdown → HTML 渲染器
// 支持：标题 / 粗斜体 / 链接 / 代码块 / 表格 / 列表 / 引用 / 分割线
// ---------------------------------------------------------------------------

function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function renderInline(text: string): string {
  // 图片 ![alt](url)
  let html = text.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, '<img src="$2" alt="$1" style="max-width:100%">')
  // 链接 [text](url)
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>')
  // 粗体 **text**
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  // 斜体 *text* （避免与粗体冲突）
  html = html.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>')
  // 行内代码
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>')
  // 删除线 ~~text~~
  html = html.replace(/~~(.+?)~~/g, '<del>$1</del>')
  return html
}

function renderTable(lines: string[], i: number): { html: string; endIdx: number } {
  const rows: string[][] = []
  let idx = i
  while (idx < lines.length && lines[idx].trim() !== '' && !lines[idx].startsWith('#')) {
    const line = lines[idx].trim()
    if (/^\|.*\|$/.test(line)) {
      rows.push(line.split('|').slice(1, -1).map(c => c.trim()))
    } else if (/^[\s|:-\]]+$/.test(line.replace(/\|/g, '').trim())) {
      // separator row, skip
      rows.push([]) // placeholder for separator
    } else {
      break
    }
    idx++
  }

  if (rows.length < 2) return { html: '', endIdx: i }

  const sepRow = rows[1]
  const alignments = sepRow.map(cell => {
    if (/^:?-+:?$/.test(cell)) {
      if (cell.startsWith(':') && cell.endsWith(':')) return ' style="text-align:center"'
      if (cell.endsWith(':')) return ' style="text-align:right"'
      return ''
    }
    return ''
  })

  let html = '<table>\n<thead>\n<tr>'
  rows[0].forEach((cell, ci) => {
    html += `<th${alignments[ci] || ''}>${renderInline(escapeHtml(cell))}</th>`
  })
  html += '</tr>\n</thead>\n<tbody>\n'

  for (let r = 2; r < rows.length; r++) {
    html += '<tr>'
    rows[r].forEach((cell, ci) => {
      html += `<td${alignments[ci] || ''}>${renderInline(escapeHtml(cell))}</td>`
    })
    html += '</tr>\n'
  }
  html += '</tbody>\n</table>\n'

  return { html, endIdx: idx }
}

function renderList(lines: string[], i: number): { html: string; endIdx: number } {
  let html = ''
  let idx = i
  const firstLine = lines[idx]
  const isOrdered = /^\d+[.)]\s/.test(firstLine)

  html += isOrdered ? '<ol>\n' : '<ul>\n'

  while (idx < lines.length) {
    const line = lines[idx]
    const trimmed = line.trim()

    // Check if this is a list item
    const ulMatch = trimmed.match(/^[-*+]\s+(.*)/)
    const olMatch = trimmed.match(/^\d+[.)]\s+(.*)/)

    if (!ulMatch && !olMatch && trimmed !== '') break
    if (trimmed === '') { idx++; break }

    const content = ulMatch ? ulMatch[1] : (olMatch ? olMatch[1] : '')
    html += `<li>${renderInline(escapeHtml(content))}</li>\n`
    idx++
  }

  html += isOrdered ? '</ol>\n' : '</ul>\n'
  return { html, endIdx: idx }
}

type RenderedMarkdown = { bodyHtml: string; tocHtml: string; title: string }

function mdToHtml(markdown: string): RenderedMarkdown {
  const lines = markdown.split('\n')
  const htmlParts: string[] = []
  let inCodeBlock = false
  let codeLang = ''
  let codeContent = ''
  const documentSections: { level: number; title: string; anchor: string }[] = []
  let sectionCounter: number[] = [0, 0, 0]

  function flushCodeBlock(): void {
    if (codeContent) {
      const langLabel = codeLang ? `<div class="code-label">${escapeHtml(codeLang)}</div>` : ''
      htmlParts.push(
        `${langLabel}<pre><code>${escapeHtml(codeContent.trimEnd())}</code></pre>\n`
      )
      codeContent = ''
      codeLang = ''
    }
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const trimmed = line.trim()

    // Code block fence
    if (/^```/.test(trimmed)) {
      if (inCodeBlock) {
        flushCodeBlock()
        inCodeBlock = false
      } else {
        flushCodeBlock()
        inCodeBlock = true
        codeLang = trimmed.slice(3).trim()
      }
      continue
    }

    if (inCodeBlock) {
      codeContent += line + '\n'
      continue
    }

    // Empty line
    if (trimmed === '') {
      htmlParts.push('<div class="md-gap"></div>\n')
      continue
    }

    // Heading
    const headingMatch = trimmed.match(/^(#{1,4})\s+(.+)$/)
    if (headingMatch) {
      const level = headingMatch[1].length
      const titleText = headingMatch[2].replace(/\*\*/g, '').trim()
      if (level <= 3) {
        if (level === 1) sectionCounter = [sectionCounter[0] + 1, 0, 0]
        else if (level === 2) sectionCounter = [sectionCounter[0], sectionCounter[1] + 1, 0]
        else sectionCounter = [sectionCounter[0], sectionCounter[1], sectionCounter[2] + 1]

        const numberStr = level === 1 ? `${sectionCounter[0]}`
          : level === 2 ? `${sectionCounter[0]}.${sectionCounter[1]}`
          : `${sectionCounter[0]}.${sectionCounter[1]}.${sectionCounter[2]}`

        const anchor = `section-${numberStr.replace(/\./g, '-')}`
        documentSections.push({ level, title: titleText, anchor })
        htmlParts.push(
          `<h${level} id="${anchor}">${numberStr}. ${renderInline(escapeHtml(titleText))}</h${level}>\n`
        )
      } else {
        htmlParts.push(
          `<h${level}>${renderInline(escapeHtml(titleText))}</h${level}>\n`
        )
      }
      continue
    }

    // Table
    if (trimmed.startsWith('|')) {
      const result = renderTable(lines, i)
      if (result.html) {
        htmlParts.push(result.html)
        i = result.endIdx - 1
        continue
      }
    }

    // Horizontal rule
    if (/^[-*_]{3,}$/.test(trimmed)) {
      htmlParts.push('<hr>\n')
      continue
    }

    // Blockquote
    if (trimmed.startsWith('> ')) {
      const quoteText = trimmed.slice(2)
      let fullQuote = renderInline(escapeHtml(quoteText))
      let j = i + 1
      while (j < lines.length && lines[j].trim().startsWith('> ')) {
        fullQuote += '<br>' + renderInline(escapeHtml(lines[j].trim().slice(2)))
        j++
      }
      htmlParts.push(`<blockquote>${fullQuote}</blockquote>\n`)
      i = j - 1
      continue
    }

    // Unordered list
    if (/^[-*+]\s/.test(trimmed)) {
      const result = renderList(lines, i)
      htmlParts.push(result.html)
      i = result.endIdx - 1
      continue
    }

    // Ordered list
    if (/^\d+[.)]\s/.test(trimmed)) {
      const result = renderList(lines, i)
      htmlParts.push(result.html)
      i = result.endIdx - 1
      continue
    }

    // 普通段落
    htmlParts.push(`<p>${renderInline(escapeHtml(trimmed))}</p>\n`)
  }

  flushCodeBlock()

  const bodyHtml = htmlParts.join('')

  // 生成目录（如果文档有多个一级标题）
  const tocItems = documentSections.filter(s => s.level <= 2)
  const tocHtml = tocItems.length > 1 ? `
    <div class="toc">
      <h2 class="toc-title">目录</h2>
      ${tocItems.map(s => {
        const indent = s.level === 2 ? ' style="padding-left:1.5em"' : ''
        return `<div${indent}><a href="#${s.anchor}">${escapeHtml(s.title)}</a></div>`
      }).join('\n')}
    </div>
    <div style="page-break-before:avoid;"></div>
  ` : ''

  return { bodyHtml, tocHtml, title: documentSections[0]?.title || '文档' }
}

export function printContentAsPdf(content: string, title: string) {
  if (!content || content.trim().length === 0) {
    alert('没有内容可以导出。请先等待内容生成完成。')
    return
  }

  const win = window.open('', '_blank')
  if (!win) return

  const { bodyHtml, tocHtml, title: docTitle } = mdToHtml(content)
  const today = new Date().toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' })

  win.document.write(`<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>${title || docTitle}</title>
<style>
  @page {
    size: A4;
    margin: 2cm 2.5cm;
  }

  * { box-sizing: border-box; }

  body {
    font-family: 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'Helvetica Neue', sans-serif;
    font-size: 11pt;
    line-height: 1.8;
    color: #1a1a2e;
    max-width: 800px;
    margin: 0 auto;
    padding: 1em;
  }

  /* 封面 */
  .cover {
    text-align: center;
    padding: 80px 0 60px;
    page-break-after: always;
  }
  .cover h1 {
    font-size: 22pt;
    font-weight: 700;
    color: #1e3a5f;
    line-height: 1.4;
    margin-bottom: 30px;
    border: none;
  }
  .cover .meta {
    color: #64748b;
    font-size: 10pt;
    margin-top: 40px;
  }
  .cover .meta span {
    display: block;
    margin: 4px 0;
  }

  /* 目录 */
  .toc {
    margin: 2em 0;
    padding: 1.5em 2em;
    background: #f8fafc;
    border-radius: 8px;
    page-break-after: always;
  }
  .toc-title {
    font-size: 14pt;
    color: #1e3a5f;
    border: none !important;
    margin-top: 0 !important;
    margin-bottom: 1em !important;
  }
  .toc a {
    display: block;
    color: #2563eb;
    text-decoration: none;
    padding: 4px 0;
    font-size: 10pt;
    border-bottom: 1px solid #f1f5f9;
  }
  .toc a:hover { text-decoration: underline; }
  .toc a::before { content: "§ "; color: #94a3b8; }

  /* 标题 */
  h1 {
    font-size: 16pt;
    font-weight: 700;
    color: #1e3a5f;
    border-bottom: 2px solid #2563eb;
    padding-bottom: 6px;
    margin-top: 1.8em;
    margin-bottom: 0.8em;
    page-break-after: avoid;
  }
  h2 {
    font-size: 13pt;
    font-weight: 700;
    color: #2c3e50;
    border-bottom: 1px solid #e2e8f0;
    padding-bottom: 4px;
    margin-top: 1.5em;
    margin-bottom: 0.6em;
    page-break-after: avoid;
  }
  h3 {
    font-size: 11.5pt;
    font-weight: 600;
    color: #34495e;
    margin-top: 1.2em;
    margin-bottom: 0.4em;
    page-break-after: avoid;
  }
  h4 {
    font-size: 11pt;
    font-weight: 600;
    color: #444;
    margin-top: 1em;
    margin-bottom: 0.3em;
  }

  /* 段落 */
  p {
    margin: 0.6em 0;
    text-align: justify;
    orphans: 2;
    widows: 2;
  }

  /* 代码 */
  pre {
    background: #f1f5f9;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 1em 1.2em;
    font-family: 'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace;
    font-size: 9pt;
    line-height: 1.5;
    overflow-x: auto;
    white-space: pre-wrap;
    word-break: break-word;
    margin: 0.8em 0;
    page-break-inside: avoid;
  }
  code {
    background: #f1f5f9;
    padding: 0.15em 0.4em;
    border-radius: 3px;
    font-family: 'JetBrains Mono', 'Cascadia Code', monospace;
    font-size: 0.9em;
    color: #c7254e;
  }
  pre code {
    background: transparent;
    padding: 0;
    color: inherit;
  }
  .code-label {
    font-size: 8pt;
    color: #94a3b8;
    margin-bottom: -0.5em;
    padding: 0 0.5em;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  /* 表格 */
  table {
    width: 100%;
    border-collapse: collapse;
    margin: 1em 0;
    font-size: 9.5pt;
    page-break-inside: avoid;
  }
  th, td {
    border: 1px solid #d1d5db;
    padding: 6px 10px;
    text-align: left;
    vertical-align: top;
  }
  th {
    background: #f1f5f9;
    font-weight: 600;
    color: #1e3a5f;
  }
  tr:nth-child(even) td {
    background: #fafbfc;
  }

  /* 引用 */
  blockquote {
    border-left: 3px solid #2563eb;
    margin: 1em 0;
    padding: 0.5em 1em;
    background: #f8fafc;
    color: #475569;
    font-style: italic;
  }
  blockquote p { margin: 0.3em 0; }

  /* 列表 */
  ul, ol {
    margin: 0.5em 0;
    padding-left: 1.8em;
  }
  li { margin: 0.2em 0; }

  /* 链接 */
  a { color: #2563eb; text-decoration: none; }
  a:hover { text-decoration: underline; }

  /* 分割线 */
  hr {
    border: none;
    border-top: 1px solid #e2e8f0;
    margin: 1.5em 0;
  }

  /* 图片 */
  img { max-width: 100%; height: auto; border-radius: 4px; margin: 0.5em 0; }

  /* 间距 */
  .md-gap { height: 0.5em; }

  /* 打印样式 */
  @media print {
    body { padding: 0; font-size: 10pt; }
    .cover { padding: 100px 0 60px; }
    .cover h1 { font-size: 20pt; }
    h1 { font-size: 14pt; }
    h2 { font-size: 12pt; }
    pre { font-size: 8pt; }
    table { font-size: 8.5pt; }
    a { color: #1a1a2e; }
    h1, h2, h3, h4 { page-break-after: avoid; }
    pre, table, blockquote { page-break-inside: avoid; }
  }

  @media screen {
    body { background: #f6f7f9; padding: 2em 1em; }
    .print-container {
      background: #fff;
      max-width: 800px;
      margin: 0 auto;
      padding: 2em 3em;
      box-shadow: 0 2px 20px rgba(0,0,0,0.08);
      border-radius: 4px;
    }
  }
</style>
</head>
<body>
<div class="print-container">

  <!-- 封面 -->
  <div class="cover">
    <h1>${escapeHtml(docTitle)}</h1>
    <div class="meta">
      <span>生成日期：${today}</span>
      <span>来源：Claw AI Lab</span>
    </div>
  </div>

  <!-- 目录 -->
  ${tocHtml}

  <!-- 正文 -->
  ${bodyHtml}

</div>
<script>
  (function() {
    // 自动打印
    setTimeout(function() {
      window.print();
    }, 500);
  })();
<\/script>
</body>
</html>`)
  win.document.close()
}

export function printElementAsPdf(elementId: string, title: string) {
  const el = document.getElementById(elementId)
  if (!el) return
  const content = el.textContent || el.innerText || ''
  printContentAsPdf(content, title)
}
