"""Test LLM-as-Judge with Yunwu API using gpt-5.5-pro.

NOTE: gpt-5.5-pro has been replaced by deepseek-v4-pro as the default judge.
Set env vars to override:
  IDEA_JUDGE_API_URL=https://yunwu.ai/v1
  IDEA_JUDGE_API_KEY=sk-...
  IDEA_JUDGE_MODEL=gpt-5.5-pro
"""

import os
import sys
from pathlib import Path

# Ensure researchclaw is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from researchclaw.llm.client import LLMClient, LLMConfig


def main():
    # Config from environment (with yunwu.ai defaults for backward compat)
    base_url = os.environ.get("IDEA_JUDGE_API_URL", "https://yunwu.ai/v1")
    api_key = os.environ.get("IDEA_JUDGE_API_KEY", "") or os.environ.get(
        "RESEARCHCLAW_IDEA_JUDGE_API_KEY", ""
    )
    model = os.environ.get("IDEA_JUDGE_MODEL", "gpt-5.5-pro")

    if not api_key:
        print("ERROR: Set IDEA_JUDGE_API_KEY or RESEARCHCLAW_IDEA_JUDGE_API_KEY")
        sys.exit(1)

    config = LLMConfig(
        base_url=base_url,
        api_key=api_key,
        primary_model=model,
        fallback_models=[],
        timeout_sec=90,
        max_retries=1,
        temperature=0,
        strip_thinking=True,
    )
    client = LLMClient(config)

    # Step 1: Quick preflight to check connectivity
    print("=== Step 1: Preflight check ===")
    ok, msg = client.preflight()
    print(f"  Result: {'PASS' if ok else 'FAIL'} — {msg}")
    if not ok:
        print("  Preflight failed, skipping full test.")
        return 1

    # Step 2: Test the chat method directly
    print("\n=== Step 2: Direct chat test ===")
    resp = client.chat(
        [{"role": "user", "content": "Say 'hello, LLM judge ready' in exactly 5 words."}],
        max_tokens=64,
        temperature=0,
    )
    print(f"  Model: {resp.model}")
    print(f"  Content: {resp.content[:200]}")
    print(f"  Tokens: prompt={resp.prompt_tokens}, completion={resp.completion_tokens}")

    # Step 3: Full LLM-as-Judge test
    print("\n=== Step 3: LLM-as-Judge evaluation ===")
    from researchclaw.evaluation.idea_quality import evaluate_ideas_with_llm_judge

    sample_ideas = """## Idea 1: Adaptive Prompt Compression for Long-Context LLMs

核心假设：LLMs process long contexts inefficiently; we hypothesize that adaptive token pruning based on attention entropy can reduce context length by 40-60% with less than 2% accuracy degradation.

文献依据：Related to LongFormer (Beltagy et al., 2020), LLMLingua (Jiang et al., 2023 ICML), and Retrieval-Augmented Generation (Lewis et al., 2020 NeurIPS). However, existing methods use fixed compression ratios rather than attention-based adaptive pruning.

技术路线：1) Collect attention maps across diverse tasks (MMLU, GSM8K, HumanEval); 2) Train a lightweight entropy predictor to identify prunable tokens; 3) Implement adaptive compression with a threshold controller; 4) Evaluate on 5 benchmarks.

可验证实验：Baseline against LLMLingua and vanilla RAG on MMLU, GSM8K, HumanEval, and a custom 32K context benchmark. Metrics: accuracy, latency (ms), compression ratio. Failure threshold: accuracy drop > 3% vs uncompressed baseline.

两周 MVP：Week 1 — implement attention entropy calculator, test on GPT-4 and Claude, measure pruning accuracy. Week 2 — build adaptive threshold controller, run benchmarks. Go/No-Go: if accuracy drop > 5% at 50% compression ratio, pivot to fixed-ratio approach.

风险失败条件：Main risk is model-specific attention patterns may not generalize. Fallback: fixed-ratio compression with manual tuning. Early stop signal: >5% accuracy drop on MMLU at any compression level.

评分：Novelty 4, Feasibility 4, Impact 4, Testability 4, Literature Grounding 4, Risk 4
"""

    print("  Sending to LLM judge (gpt-5.5-pro via yunwu.ai)...")
    report = evaluate_ideas_with_llm_judge(sample_ideas, client, model_name="gpt-5.5-pro")

    import json
    print(f"\n  Status: {report.get('status')}")
    print(f"  Judge Model: {report.get('judge_model')}")

    if report.get('status') == 'ok':
        summary = report.get('summary', {})
        print(f"  Verdict: {summary.get('verdict', 'N/A')}")
        print(f"  Overall Avg: {summary.get('overall_avg', 'N/A')}")
        print(f"  Best Idea: {summary.get('best_idea', 'N/A')}")
        print(f"  Main Reason: {summary.get('main_reason', 'N/A')}")
        ideas = report.get('ideas', [])
        print(f"\n  Detailed scores for {len(ideas)} idea(s):")
        for idea in ideas:
            print(f"    {idea.get('idea_id')}: overall={idea.get('overall')}, "
                  f"novelty={idea.get('novelty')}, feasibility={idea.get('feasibility')}, "
                  f"impact={idea.get('impact')}")
            if idea.get('strengths'):
                for s in idea['strengths'][:2]:
                    print(f"      + {s}")
            if idea.get('weaknesses'):
                for w in idea['weaknesses'][:2]:
                    print(f"      - {w}")
        print("\n  TEST PASSED: LLM-as-Judge works correctly with yunwu.ai + gpt-5.5-pro")
        return 0
    else:
        print(f"  Error: {report.get('error', 'unknown')}")
        print(f"  Raw preview: {report.get('raw_preview', 'N/A')}")
        print("\n  TEST FAILED: Judge returned non-JSON or invalid response")
        return 1


if __name__ == "__main__":
    sys.exit(main())
