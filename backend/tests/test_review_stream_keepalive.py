"""Long model calls must keep a public NDJSON connection active."""
from io import BytesIO
import json
from queue import Empty

from services import review_service


def test_idle_review_sends_flushed_heartbeats_and_preserves_final_result(monkeypatch):
    final = {'type': 'complete', 'results': [{'score': 84}], 'meta': {'review_model': 'Qwen3.8-27B-FP8'}}
    class SlowQueue:
        calls = 0
        def get(self, timeout):
            assert 0 < timeout <= 30
            self.calls += 1
            if self.calls == 1:
                raise Empty
            return final
    class Output(BytesIO):
        flushes = 0
        def flush(self):
            self.flushes += 1
            super().flush()
    saved = []
    monkeypatch.setattr(review_service, 'run_review_streaming', lambda *args, **kwargs: SlowQueue())
    monkeypatch.setattr(review_service, 'save_review', lambda *args: saved.append(args))
    handler = object.__new__(review_service.ReviewHandler)
    handler.wfile = Output()
    handler.send_response = lambda status: None
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler._handle_streaming_review('fixture', 'paper.pdf', ['methodology'], 'Qwen3.8-27B-FP8')
    events = [json.loads(line) for line in handler.wfile.getvalue().splitlines()]
    assert events == [{'type': 'heartbeat'}, final]
    assert handler.wfile.flushes == 2
    assert saved[0][3] == final['results'] and saved[0][4] == final['meta']
