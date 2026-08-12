"""Auto Review HTTP service — multi-dimensional paper review using LLM.

Endpoints:
  POST /api/review         — Submit a paper for review (JSON; streams NDJSON if Accept: application/x-ndjson)
  GET  /api/models         — List available review models
  GET  /api/review/history — List past reviews
  GET  /api/review/history/search — Search review history (?q=&model=&min_score=&max_score=)
  GET  /api/review/history/<id> — Get a specific past review
  GET  /api/review/history/compare/<id1>/<id2> — Compare two reviews
  GET  /api/review/leaderboard — Rank reviews by score
  DELETE /api/review/history/<id> — Delete a past review
  GET  /api/health         — Health check

Usage:
    python review_service.py [--port 8907]
"""

from __future__ import annotations

import argparse
import base64
import cgi
import json
import os
import socket
import sys
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import BytesIO
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any

# Ensure packages are importable
ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT_DIR / "backend"
AGENT_DIR = BACKEND_DIR / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from review_engine.llm_client import get_model_options, _build_model_configs, register_config_section
from review_engine.reviewer import (
    run_review,
    run_review_streaming,
    save_review,
    load_review_history,
    search_review_history,
    load_review_by_id,
    delete_review,
    load_leaderboard,
)
from review_engine.dimensions import (
    MAX_FILE_SIZE_BYTES,
    REVIEW_DIMENSIONS,
)
from review_engine.export_utils import export_latex, export_pdf

HISTORY_DIR = ROOT_DIR / "backend" / "review_history"

# =============================================================================
# HTTP Server
# =============================================================================


def _write_ndjson(wfile, data: Any) -> None:
    line = json.dumps(data, ensure_ascii=False) + "\n"
    wfile.write(line.encode("utf-8"))
    wfile.flush()


def _configured_qwen_endpoints() -> list[str]:
    """Return configured model endpoints without exposing credentials."""
    try:
        import yaml
        config = yaml.safe_load((ROOT_DIR / "config.arc.yaml").read_text()) or {}
    except Exception:
        config = {}
    sections: list[Any] = [config.get("web_chat_llm"), config.get("llm")]
    sections.extend(config.get("web_chat_llm_fallbacks", []) or [])
    endpoints: list[str] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        model = str(section.get("primary_model", ""))
        base_url = str(section.get("base_url", "")).rstrip("/")
        if "qwen" in model.lower() and base_url and base_url not in endpoints:
            endpoints.append(base_url)
    return endpoints


def _qwen_health(timeout_sec: float = 0.5) -> dict[str, Any]:
    endpoints = _configured_qwen_endpoints()
    states: list[dict[str, Any]] = []
    for endpoint in endpoints:
        parsed = urllib.parse.urlparse(endpoint)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        available = False
        error = ""
        try:
            with socket.create_connection((parsed.hostname or "", port), timeout=timeout_sec):
                available = True
        except OSError as exc:
            error = str(exc)
        states.append({
            "endpoint": endpoint,
            "available": available,
            **({"error": error[:160]} if error else {}),
        })
    return {
        "available": any(item["available"] for item in states),
        "endpoints": states,
    }


def _qwen_unavailable_message(health: dict[str, Any]) -> str:
    endpoint_states = health.get("endpoints", [])
    details = "; ".join(
        f"{item.get('endpoint', 'unknown')}: {item.get('error', 'unavailable')}"
        for item in endpoint_states
    )
    return (
        "Qwen 评审模型当前不可用，本次未生成、未保存任何评审结果。"
        + (f" 模型端点状态：{details}" if details else "")
    )


def _validate_uploaded_base64(file_base64: str) -> str | None:
    try:
        raw = base64.b64decode(file_base64.strip(), validate=True)
    except Exception:
        return "Invalid base64 file payload"
    if not raw:
        return "Uploaded file is empty"
    if len(raw) > MAX_FILE_SIZE_BYTES:
        return (
            f"File too large ({len(raw) / 1024 / 1024:.1f} MB). "
            f"Maximum is {MAX_FILE_SIZE_BYTES / 1024 / 1024:.0f} MB."
        )
    return None


class ReviewHandler(BaseHTTPRequestHandler):
    """HTTP request handler for review endpoints."""

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[review] %s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, status: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/health":
            qwen = _qwen_health()
            self._send_json(200, {
                "status": "ok" if qwen["available"] else "degraded",
                "service": "auto-review",
                "businessService": "ok",
                "qwen": qwen,
            })
        elif path == "/api/models":
            _build_model_configs()
            qwen_models = [
                option for option in get_model_options()
                if "qwen" in option.get("value", "").lower()
            ]
            self._send_json(200, {"models": qwen_models})
        elif path == "/api/review/history":
            records = load_review_history()
            self._send_json(200, {"history": records})
        elif path == "/api/review/history/search":
            qs = urllib.parse.parse_qs(parsed.query)
            self._send_json(200, {
                "results": search_review_history(
                    query=qs.get("q", [""])[0],
                    model_filter=qs.get("model", [""])[0],
                    min_score=int(qs.get("min_score", [0])[0]),
                    max_score=int(qs.get("max_score", [100])[0]),
                    limit=int(qs.get("limit", [50])[0]),
                )
            })
        elif path == "/api/review/leaderboard":
            qs = urllib.parse.parse_qs(parsed.query)
            self._send_json(200, {
                "leaderboard": load_leaderboard(
                    sort_by=qs.get("sort_by", ["score"])[0],
                    limit=int(qs.get("limit", [20])[0]),
                )
            })
        elif path.startswith("/api/review/history/compare/"):
            parts = path[len("/api/review/history/compare/"):].split("/")
            if len(parts) >= 2:
                r1 = load_review_by_id(parts[0])
                r2 = load_review_by_id(parts[1])
                if r1 and r2:
                    self._send_json(200, {
                        "review1": r1,
                        "review2": r2,
                        "diff_score": r1.get("overallScore", 0) - r2.get("overallScore", 0),
                    })
                else:
                    self._send_error_json(404, "One or both reviews not found")
            else:
                self._send_error_json(400, "Need two review IDs: compare/<id1>/<id2>")
        elif path.startswith("/api/review/history/") and path.endswith("/export/latex"):
            review_id = path[len("/api/review/history/"):-len("/export/latex")]
            record = load_review_by_id(review_id)
            if not record:
                self._send_error_json(404, "Review not found")
            else:
                tex = export_latex(record)
                body = tex.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/x-latex; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="review_{review_id}.tex"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
        elif path.startswith("/api/review/history/") and path.endswith("/export/pdf"):
            review_id = path[len("/api/review/history/"):-len("/export/pdf")]
            record = load_review_by_id(review_id)
            if not record:
                self._send_error_json(404, "Review not found")
            else:
                try:
                    pdf_bytes = export_pdf(record)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Disposition", f'attachment; filename="review_{review_id}.pdf"')
                    self.send_header("Content-Length", str(len(pdf_bytes)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(pdf_bytes)
                except RuntimeError as e:
                    self._send_error_json(500, str(e))
                except Exception as e:
                    self._send_error_json(500, f"PDF generation failed: {e}")
        elif path.startswith("/api/review/history/"):
            review_id = path[len("/api/review/history/"):]
            record = load_review_by_id(review_id)
            if record:
                self._send_json(200, record)
            else:
                self._send_error_json(404, "Review not found")
        else:
            self._send_error_json(404, "Not found")

    def do_DELETE(self) -> None:
        path = self.path
        if path.startswith("/api/review/history/"):
            review_id = path[len("/api/review/history/"):]
            if delete_review(review_id):
                self._send_json(200, {"status": "deleted"})
            else:
                self._send_error_json(404, "Review not found")
        else:
            self._send_error_json(404, "Not found")

    def do_POST(self) -> None:
        if self.path != "/api/review":
            self._send_error_json(404, "Not found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_error_json(400, "Empty request body")
            return

        accept = self.headers.get("Accept", "")
        wants_stream = "application/x-ndjson" in accept

        content_type = self.headers.get("Content-Type", "")
        try:
            if "multipart/form-data" in content_type:
                self._handle_multipart_review(content_length, wants_stream)
            else:
                self._handle_json_review(content_length, wants_stream)
        except Exception as exc:
            self._send_error_json(500, f"Review failed: {exc}")

    # -- body reading ---------------------------------------------------------

    def _read_body(self, content_length: int) -> bytes:
        return self.rfile.read(content_length)

    # -- file validation ------------------------------------------------------

    def _validate_file(self, file_base64: str) -> str | None:
        return _validate_uploaded_base64(file_base64)

    # -- JSON review ----------------------------------------------------------

    def _handle_json_review(self, content_length: int, wants_stream: bool) -> None:
        body = self._read_json(content_length)
        file_base64 = body.get("fileBase64", "")
        file_name = body.get("fileName", "paper.pdf")
        dimensions = body.get("dimensions", [])
        model = body.get("model", None)
        vision_reader = body.get("vision_reader", True)
        batch = body.get("batch", False)
        hybrid = body.get("hybrid", True)
        models = body.get("models", None)
        enable_debate = body.get("debate", True)
        max_debates = max(0, min(4, int(body.get("max_debates", 2))))
        min_finding_confidence = max(0.0, min(1.0, float(body.get("min_confidence", 0.65))))
        venue = str(body.get("venue", ""))

        if not file_base64:
            self._send_error_json(400, "Missing fileBase64")
            return

        err = self._validate_file(file_base64)
        if err:
            self._send_error_json(400, err)
            return

        qwen = _qwen_health()
        if not qwen["available"]:
            self._send_error_json(503, _qwen_unavailable_message(qwen))
            return

        if wants_stream:
            self._handle_streaming_review(
                file_base64, file_name, dimensions, model,
                vision_reader, batch, models, hybrid,
                enable_debate, max_debates, venue, min_finding_confidence,
            )
        else:
            results, meta, overall_summary = run_review(
                file_base64, file_name, dimensions, model,
                vision_reader=vision_reader, batch=batch, hybrid=hybrid, models=models,
                enable_debate=enable_debate, max_debates=max_debates, venue=venue,
                min_finding_confidence=min_finding_confidence,
            )
            overall = sum(r["score"] for r in results) // len(results) if results else 0
            review_id = save_review(file_name, meta.get("review_model", model), dimensions, results, meta, overall_summary)
            resp: dict[str, Any] = {
                "results": results,
                "overallScore": overall,
                "dimensionCount": len(results),
                "reviewId": review_id,
                "meta": meta,
                "verifiedFindings": meta.get("verifiedFindings", []),
                "filteredFindings": meta.get("filteredFindings", []),
                "consensusMetrics": meta.get("consensusMetrics", {}),
                "categorizedFindings": meta.get("categorizedFindings", []),
                "confidenceSummary": meta.get("confidenceSummary", {}),
                "claimEvidenceMatrix": meta.get("claimEvidenceMatrix", {}),
                "reportSummary": meta.get("reportSummary", {}),
            }
            if overall_summary:
                resp["overallSummary"] = overall_summary
            self._send_json(200, resp)

    # -- streaming review (NDJSON) --------------------------------------------

    def _handle_streaming_review(
        self, file_base64: str, file_name: str,
        dimensions: list[str], model: str | None,
        vision_reader: bool = False,
        batch: bool = False,
        models: list[str] | None = None,
        hybrid: bool = True,
        enable_debate: bool = True,
        max_debates: int = 2,
        venue: str = "THESIS",
        min_finding_confidence: float = 0.65,
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            q = run_review_streaming(
                file_base64, file_name, dimensions, model,
                vision_reader=vision_reader, batch=batch, hybrid=hybrid, models=models,
                enable_debate=enable_debate, max_debates=max_debates, venue=venue,
                min_finding_confidence=min_finding_confidence,
            )
            while True:
                event = q.get()
                _write_ndjson(self.wfile, event)
                if event["type"] == "progress":
                    pass
                elif event["type"] == "complete":
                    try:
                        save_review(
                            file_name,
                            (event.get("meta") or {}).get("review_model", model),
                            dimensions,
                            event["results"],
                            event.get("meta"),
                            event.get("overallSummary"),
                        )
                    except Exception:
                        pass
                    break
                elif event["type"] == "error":
                    break
        except Exception as exc:
            _write_ndjson(self.wfile, {"type": "error", "error": str(exc)})

    # -- multipart review -----------------------------------------------------

    def _handle_multipart_review(self, content_length: int, wants_stream: bool) -> None:
        boundary = self._extract_boundary()
        if not boundary:
            self._send_error_json(400, "Missing boundary in multipart/form-data")
            return

        raw = self._read_body(content_length)
        parts = self._parse_multipart(raw, boundary)

        file_base64 = ""
        file_name = "paper.pdf"
        dimensions: list[str] = []
        model = None
        vision_reader = True
        batch = False
        hybrid = True
        models: list[str] | None = None
        enable_debate = True
        max_debates = 2
        min_finding_confidence = 0.65
        venue = "THESIS"

        for field_name, field_value, field_filename in parts:
            if field_name == "file" and field_value:
                file_base64 = field_value
                if field_filename:
                    file_name = field_filename
            elif field_name == "dimensions" and field_value:
                try:
                    dimensions = json.loads(field_value)
                except json.JSONDecodeError:
                    dimensions = [d.strip() for d in field_value.split(",") if d.strip()]
            elif field_name == "model" and field_value:
                model = field_value
            elif field_name == "vision_reader" and field_value:
                vision_reader = field_value.lower() in ("true", "1", "yes")
            elif field_name == "batch" and field_value:
                batch = field_value.lower() in ("true", "1", "yes")
            elif field_name == "hybrid" and field_value:
                hybrid = field_value.lower() in ("true", "1", "yes")
            elif field_name == "models" and field_value:
                try:
                    parsed_models = json.loads(field_value)
                    models = parsed_models if isinstance(parsed_models, list) else None
                except json.JSONDecodeError:
                    models = [m.strip() for m in field_value.split(",") if m.strip()]
            elif field_name == "debate" and field_value:
                enable_debate = field_value.lower() in ("true", "1", "yes")
            elif field_name == "max_debates" and field_value:
                max_debates = max(0, min(4, int(field_value)))
            elif field_name == "min_confidence" and field_value:
                min_finding_confidence = max(0.0, min(1.0, float(field_value)))
            elif field_name == "venue" and field_value:
                venue = field_value

        if not file_base64:
            self._send_error_json(400, "Missing file")
            return
        err = self._validate_file(file_base64)
        if err:
            self._send_error_json(400, err)
            return


        qwen = _qwen_health()
        if not qwen["available"]:
            self._send_error_json(503, _qwen_unavailable_message(qwen))
            return

        if wants_stream:
            self._handle_streaming_review(
                file_base64, file_name, dimensions, model,
                vision_reader, batch, models, hybrid,
                enable_debate, max_debates, venue, min_finding_confidence,
            )
        else:
            results, meta, overall_summary = run_review(
                file_base64, file_name, dimensions, model,
                vision_reader=vision_reader, batch=batch, hybrid=hybrid, models=models,
                enable_debate=enable_debate, max_debates=max_debates, venue=venue,
                min_finding_confidence=min_finding_confidence,
            )
            overall = sum(r["score"] for r in results) // len(results) if results else 0
            review_id = save_review(file_name, meta.get("review_model", model), dimensions, results, meta, overall_summary)
            resp: dict[str, Any] = {
                "results": results,
                "overallScore": overall,
                "dimensionCount": len(results),
                "reviewId": review_id,
                "meta": meta,
                "verifiedFindings": meta.get("verifiedFindings", []),
                "filteredFindings": meta.get("filteredFindings", []),
                "consensusMetrics": meta.get("consensusMetrics", {}),
                "categorizedFindings": meta.get("categorizedFindings", []),
                "confidenceSummary": meta.get("confidenceSummary", {}),
                "claimEvidenceMatrix": meta.get("claimEvidenceMatrix", {}),
                "reportSummary": meta.get("reportSummary", {}),
            }
            if overall_summary:
                resp["overallSummary"] = overall_summary
            self._send_json(200, resp)

    def _read_json(self, content_length: int) -> dict[str, Any]:
        raw = self._read_body(content_length)
        return json.loads(raw.decode("utf-8"))

    def _extract_boundary(self) -> str | None:
        ct = self.headers.get("Content-Type", "")
        for part in ct.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                return part[9:].strip().strip('"')
        return None

    def _parse_multipart(
        self, raw: bytes, boundary: str
    ) -> list[tuple[str, str, str]]:
        import io
        headers = {"content-type": f'multipart/form-data; boundary={boundary}'}
        environ = {
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": f'multipart/form-data; boundary={boundary}',
            "CONTENT_LENGTH": str(len(raw)),
        }
        fs = cgi.FieldStorage(
            fp=io.BytesIO(raw),
            environ=environ,
            headers=headers,
            keep_blank_values=True,
        )

        results: list[tuple[str, str, str]] = []
        if fs.list:
            for field in fs.list:
                name = field.name or ""
                filename = field.filename or ""
                if field.file:
                    file_data = field.file.read()
                    if isinstance(file_data, str):
                        results.append((name, file_data, filename))
                        continue
                    is_pdf_file = filename.lower().endswith(".pdf") or file_data[:4] == b"%PDF"
                    if is_pdf_file:
                        b64 = base64.b64encode(file_data).decode("ascii")
                        results.append((name, b64, filename))
                    else:
                        try:
                            text = file_data.decode("utf-8")
                            results.append((name, text, filename))
                        except Exception:
                            b64 = base64.b64encode(file_data).decode("ascii")
                            results.append((name, b64, filename))
                elif field.value:
                    results.append((name, str(field.value), filename))
        return results


class ThreadedReviewServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server for review service."""
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto Review HTTP Service")
    parser.add_argument("--port", type=int, default=8907, help="Port to listen on")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to")
    args = parser.parse_args()

    server = ThreadedReviewServer((args.host, args.port), ReviewHandler)
    print(f"[review] Auto Review service starting on {args.host}:{args.port}")
    print(f"[review] API endpoints:")
    print(f"  POST http://localhost:{args.port}/api/review")
    print(f"  GET  http://localhost:{args.port}/api/models")
    print(f"  GET  http://localhost:{args.port}/api/review/history")
    print(f"  GET  http://localhost:{args.port}/api/review/history/search?q=&model=&min_score=&max_score=")
    print(f"  GET  http://localhost:{args.port}/api/review/leaderboard")
    print(f"  GET  http://localhost:{args.port}/api/health")

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    try:
        _build_model_configs()
        # Register Qwen3 from review_llm section so the review pipeline
        # uses the correct model/endpoint (overrides fallback configs).
        register_config_section("review_llm")
        print(f"[review] Available AutoReview models:")
        for m in get_model_options():
            print(f"  - {m['label']}")
    except Exception as exc:
        print(f"[review] Warning: model init failed (will retry on first request): {exc}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[review] Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
