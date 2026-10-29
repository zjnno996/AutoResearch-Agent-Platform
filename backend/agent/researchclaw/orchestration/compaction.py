"""Compact completed tool rounds while preserving the original task verbatim."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from researchclaw.orchestration.artifacts import write_json


def compact_history(messages: list[dict[str, Any]], archive: Path,
                    max_chars: int = 100000) -> list[dict[str, Any]]:
    if len(json.dumps(messages, ensure_ascii=False)) <= max_chars or len(messages) < 5:
        return messages
    # Each assistant tool-call message and all its results stay in one group.
    starts = [i for i, row in enumerate(messages) if i > 0 and row.get("role") == "assistant"]
    if len(starts) < 4:
        return messages
    cut = starts[-3]
    older, recent = messages[1:cut], messages[cut:]
    archive.mkdir(parents=True, exist_ok=True)
    snapshot = archive / f"history-{len(list(archive.glob('history-*.json'))):04d}.json"
    write_json(snapshot, messages)
    facts = []
    for row in older:
        for call in row.get("tool_calls", []) or []:
            function = call.get("function", {})
            try:
                args = json.loads(function.get("arguments", "{}"))
            except (ValueError, TypeError):
                args = {}
            facts.append({"tool": function.get("name"), "path": args.get("path"),
                          "command": str(args.get("command", ""))[:240]})
        if row.get("role") == "tool":
            content = str(row.get("content", ""))
            facts.append({"tool_call_id": row.get("tool_call_id"), "result_excerpt": content[-400:]})
    summary = {"kind": "compressed_tool_history", "archive": str(snapshot),
               "policy": "Excerpts are navigation aids, not scientific evidence. Read current files and receipts. Original task below is unchanged.",
               "recent_operations": facts[-24:]}
    # Old compressed memory stays available through the chained snapshots.
    return [messages[0], {"role": "user", "content": json.dumps(summary, ensure_ascii=False)}, *recent]
