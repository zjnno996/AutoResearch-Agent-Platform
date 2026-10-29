"""Public grounding is bounded, primary-source-only and separate from model calls."""
import json
from pathlib import Path
from types import SimpleNamespace
from researchclaw.orchestration.artifacts import read_json
from researchclaw.orchestration.grounding import discover_sources, primary_url


def config():
    return SimpleNamespace(research_engine=SimpleNamespace(public_source_search=True,max_source_queries=2,max_public_sources=2),
                           research=SimpleNamespace(topic="convex optimization convergence"))


def test_only_primary_sources_are_eligible():
    assert primary_url("https://export.arxiv.org/pdf/2108.00132#12") == "https://arxiv.org/abs/2108.00132"
    assert not primary_url("https://huggingface.co/datasets/benchmark/target_study/checklist.json")
    assert not primary_url("https://arxiv.org.evil.example/abs/1234")
    assert not primary_url("http://127.0.0.1/private")


def test_no_credentials_are_sent_to_a_different_provider(tmp_path,monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError("Unexpected request")
    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    llm=SimpleNamespace(base_url="https://other-provider.example/v1",api_key="private-fixture")
    assert discover_sources(tmp_path,config(),llm,{"source_queries":["convergence"]}) == []
    assert read_json(tmp_path/"public_source_retrieval.json")["status"] == "configured_provider_has_no_supported_raw_search"


def test_source_acquisition_is_observed_without_additional_model_calls(tmp_path,monkeypatch):
    sent=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,*args):
            return json.dumps({"results":[{"url":"https://arxiv.org/abs/1234.56789","title":"A public method",
                "content":"Convex convergence theorem and assumptions. "*100,"rerank_score":2}]}).encode()
    def request(req,**kwargs):
        sent.append(req.full_url);return Response()
    monkeypatch.setattr("urllib.request.urlopen",request)
    llm=SimpleNamespace(base_url="https://api.deepseek.com/v1",api_key="private-fixture")
    refs=discover_sources(tmp_path,config(),llm,{"source_queries":["convex convergence"]})
    assert len(refs)==1 and refs[0]["origin"]=="public_primary_source"
    audit=read_json(tmp_path/"public_source_retrieval.json")
    assert audit["llm_calls"]==0 and len(sent)==1
    assert "private-fixture" not in (tmp_path/"public_source_retrieval.json").read_text()


def test_empty_fetches_cannot_create_unbounded_crawling(tmp_path,monkeypatch):
    sent=[]
    class Response:
        def __init__(self,data):self.data=data
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,*args):return json.dumps(self.data).encode()
    def request(req,**kwargs):
        sent.append(req.full_url)
        if req.full_url.endswith('/fetch'):return Response({})
        return Response({"results":[{"url":f"https://arxiv.org/abs/1234.{i:05d}","content":""} for i in range(10)]})
    monkeypatch.setattr("urllib.request.urlopen",request)
    llm=SimpleNamespace(base_url="https://api.deepseek.com/v1",api_key="private-fixture")
    assert discover_sources(tmp_path,config(),llm,{"source_queries":["method one","method two"]})==[]
    assert len(sent)<=4


def test_evaluation_queries_are_rejected(tmp_path,monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",lambda *args,**kwargs: (_ for _ in ()).throw(AssertionError("Unexpected network")))
    llm=SimpleNamespace(base_url="https://api.deepseek.com/v1",api_key="private-fixture")
    assert discover_sources(tmp_path,config(),llm,{"source_queries":["ResearchClawBench checklist"]})==[]


def test_mask_rcnn_is_not_mistaken_for_an_api_key(tmp_path,monkeypatch):
    sent=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,*args):return b'{"results":[]}'
    def request(req,**kwargs):sent.append(req.full_url);return Response()
    monkeypatch.setattr("urllib.request.urlopen",request)
    llm=SimpleNamespace(base_url="https://api.deepseek.com/v1",api_key="private-fixture")
    discover_sources(tmp_path,config(),llm,{"source_queries":["Mask-RCNN segmentation method"]})
    assert len(sent)==1
