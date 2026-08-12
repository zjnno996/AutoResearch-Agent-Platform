"""Lightweight tracing utilities for ResearchClaw workflows.

The module is intentionally stdlib-only. It writes JSONL events so local runs,
web backends, and future tracing backends (LangSmith/Phoenix/OpenTelemetry) can
consume the same event stream without changing pipeline code.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(text: str, *, length: int = 16) -> str:
    return hashlib.sha256(text.encode('utf-8', errors='ignore')).hexdigest()[:length]


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(payload)
    record.setdefault('timestamp', utcnow_iso())
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + '\n')


def trace_event(trace_dir: str | Path | None, event: str, payload: dict[str, Any]) -> None:
    if not trace_dir:
        return
    try:
        append_jsonl(Path(trace_dir) / 'workflow_trace.jsonl', {'event': event, **payload})
    except OSError:
        return


class StageTrace:
    """Context helper for stage-level observability."""

    def __init__(self, run_dir: Path, stage_dir: Path, stage_no: int, stage_name: str, run_id: str) -> None:
        self.run_dir = run_dir
        self.stage_dir = stage_dir
        self.trace_dir = run_dir / 'traces'
        self.stage_no = stage_no
        self.stage_name = stage_name
        self.run_id = run_id
        self._start = time.monotonic()
        self._previous_env = os.environ.get('RESEARCHCLAW_TRACE_DIR')

    def __enter__(self) -> 'StageTrace':
        os.environ['RESEARCHCLAW_TRACE_DIR'] = str(self.trace_dir)
        os.environ['RESEARCHCLAW_TRACE_STAGE'] = str(self.stage_no)
        trace_event(self.trace_dir, 'stage_started', {
            'run_id': self.run_id,
            'stage': self.stage_no,
            'stage_name': self.stage_name,
        })
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._previous_env is None:
            os.environ.pop('RESEARCHCLAW_TRACE_DIR', None)
        else:
            os.environ['RESEARCHCLAW_TRACE_DIR'] = self._previous_env
        os.environ.pop('RESEARCHCLAW_TRACE_STAGE', None)

    def finish(self, *, status: str, artifacts_count: int, error: str = '') -> None:
        trace_event(self.trace_dir, 'stage_finished', {
            'run_id': self.run_id,
            'stage': self.stage_no,
            'stage_name': self.stage_name,
            'status': status,
            'duration_sec': round(time.monotonic() - self._start, 3),
            'artifacts_count': artifacts_count,
            'error': error,
        })


def trace_llm_call(
    *,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    json_mode: bool,
    status: str,
    latency_sec: float,
    response_model: str = '',
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    finish_reason: str = '',
    error: str = '',
    request_body: dict[str, Any] | None = None,
    response_content: str = '',
) -> None:
    trace_dir = os.environ.get('RESEARCHCLAW_TRACE_DIR', '')
    if not trace_dir:
        return
    prompt_text = '\n'.join(str(m.get('content', '')) for m in messages)
    response_text = response_content or ''
    stage = os.environ.get('RESEARCHCLAW_TRACE_STAGE', '')
    call_id = f"{int(time.time() * 1000)}-{os.getpid()}-{stage}"
    request_chars = 0
    if request_body is not None:
        try:
            request_chars = len(json.dumps(request_body, ensure_ascii=False))
        except (TypeError, ValueError):
            request_chars = 0
    if not request_chars:
        request_chars = len(prompt_text)

    payload = {
        'event': 'llm_call',
        'call_id': call_id,
        'stage': stage,
        'model': model,
        'response_model': response_model or model,
        'status': status,
        'latency_sec': round(latency_sec, 3),
        'json_mode': json_mode,
        'temperature': temperature,
        'max_tokens': max_tokens,
        'messages_count': len(messages),
        'request_chars': request_chars,
        'prompt_chars': len(prompt_text),
        'response_chars': len(response_text),
        'prompt_tokens': prompt_tokens,
        'completion_tokens': completion_tokens,
        'total_tokens': total_tokens,
        'finish_reason': finish_reason,
        'error': error[:500],
    }
    try:
        append_jsonl(Path(trace_dir) / 'llm_calls.jsonl', payload)
    except OSError:
        return


def write_llm_trace_summary(run_dir: str | Path) -> dict[str, Any]:
    """Aggregate per-call LLM traces for a completed project run."""
    trace_dir = Path(run_dir) / 'traces'
    calls_path = trace_dir / 'llm_calls.jsonl'
    calls: list[dict[str, Any]] = []
    if calls_path.exists():
        for line in calls_path.read_text(encoding='utf-8', errors='replace').splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                calls.append(item)

    by_model: dict[str, dict[str, Any]] = {}
    by_stage: dict[str, dict[str, Any]] = {}
    for item in calls:
        model = str(item.get('model') or 'unknown')
        stage = str(item.get('stage') or 'unknown')
        status = str(item.get('status') or 'unknown')
        total_tokens = int(item.get('total_tokens') or 0)
        latency = float(item.get('latency_sec') or 0)
        for bucket, key in ((by_model, model), (by_stage, stage)):
            row = bucket.setdefault(key, {
                'calls': 0,
                'ok': 0,
                'error': 0,
                'total_tokens': 0,
                'latency_sec': 0.0,
            })
            row['calls'] += 1
            if status == 'ok':
                row['ok'] += 1
            elif status == 'error':
                row['error'] += 1
            row['total_tokens'] += total_tokens
            row['latency_sec'] = round(float(row['latency_sec']) + latency, 3)

    summary = {
        'generated': utcnow_iso(),
        'trace_dir': str(trace_dir),
        'call_count': len(calls),
        'ok_count': sum(1 for c in calls if c.get('status') == 'ok'),
        'error_count': sum(1 for c in calls if c.get('status') == 'error'),
        'total_prompt_tokens': sum(int(c.get('prompt_tokens') or 0) for c in calls),
        'total_completion_tokens': sum(int(c.get('completion_tokens') or 0) for c in calls),
        'total_tokens': sum(int(c.get('total_tokens') or 0) for c in calls),
        'total_latency_sec': round(sum(float(c.get('latency_sec') or 0) for c in calls), 3),
        'by_model': by_model,
        'by_stage': by_stage,
        'recent_errors': [
            {
                'call_id': c.get('call_id', ''),
                'stage': c.get('stage', ''),
                'model': c.get('model', ''),
                'error': c.get('error', ''),
                }
            for c in calls if c.get('status') == 'error'
        ][-20:],
    }
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / 'llm_trace_summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8'
    )

    lines = [
        '# LLM Trace Summary',
        '',
        f"Generated: {summary['generated']}",
        f"Calls: {summary['call_count']} total, {summary['ok_count']} ok, {summary['error_count']} error",
        f"Tokens: {summary['total_tokens']} total ({summary['total_prompt_tokens']} prompt / {summary['total_completion_tokens']} completion)",
        f"Latency: {summary['total_latency_sec']} sec cumulative",
        '',
        '## By Model',
        '',
        '| Model | Calls | OK | Error | Tokens | Latency sec |',
        '|---|---:|---:|---:|---:|---:|',
    ]
    for model, row in sorted(by_model.items()):
        lines.append(f"| {model} | {row['calls']} | {row['ok']} | {row['error']} | {row['total_tokens']} | {row['latency_sec']} |")
    lines.extend(['', '## By Stage', '', '| Stage | Calls | OK | Error | Tokens | Latency sec |', '|---|---:|---:|---:|---:|---:|'])
    for stage, row in sorted(by_stage.items(), key=lambda kv: kv[0]):
        lines.append(f"| {stage} | {row['calls']} | {row['ok']} | {row['error']} | {row['total_tokens']} | {row['latency_sec']} |")
    if summary['recent_errors']:
        lines.extend(['', '## Recent Errors', ''])
        for err in summary['recent_errors']:
            lines.append(f"- stage={err['stage']} model={err['model']} call={err['call_id']} error={err['error']}")
    (trace_dir / 'llm_trace_summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return summary

