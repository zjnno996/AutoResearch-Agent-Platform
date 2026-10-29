"""Executable research design and reusable codebase discovery."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

from researchclaw.adapters import AdapterBundle
from researchclaw.config import RCConfig
from researchclaw.llm.client import LLMClient
from researchclaw.pipeline.models import StageResult
from researchclaw.pipeline.stages import Stage, StageStatus
from researchclaw.pipeline.support.artifacts import (
    _load_hardware_profile,
    _read_prior_artifact,
    _utcnow_iso,
)
from researchclaw.pipeline.support.context import (
    _build_context_preamble,
    _chat_with_prompt,
    _get_evolution_overlay,
)
from researchclaw.pipeline.support.domain import _detect_domain
from researchclaw.pipeline.support.parsing import _extract_yaml_block, _parse_yaml_dict_from_llm
from researchclaw.prompts import PromptManager

logger = logging.getLogger(__name__)


def _enforce_topic_experiment_constraints(
    plan: dict[str, Any],
    *,
    topic: str,
    metric_key: str,
    metric_direction: str,
) -> dict[str, Any]:
    """Apply explicit experiment-scope constraints stated by the user.

    LLM planning may turn a reproduction request into a new-method comparison or
    silently change the requested number of seeds.  Those are not creative
    choices: phrases such as "only", "仅使用", and "禁止" are hard constraints
    and must win over generated plan content.
    """
    import copy
    import re

    constrained = copy.deepcopy(plan)
    topic_lower = topic.lower()
    applied: list[str] = []

    uci_har_only = (
        "uci-har" in topic_lower
        and any(token in topic_lower for token in ("仅使用", "只使用", "only use", "only official"))
    )
    if uci_har_only:
        constrained["datasets"] = [{
            "name": "UCI Human Activity Recognition Using Smartphones",
            "source": "Official UCI Machine Learning Repository",
            "split_strategy": "Use the official train/test split without subject reshuffling",
            "preprocessing": "Use the official precomputed feature matrices; fit any scaler on training data only",
        }]
        applied.append("official_uci_har_only")

    baseline_only = any(
        token in topic_lower
        for token in (
            "未实现的新方法", "禁止.*新方法", "baseline-only", "baselines only",
            "只复现", "仅复现",
        )
    )
    # The literal token above intentionally handles the common Chinese wording;
    # also recognize the semantic combination when punctuation varies.
    baseline_only = baseline_only or (
        "禁止" in topic_lower and "新方法" in topic_lower
    )
    requested_sgd = "sgd" in topic_lower
    requested_rf = "随机森林" in topic_lower or "random forest" in topic_lower
    if baseline_only and (requested_sgd or requested_rf):
        existing = constrained.get("baselines", [])
        if not isinstance(existing, list):
            existing = []

        def _matches(item: Any, *needles: str) -> bool:
            text = str(item).lower().replace("_", " ")
            return any(needle in text for needle in needles)

        selected: list[Any] = []
        if requested_sgd:
            selected.extend(item for item in existing if _matches(item, "sgd", "linear"))
            if not any(_matches(item, "sgd", "linear") for item in selected):
                selected.append({
                    "name": "Linear SGD",
                    "implementation_spec": {
                        "estimator": "sklearn.linear_model.SGDClassifier",
                        "loss": "log_loss",
                    },
                })
        if requested_rf:
            selected.extend(item for item in existing if _matches(item, "randomforest", "random forest", "随机森林"))
            if not any(_matches(item, "randomforest", "random forest", "随机森林") for item in selected):
                selected.append({
                    "name": "Random Forest",
                    "implementation_spec": {
                        "estimator": "sklearn.ensemble.RandomForestClassifier",
                    },
                })
        # Preserve order while removing duplicate generated entries.
        deduped: list[Any] = []
        seen: set[str] = set()
        for item in selected:
            key = str(item.get("name", item) if isinstance(item, dict) else item).lower()
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        constrained["baselines"] = deduped
        for item in constrained["baselines"]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))
            name_lower = name.lower().replace("_", " ")
            if "sgd" in name_lower or ("linear" in name_lower and "random" not in name_lower):
                item["description"] = "Requested linear SGD classification baseline."
                item["rationale"] = "Reproduce the requested linear baseline on the official split."
            elif "randomforest" in name_lower or "random forest" in name_lower or "随机森林" in name_lower:
                item["description"] = "Requested Random Forest classification baseline."
                item["rationale"] = "Reproduce the requested nonlinear tree-ensemble baseline on the official split."
        constrained["proposed_methods"] = []
        constrained["ablations"] = []
        # Failed benchmark-agent suggestions remain available in the separate
        # benchmark_plan artifact, but must not leak into executable codegen.
        constrained.pop("benchmark_suggestions", None)
        constrained["objectives"] = [
            "Reproduce and compare the requested baselines on the official dataset split; do not presuppose which method wins.",
            "Report only metrics and statistical comparisons computed from retained per-seed results.",
        ]
        constrained["compute_budget"] = {
            "hardware_requirements": "CPU sufficient for the requested scikit-learn baselines",
            "total_conditions": len(deduped),
            "total_seeds": 3,
            "total_runs": len(deduped) * 3,
        }
        constrained["risks"] = [
            {
                "risk": "Data leakage in preprocessing",
                "mitigation": "Use the official split and fit any scaler on training data only.",
            },
            {
                "risk": "Limited statistical power from three requested seeds",
                "mitigation": "Retain all per-seed results and report uncertainty without presupposing significance.",
            },
        ]
        applied.append("requested_baselines_only")

    seed_match = re.search(r"(\d+)\s*个(?:独立)?随机种子", topic)
    if seed_match is None:
        seed_match = re.search(r"(\d+)\s+(?:independent\s+)?(?:random\s+)?seeds?", topic_lower)
    if seed_match:
        seed_count = max(1, min(100, int(seed_match.group(1))))
        protocol = constrained.setdefault("evaluation_protocol", {})
        if not isinstance(protocol, dict):
            protocol = {}
            constrained["evaluation_protocol"] = protocol
        existing_seeds = protocol.get("independent_seeds", [])
        if not isinstance(existing_seeds, list):
            existing_seeds = []
        defaults = [11, 29, 47, 71, 101, 131, 173, 211]
        seeds = [int(x) for x in existing_seeds if isinstance(x, int)]
        for value in defaults:
            if len(seeds) >= seed_count:
                break
            if value not in seeds:
                seeds.append(value)
        protocol["independent_seeds"] = seeds[:seed_count]
        protocol["minimum_seeds_per_condition"] = seed_count
        constrained["seeds"] = seeds[:seed_count]
        budget = constrained.get("compute_budget")
        if isinstance(budget, dict):
            condition_count = len(constrained.get("baselines", [])) + len(constrained.get("proposed_methods", []))
            budget["total_seeds"] = seed_count
            budget["total_runs"] = condition_count * seed_count
        applied.append(f"exact_seed_count_{seed_count}")

    requested_metrics: list[str] = []
    if "accuracy" in topic_lower or "准确率" in topic_lower:
        requested_metrics.append("accuracy")
    if "macro-f1" in topic_lower or "macro f1" in topic_lower:
        requested_metrics.append("f1_macro")
    if requested_metrics:
        constrained["metrics"] = requested_metrics
        applied.append("explicit_metrics")
    constrained["primary_metric"] = metric_key
    constrained["metric_direction"] = metric_direction
    constrained["user_hard_constraints"] = {
        "applied": applied,
        "source": "research.topic",
    }
    return constrained


def _execute_experiment_design(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    llm: LLMClient | None = None,
    prompts: PromptManager | None = None,
) -> StageResult:
    hypotheses = _read_prior_artifact(run_dir, "hypotheses.md") or ""
    preamble = _build_context_preamble(
        config, run_dir, include_goal=True, include_hypotheses=True
    )
    plan: dict[str, Any] | None = None

    # ── Load reference paper full text (produced by S4) ───────────────────
    _reference_paper_text = _read_prior_artifact(run_dir, "reference_paper_text.md") or ""
    if _reference_paper_text:
        _reference_paper_text = _reference_paper_text[:30_000]
        logger.info(
            "Stage 09: Found reference paper text (%d chars) — "
            "will inject into experiment design prompt",
            len(_reference_paper_text),
        )

    # ── Domain detection ──────────────────────────────────────────────────
    # Detect the research domain early so we can adapt experiment design
    # and code generation. For ML domains, existing behavior is unchanged.
    _domain_profile = None
    try:
        from researchclaw.domains.detector import detect_domain as _detect_domain_adv
        _domain_profile = _detect_domain_adv(
            topic=config.research.topic,
            hypotheses=hypotheses,
        )
        logger.info(
            "Domain detected: %s (%s)",
            _domain_profile.display_name,
            _domain_profile.domain_id,
        )
        # Persist domain profile for Stage 10
        import json as _json_dd
        (stage_dir / "domain_profile.json").write_text(
            _json_dd.dumps({
                "domain_id": _domain_profile.domain_id,
                "display_name": _domain_profile.display_name,
                "experiment_paradigm": _domain_profile.experiment_paradigm,
                "core_libraries": _domain_profile.core_libraries,
                "gpu_required": _domain_profile.gpu_required,
            }, indent=2),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        logger.debug("Domain detection unavailable", exc_info=True)

    import os as _os_s9
    _datasets_dir_s9 = getattr(config.experiment, "datasets_dir", "") or ""
    _codebases_dir_s9 = getattr(config.experiment, "codebases_dir", "") or ""
    _checkpoints_dir_s9 = getattr(config.experiment, "checkpoints_dir", "") or ""
    _s9_diagnostics: dict[str, Any] = {
        "schema_version": 1,
        "reference_purpose": config.research.reference_purpose,
        "status": "normal",
        "degraded": False,
        "parse_strategy": "not_attempted",
        "fallback_reason": "",
        "benchmark_agent_validation_passed": None,
        "benchmark_agent_errors": [],
        "benchmark_agent_warnings": [],
        "user_facing_status_zh": "实验计划已生成。",
    }
    if llm is not None:
        _pm = prompts or PromptManager()
        # Pass dataset_guidance block for experiment design
        try:
            _dg_block = _pm.block("dataset_guidance")
        except (KeyError, Exception):  # noqa: BLE001
            _dg_block = ""
        # ── Inject project-specific local data paths into experiment design ──
        _local_data_parts: list[str] = []
        if _datasets_dir_s9 and _os_s9.path.isdir(_datasets_dir_s9):
            _ds_items = [d for d in sorted(_os_s9.listdir(_datasets_dir_s9)) if not d.startswith(".")]
            if _ds_items:
                _local_data_parts.append(
                    f"### LOCAL DATASETS (MUST USE — do NOT use synthetic data)\n"
                    f"Directory: `{_datasets_dir_s9}`\n"
                    f"Available: {', '.join(_ds_items)}\n"
                    f"In code: `DATASETS_DIR = '{_datasets_dir_s9}'`\n"
                    f"Design your experiment to use these REAL datasets. "
                    f"NEVER generate synthetic torch.randn() data when real data is available."
                )
                for _ds_name in _ds_items:
                    _ds_path = _os_s9.path.join(_datasets_dir_s9, _ds_name)
                    if _os_s9.path.isdir(_ds_path):
                        _ds_sub = []
                        for _root, _dirs, _files in _os_s9.walk(_ds_path):
                            _rel = _os_s9.path.relpath(_root, _ds_path)
                            for _f in _files[:5]:
                                _ds_sub.append(_os_s9.path.join(_rel, _f) if _rel != "." else _f)
                            if len(_ds_sub) > 20:
                                break
                        if _ds_sub:
                            _local_data_parts.append(
                                f"  Dataset `{_ds_name}` sample files: {', '.join(_ds_sub[:15])}"
                            )
        if _codebases_dir_s9 and _os_s9.path.isdir(_codebases_dir_s9):
            _cb_items = [d for d in sorted(_os_s9.listdir(_codebases_dir_s9))
                         if _os_s9.path.isdir(_os_s9.path.join(_codebases_dir_s9, d)) and not d.startswith(".")]
            if _cb_items:
                _local_data_parts.append(
                    f"\n### LOCAL CODEBASES (MUST BUILD ON TOP OF)\n"
                    f"Directory: `{_codebases_dir_s9}`\n"
                    f"Available: {', '.join(_cb_items)}\n"
                    f"**CRITICAL**: Your experiment MUST extend/wrap these existing codebases. "
                    f"Do NOT design a from-scratch implementation when a reference codebase exists."
                )
                for _cb_name in _cb_items:
                    _cb_path = _os_s9.path.join(_codebases_dir_s9, _cb_name)
                    try:
                        from researchclaw.utils.codebase_manifest import generate_manifest, manifest_to_prompt
                        _cb_manifest = generate_manifest(_cb_path)
                        _cb_prompt = manifest_to_prompt(_cb_manifest)
                        if len(_cb_prompt) > 6000:
                            _cb_prompt = _cb_prompt[:6000] + "\n  ... (truncated)"
                        _local_data_parts.append(_cb_prompt)
                    except Exception:  # noqa: BLE001
                        _local_data_parts.append(f"  Codebase `{_cb_name}` at `{_cb_path}`")
        if _checkpoints_dir_s9 and _os_s9.path.isdir(_checkpoints_dir_s9):
            _ck_items = [d for d in sorted(_os_s9.listdir(_checkpoints_dir_s9)) if not d.startswith(".")]
            if _ck_items:
                _local_data_parts.append(
                    f"\n### LOCAL CHECKPOINTS (pre-trained weights available)\n"
                    f"Directory: `{_checkpoints_dir_s9}`\n"
                    f"Available: {', '.join(_ck_items)}\n"
                    f"Use these checkpoints directly — do NOT plan to download them."
                )
        if _local_data_parts:
            _dg_block += (
                "\n\n## PROJECT-SPECIFIC LOCAL RESOURCES (HIGHEST PRIORITY)\n"
                + "\n".join(_local_data_parts)
                + "\n\nWhen designing the experiment, your `datasets` section MUST reference "
                "these local resources. The experiment code will have direct filesystem access "
                "to these paths.\n"
                "Your YAML MUST include a top-level `local_resources` mapping with "
                "`datasets_dir` equal to the exact local dataset path and a "
                "`usage_requirement` that says core experiments must use this local data.\n"
            )

        # I-08: Inject RL step guidance for RL topics
        _rl_kws = ("reinforcement learning", "ppo", "sac", "td3", "ddpg",
                    "dqn", "mujoco", "continuous control", "actor-critic",
                    "policy gradient", "exploration bonus")
        if any(kw in config.research.topic.lower() for kw in _rl_kws):
            try:
                _dg_block += _pm.block("rl_step_guidance")
            except Exception:  # noqa: BLE001
                pass
        # F-01: Inject framework docs for experiment design
        try:
            from researchclaw.data import detect_frameworks, load_framework_docs
            _fw_ids = detect_frameworks(config.research.topic, hypotheses)
            if _fw_ids:
                _fw_docs = load_framework_docs(_fw_ids, max_chars=4000)
                if _fw_docs:
                    _dg_block += _fw_docs
        except Exception:  # noqa: BLE001
            pass
        # ── Build reference paper block for reproduce-mode projects ────
        _ref_block = ""
        if _reference_paper_text and config.research.reference_purpose == "background":
            _ref_block = (
                "RELATED WORK (BACKGROUND CONTEXT):\n"
                "These papers provide background, not a target paper to reproduce. "
                "The research task and supplied data define the objective. Do not replace "
                "that objective with reproduction of one of these papers. Specify concrete "
                "algorithms, assumptions, parameters and validation for the requested task.\n\n"
                "--- BEGIN RELATED WORK ---\n" + _reference_paper_text + "\n--- END RELATED WORK ---\n\n"
            )
        elif _reference_paper_text:
            _ref_block = (
                "REFERENCE PAPER REPRODUCTION (HIGHEST PRIORITY):\n"
                "You are designing an experiment to REPRODUCE the following paper.\n"
                "Extract the EXACT algorithm, architecture, loss functions, training\n"
                "procedure, and hyperparameters from the paper text below.\n\n"
                "Your `proposed_methods` MUST include `implementation_spec` with:\n"
                "  - class_name matching the paper's described components\n"
                "  - algorithm_steps that faithfully reproduce the paper's method\n"
                "  - loss_function matching the paper's stated objective\n"
                "  - key_hyperparameters matching the paper's reported values\n"
                "Do NOT invent generic placeholders — extract details FROM THE PAPER.\n\n"
                "--- BEGIN REFERENCE PAPER TEXT ---\n"
                f"{_reference_paper_text}\n"
                "--- END REFERENCE PAPER TEXT ---\n\n"
            )

        _overlay = _get_evolution_overlay(run_dir, "experiment_design")
        sp = _pm.for_stage(
            "experiment_design",
            evolution_overlay=_overlay,
            preamble=preamble,
            hypotheses=hypotheses,
            dataset_guidance=_dg_block,
            time_budget_sec=config.experiment.time_budget_sec,
            metric_key=config.experiment.metric_key,
            metric_direction=config.experiment.metric_direction,
            reference_paper_block=_ref_block,
        )
        resp = _chat_with_prompt(
            llm,
            sp.system,
            sp.user,
            json_mode=sp.json_mode,
            max_tokens=sp.max_tokens,
        )
        raw_yaml = _extract_yaml_block(resp.content)
        parsed = _parse_yaml_dict_from_llm(resp.content)
        # Last fallback: try to find any YAML-like dict in the response
        if not isinstance(parsed, dict):
            import re as _re_yaml

            # Look for lines starting with known keys
            _yaml_lines = []
            _capturing = False
            for line in resp.content.splitlines():
                if _re_yaml.match(
                    r"^(baselines|proposed_methods|ablations|datasets|"
                    r"metrics|objectives|risks|compute_budget)\s*:",
                    line,
                ):
                    _capturing = True
                if _capturing:
                    if line.strip() == "" or line.startswith("```"):
                        continue
                    if line.startswith("#") or line.startswith("**"):
                        continue
                    _yaml_lines.append(line)
            if _yaml_lines:
                try:
                    parsed = yaml.safe_load("\n".join(_yaml_lines))
                except yaml.YAMLError:
                    pass
        if isinstance(parsed, dict):
            plan = parsed
            _s9_diagnostics["parse_strategy"] = "qwen_yaml"
        else:
            logger.warning(
                "Stage 09: LLM response could not be parsed as YAML "
                "(len=%d, first 200 chars: %s). Content extraction method "
                "returned: %s",
                len(resp.content),
                resp.content[:200],
                raw_yaml[:200] if raw_yaml else "<empty>",
            )
            # BUG-12: Retry with a stricter, shorter prompt
            if llm is not None:
                logger.info("Stage 09: Retrying with strict YAML-only prompt...")
                _retry_prompt = (
                    "Output ONLY valid YAML. No prose, no markdown fences, no explanation.\n"
                    f"Topic: {config.research.topic}\n"
                    "Required keys: baselines, proposed_methods, ablations, "
                    "datasets, metrics, objectives, risks, compute_budget.\n"
                    "Each key maps to a list of strings."
                )
                _retry_resp = _chat_with_prompt(
                    llm,
                    "You output ONLY valid YAML. Nothing else.",
                    _retry_prompt,
                    max_tokens=4096,
                )
                _retry_parsed = _parse_yaml_dict_from_llm(_retry_resp.content)
                if isinstance(_retry_parsed, dict):
                    plan = _retry_parsed
                    _s9_diagnostics["parse_strategy"] = "strict_retry"
                    logger.info("Stage 09: Strict YAML retry succeeded.")

    # BUG-12: Fallback 4 — extract method/baseline names from Stage 8 hypotheses
    if plan is None:
        _hyp_text = _read_prior_artifact(run_dir, "hypotheses.md") or ""
        if _hyp_text:
            import re as _re_hyp
            # Extract method-like names from hypothesis text
            _method_candidates = _re_hyp.findall(
                r"(?:proposed|our|novel|new)\s+(?:method|approach|algorithm|framework|model)[:\s]+[\"']?([A-Za-z][\w-]+)",
                _hyp_text, _re_hyp.IGNORECASE,
            )
            _baseline_candidates = _re_hyp.findall(
                r"(?:baseline|compare|existing|standard|traditional)\s+(?:method|approach|model)?[:\s]+[\"']?([A-Za-z][\w-]+)",
                _hyp_text, _re_hyp.IGNORECASE,
            )
            if _method_candidates or _baseline_candidates:
                logger.info(
                    "Stage 09: Extracted names from hypotheses: methods=%s, baselines=%s",
                    _method_candidates[:3], _baseline_candidates[:3],
                )
                plan = {
                    "topic": config.research.topic,
                    "generated": _utcnow_iso(),
                    "objectives": ["Evaluate hypotheses with controlled experiments"],
                    "datasets": ["primary_dataset"],
                    "baselines": _baseline_candidates[:3] or ["baseline_1", "baseline_2"],
                    "proposed_methods": _method_candidates[:3] or ["proposed_method"],
                    "ablations": ["without_key_component", "simplified_version"],
                    "metrics": [config.experiment.metric_key, "secondary_metric"],
                    "risks": ["validity threats", "confounding variables"],
                    "compute_budget": {"max_gpu": 1, "max_hours": 4},
                }
                _s9_diagnostics.update({
                    "status": "degraded",
                    "degraded": True,
                    "parse_strategy": "hypotheses_extraction_fallback",
                    "fallback_reason": "qwen_yaml_parse_failed",
                    "user_facing_status_zh": "实验计划由 hypotheses 抽取降级生成；适合继续工程验证，但不足以直接支撑科研结论。",
                })

    if plan is None:
        # BUG-12: Use domain-aware names instead of fully generic placeholders
        _topic_prefix = config.research.topic.split()[0] if config.research.topic else "method"
        logger.warning(
            "Stage 09: LLM failed to produce valid experiment plan YAML. "
            "Using topic-derived fallback."
        )
        plan = {
            "topic": config.research.topic,
            "generated": _utcnow_iso(),
            "objectives": ["Evaluate hypotheses with controlled experiments"],
            "datasets": ["primary_dataset", "secondary_dataset"],
            "baselines": [f"{_topic_prefix}_baseline_1", f"{_topic_prefix}_baseline_2"],
            "proposed_methods": [f"{_topic_prefix}_proposed", f"{_topic_prefix}_variant"],
            "ablations": ["without_key_component", "simplified_version"],
            "metrics": [config.experiment.metric_key, "secondary_metric"],
            "risks": ["validity threats", "confounding variables"],
            "compute_budget": {"max_gpu": 1, "max_hours": 4},
        }
        _s9_diagnostics.update({
            "status": "degraded",
            "degraded": True,
            "parse_strategy": "topic_fallback",
            "fallback_reason": "qwen_yaml_parse_failed_and_no_method_names",
            "user_facing_status_zh": "实验计划使用主题兜底生成；仅适合打通流程，需要重新设计真实实验后再写科研结论。",
        })
    if isinstance(plan, dict):
        if _datasets_dir_s9 and _os_s9.path.isdir(_datasets_dir_s9):
            plan.setdefault("local_resources", {})
            if isinstance(plan["local_resources"], dict):
                plan["local_resources"]["datasets_dir"] = _datasets_dir_s9
                plan["local_resources"].setdefault(
                    "usage_requirement",
                    "All core experiments must use this local dataset path rather than unrelated generic benchmarks or synthetic substitutes.",
                )
            if _reference_paper_text:
                benchmark_suggestions = plan.get("benchmark_suggestions")
                if isinstance(benchmark_suggestions, dict) and "datasets" in benchmark_suggestions:
                    benchmark_suggestions.pop("datasets", None)
    # ── Validate implementation_spec presence ────────────────────────────
    # When reference paper text was provided (reproduce mode), the plan
    # MUST contain implementation_spec in proposed_methods.  If missing,
    # retry once with a targeted prompt.
    if (
        _reference_paper_text
        and config.research.reference_purpose == "reproduction"
        and isinstance(plan, dict)
        and llm is not None
    ):
        _methods = plan.get("proposed_methods", [])
        _has_impl = any(
            isinstance(m, dict) and "implementation_spec" in m
            for m in _methods
            if isinstance(m, dict)
        )
        if not _has_impl:
            logger.warning(
                "Stage 09: Plan missing implementation_spec for reproduce project "
                "— retrying with targeted prompt",
            )
            _impl_retry_prompt = (
                "The experiment plan you produced is missing `implementation_spec` "
                "under `proposed_methods`. This is required for code generation.\n\n"
                "Re-read the reference paper text and for EACH proposed method, "
                "add an `implementation_spec` block with:\n"
                "  - class_name\n"
                "  - algorithm_steps (3-10 concrete steps from the paper)\n"
                "  - loss_function (the exact loss from the paper)\n"
                "  - key_hyperparameters (values from the paper)\n"
                "  - required_loss_terms (symbolic loss/component names that must exist in code)\n"
                "  - required_distinct_helpers (non-generic helper methods that must be defined and called)\n"
                "  - required_data_pairing (how examples/prompts/pairs are grouped)\n"
                "  - required_model_edits (structural edits like monkey_patch_forward / replace_module / selective_unfreezing / attach_lora)\n"
                "  - required_runtime_hooks (hook targets or runtime interception points)\n"
                "  - key_methods\n"
                "  - differentiator\n\n"
                "Output the COMPLETE updated `proposed_methods` list as YAML.\n"
                "Include ONLY the `proposed_methods` key.\n\n"
                "Reference paper (first 15000 chars):\n"
                f"{_reference_paper_text[:15000]}\n\n"
                "Current proposed_methods:\n"
                f"{yaml.dump(_methods, default_flow_style=False)}"
            )
            try:
                _impl_resp = _chat_with_prompt(
                    llm,
                    "You are a research engineer. Output ONLY valid YAML.",
                    _impl_retry_prompt,
                    max_tokens=8192,
                )
                _impl_parsed = _parse_yaml_dict_from_llm(_impl_resp.content)
                if isinstance(_impl_parsed, dict):
                    _new_methods = _impl_parsed.get("proposed_methods", [])
                    if _new_methods and any(
                        isinstance(m, dict) and "implementation_spec" in m
                        for m in _new_methods
                        if isinstance(m, dict)
                    ):
                        plan["proposed_methods"] = _new_methods
                        logger.info(
                            "Stage 09: implementation_spec retry succeeded "
                            "(%d methods with specs)",
                            sum(1 for m in _new_methods
                                if isinstance(m, dict) and "implementation_spec" in m),
                        )
                    else:
                        logger.warning(
                            "Stage 09: implementation_spec retry returned no specs",
                        )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Stage 09: implementation_spec retry failed",
                    exc_info=True,
                )

    # ── BA: BenchmarkAgent — intelligent dataset/baseline selection ──────
    _benchmark_plan = None
    # BUG-40: Skip BenchmarkAgent for non-ML domains — it has no relevant
    # benchmarks for physics/chemistry/mathematics/etc. and would inject
    # wrong datasets (e.g., CIFAR-10 for PDE topics).
    _ba_domain_hint_ids: list[str] = []
    _ba_domain_label = ""
    if _domain_profile is not None:
        _ba_domain_hint_ids = [_domain_profile.domain_id]
        _ba_domain_label = _domain_profile.domain_id
        try:
            from researchclaw.domains.detector import is_ml_domain as _is_ml_domain_profile
            _ba_domain_ok = _is_ml_domain_profile(_domain_profile)
        except Exception:  # noqa: BLE001
            _ba_domain_ok = _domain_profile.domain_id.startswith("ml_")
    else:
        _ba_domain_id, _, _ = _detect_domain(
            config.research.topic,
            tuple(config.research.domains) if config.research.domains else (),
        )
        _ba_domain_label = _ba_domain_id
        _ba_domain_ok = _ba_domain_id == "ml"
    if not _ba_domain_ok:
        logger.info(
            "BenchmarkAgent skipped: domain '%s' is not ML (topic: %s)",
            _ba_domain_label, config.research.topic[:80],
        )
    if (
        _ba_domain_ok
        and config.experiment.benchmark_agent.enabled
        and config.experiment.mode in ("sandbox", "docker")
        and llm is not None
    ):
        try:
            from researchclaw.agents.benchmark_agent import BenchmarkOrchestrator
            from researchclaw.agents.benchmark_agent.orchestrator import (
                BenchmarkAgentConfig as _BACfg,
            )

            _ba_cfg_raw = config.experiment.benchmark_agent
            _ba_cfg = _BACfg(
                enabled=_ba_cfg_raw.enabled,
                enable_hf_search=_ba_cfg_raw.enable_hf_search,
                max_hf_results=_ba_cfg_raw.max_hf_results,
                enable_web_search=_ba_cfg_raw.enable_web_search,
                max_web_results=_ba_cfg_raw.max_web_results,
                web_search_min_local=_ba_cfg_raw.web_search_min_local,
                tier_limit=_ba_cfg_raw.tier_limit,
                min_benchmarks=_ba_cfg_raw.min_benchmarks,
                min_baselines=_ba_cfg_raw.min_baselines,
                prefer_cached=_ba_cfg_raw.prefer_cached,
                max_iterations=_ba_cfg_raw.max_iterations,
            )

            _hw = _load_hardware_profile(run_dir)
            _ba = BenchmarkOrchestrator(
                llm,
                config=_ba_cfg,
                gpu_memory_mb=(
                    _hw.get("gpu_memory_mb", 49000) if _hw else 49000
                ),
                time_budget_sec=config.experiment.time_budget_sec,
                network_policy=(
                    config.experiment.docker.network_policy
                    if config.experiment.mode == "docker"
                    else "full"
                ),
                stage_dir=stage_dir / "benchmark_agent",
            )
            _benchmark_plan = _ba.orchestrate({
                "topic": config.research.topic,
                "hypothesis": hypotheses,
                "experiment_plan": plan.get("objectives", "") if isinstance(plan, dict) else "",
                "domain_hints": _ba_domain_hint_ids,
            })

            _s9_diagnostics["benchmark_agent_validation_passed"] = bool(
                _benchmark_plan.validation_passed
            )
            _s9_diagnostics["benchmark_agent_errors"] = list(
                getattr(_benchmark_plan, "validation_errors", []) or []
            )
            _s9_diagnostics["benchmark_agent_warnings"] = list(
                getattr(_benchmark_plan, "validation_warnings", []) or []
            )
            if not _benchmark_plan.validation_passed:
                _s9_diagnostics.update({
                    "status": "degraded",
                    "degraded": True,
                    "fallback_reason": (
                        _s9_diagnostics.get("fallback_reason")
                        or "benchmark_agent_validation_failed"
                    ),
                    "user_facing_status_zh": (
                        "BenchmarkAgent 生成/校验的基准代码未通过；已保留为建议，不作为核心实验计划自动采用。"
                    ),
                })

            # Inject BenchmarkAgent selections into experiment plan only when
            # generated benchmark code passes validation.  Failed validation is
            # still useful evidence, but should not silently overwrite a usable
            # plan with broken dataset/baseline choices.
            if (
                isinstance(plan, dict)
                and _benchmark_plan.selected_benchmarks
                and _benchmark_plan.validation_passed
            ):
                _has_local_dataset_contract = bool(
                    getattr(config.experiment, "datasets_dir", "") or ""
                )
                if not _has_local_dataset_contract:
                    plan["datasets"] = [
                        b["name"] for b in _benchmark_plan.selected_benchmarks
                    ]
                else:
                    logger.info(
                        "BenchmarkAgent datasets preserved as suggestions only because project has local datasets_dir=%s",
                        getattr(config.experiment, "datasets_dir", ""),
                    )
                    plan.setdefault("benchmark_suggestions", {})
                    if isinstance(plan["benchmark_suggestions"], dict):
                        plan["benchmark_suggestions"]["datasets"] = [
                            b["name"] for b in _benchmark_plan.selected_benchmarks
                        ]
                        plan["benchmark_suggestions"]["baselines"] = [
                            b["name"] for b in _benchmark_plan.selected_baselines
                        ]
                        plan["benchmark_suggestions"]["validation_passed"] = True
                # Normalize existing baselines to list of strings
                # BUG-35: LLM may emit baselines as dict, list of dicts,
                # or list of strings — normalize all to list[str].
                _baselines_from_plan = plan.get("baselines", [])
                if isinstance(_baselines_from_plan, dict):
                    _baselines_from_plan = list(_baselines_from_plan.keys())
                elif isinstance(_baselines_from_plan, list):
                    _baselines_from_plan = [
                        item["name"] if isinstance(item, dict) else str(item)
                        for item in _baselines_from_plan
                    ]
                else:
                    _baselines_from_plan = []
                plan["baselines"] = _baselines_from_plan if _has_local_dataset_contract else [
                    bl["name"] for bl in _benchmark_plan.selected_baselines
                ] + _baselines_from_plan
                # Deduplicate baselines
                plan["baselines"] = list(dict.fromkeys(plan["baselines"]))
            elif isinstance(plan, dict) and _benchmark_plan.selected_benchmarks:
                plan.setdefault("benchmark_suggestions", {})
                if isinstance(plan["benchmark_suggestions"], dict):
                    plan["benchmark_suggestions"]["datasets"] = [
                        b["name"] for b in _benchmark_plan.selected_benchmarks
                    ]
                    plan["benchmark_suggestions"]["baselines"] = [
                        bl["name"] for bl in _benchmark_plan.selected_baselines
                    ]
                    plan["benchmark_suggestions"]["validation_passed"] = False

            logger.info(
                "BenchmarkAgent: %d benchmarks, %d baselines selected (%d LLM calls, %.1fs)",
                len(_benchmark_plan.selected_benchmarks),
                len(_benchmark_plan.selected_baselines),
                _benchmark_plan.total_llm_calls,
                _benchmark_plan.elapsed_sec,
            )
        except Exception as _ba_exc:
            logger.warning("BenchmarkAgent failed (non-fatal): %s", _ba_exc)

    # Save benchmark plan for code_generation stage
    if _benchmark_plan is not None:
        try:
            (stage_dir / "benchmark_plan.json").write_text(
                json.dumps(_benchmark_plan.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

    plan = _enforce_topic_experiment_constraints(
        plan,
        topic=config.research.topic,
        metric_key=config.experiment.metric_key,
        metric_direction=config.experiment.metric_direction,
    )
    _applied_constraints = plan.get("user_hard_constraints", {}).get("applied", [])
    if _applied_constraints:
        _s9_diagnostics["user_hard_constraints_applied"] = _applied_constraints
        logger.info(
            "Stage 09: Enforced user hard constraints: %s",
            ", ".join(_applied_constraints),
        )

    plan.setdefault("topic", config.research.topic)
    if isinstance(plan, dict):
        # Every scientific experiment plan gets an executable rigor contract.
        # Downstream code generation must implement it or provenance remains limited.
        protocol = plan.setdefault("evaluation_protocol", {})
        if not isinstance(protocol, dict):
            protocol = {}
            plan["evaluation_protocol"] = protocol
        protocol.setdefault("independent_seeds", [11, 29, 47])
        protocol.setdefault("minimum_seeds_per_condition", 3)
        protocol.setdefault("report", ["mean", "standard_deviation", "95%_confidence_interval"])
        protocol.setdefault("paired_comparison", {
            "required": True,
            "preferred_tests": ["paired_t_test", "wilcoxon_signed_rank"],
            "alpha": 0.05,
        })
        protocol.setdefault("raw_result_requirement", "retain per-seed metrics for every condition")
        _s9_diagnostics["rigor_contract"] = protocol
        plan.setdefault("plan_quality", {})
        if isinstance(plan["plan_quality"], dict):
            plan["plan_quality"].update({
                "status": _s9_diagnostics.get("status", "normal"),
                "degraded": bool(_s9_diagnostics.get("degraded")),
                "parse_strategy": _s9_diagnostics.get("parse_strategy"),
                "benchmark_agent_validation_passed": _s9_diagnostics.get(
                    "benchmark_agent_validation_passed"
                ),
                "scientific_claims_allowed": not bool(_s9_diagnostics.get("degraded")),
                "user_facing_status_zh": _s9_diagnostics.get("user_facing_status_zh"),
            })
    (stage_dir / "exp_plan_diagnostics.json").write_text(
        json.dumps(_s9_diagnostics, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if config.research_engine.enabled and _s9_diagnostics.get("degraded"):
        return StageResult(stage=Stage.EXPERIMENT_DESIGN, status=StageStatus.FAILED,
                           artifacts=("exp_plan_diagnostics.json",),
                           error="No validated scientific plan was produced; refusing a placeholder experiment plan")
    (stage_dir / "exp_plan.yaml").write_text(
        yaml.dump(plan, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return StageResult(
        stage=Stage.EXPERIMENT_DESIGN,
        status=StageStatus.DONE,
        artifacts=("exp_plan.yaml", "exp_plan_diagnostics.json"),
        evidence_refs=("stage-09/exp_plan.yaml", "stage-09/exp_plan_diagnostics.json"),
    )


def _execute_codebase_search(
    stage_dir: Path,
    run_dir: Path,
    config: RCConfig,
    adapters: AdapterBundle,
    *,
    run_id: str = "",
    prompts: "PromptManager | None" = None,
    **kwargs: object,
) -> StageResult:
    """Search for reusable codebases based on the experiment plan.

    Reads exp_plan.yaml, asks LLM to identify relevant GitHub repos or
    papers-with-code, attempts to clone/download them, and records results
    in codebase_candidates.json.
    """
    from researchclaw.llm import create_llm_client

    exp_plan = _read_prior_artifact(run_dir, "exp_plan.yaml") or ""

    llm = create_llm_client(config)

    search_prompt = (
        "Based on the following experiment plan, identify up to 5 existing open-source "
        "GitHub repositories or papers-with-code that could serve as a starting codebase "
        "for this experiment. For each candidate, provide:\n"
        "- repo_url: GitHub URL\n"
        "- description: why it is relevant\n"
        "- usability: 'direct' (can use as-is with minor changes) or 'reference' (useful as reference)\n"
        "- key_files: list of key files/modules to reuse\n\n"
        "Return a JSON array. If no suitable codebase exists, return an empty array [].\n\n"
        "Experiment Plan:\n"
        f"{exp_plan}"
    )

    import json as _json
    import os as _os_s10

    candidates: list[dict] = []

    # ── Phase 1: Index LOCAL codebases already on disk ────────────────
    _local_codebases_dir = getattr(config.experiment, "codebases_dir", "") or ""
    if _local_codebases_dir and _os_s10.path.isdir(_local_codebases_dir):
        _local_repos: list[dict] = []
        for name in sorted(_os_s10.listdir(_local_codebases_dir)):
            full = _os_s10.path.join(_local_codebases_dir, name)
            if _os_s10.path.isdir(full) and not name.startswith("."):
                _local_repos.append({"name": name, "path": full})

        # Use LLM to assess relevance when multiple local repos exist
        _relevance_map: dict[str, str] = {}
        if len(_local_repos) > 1 and llm is not None:
            _repo_names = [r["name"] for r in _local_repos]
            _relevance_prompt = (
                f"Research topic: {config.research.topic}\n\n"
                f"Local codebases available: {_repo_names}\n\n"
                "For each codebase, assess its relevance to the research topic. "
                "Return a JSON object mapping each codebase name to one of: "
                '"high" (directly related, should be used), '
                '"medium" (somewhat related, could be useful), '
                '"low" (unrelated to this research topic).\n\n'
                "Example: {\"RepoA\": \"high\", \"RepoB\": \"low\"}"
            )
            try:
                _rel_resp = llm.chat(
                    [{"role": "user", "content": _relevance_prompt}],
                    system="You are a research engineer. Return valid JSON only.",
                    json_mode=True,
                    max_tokens=256,
                )
                _relevance_map = _json.loads(_rel_resp.content)
                if not isinstance(_relevance_map, dict):
                    _relevance_map = {}
            except Exception:
                _relevance_map = {}

        for _repo in _local_repos:
            _rel = _relevance_map.get(_repo["name"], "high" if len(_local_repos) == 1 else "low")
            candidates.append({
                "repo_url": f"local://{_repo['path']}",
                "description": f"Local codebase: {_repo['name']}",
                "usability": "direct",
                "relevance": _rel,
                "key_files": [],
                "local_path": _repo["path"],
                "download_status": "success",
            })

    # ── Phase 2: LLM-based GitHub search ──────────────────────────────
    try:
        response = llm.chat(
            [{"role": "user", "content": search_prompt}],
            system="You are an expert research engineer. Return valid JSON only.",
            json_mode=True,
        )

        try:
            remote_candidates = _json.loads(response.content)
            if not isinstance(remote_candidates, list):
                remote_candidates = []
        except _json.JSONDecodeError:
            remote_candidates = []

        codebase_dir = stage_dir / "codebases"
        codebase_dir.mkdir(exist_ok=True)
        for i, cand in enumerate(remote_candidates):
            url = cand.get("repo_url", "")
            usability = cand.get("usability", "reference")
            if usability == "direct" and url and "github.com" in url:
                local_path = codebase_dir / f"repo_{i}"
                try:
                    import subprocess
                    result = subprocess.run(
                        ["git", "clone", "--depth", "1", url, str(local_path)],
                        capture_output=True, text=True, timeout=60,
                    )
                    if result.returncode == 0:
                        cand["local_path"] = str(local_path)
                        cand["download_status"] = "success"
                    else:
                        cand["download_status"] = f"failed: {result.stderr[:200]}"
                except Exception as e:
                    cand["download_status"] = f"error: {e}"
            else:
                cand["download_status"] = "skipped"
        candidates.extend(remote_candidates)

    except Exception as e:
        logger.warning("LLM codebase search failed: %s", e)

    # ── Phase 3: Filter out low-relevance candidates, then persist ────
    _filtered = [c for c in candidates if c.get("relevance", "high") != "low"]
    _dropped = len(candidates) - len(_filtered)
    if _dropped:
        logger.info("CODEBASE_SEARCH: dropped %d low-relevance candidates", _dropped)

    try:
        output = _json.dumps(_filtered, indent=2, ensure_ascii=False)
        (stage_dir / "codebase_candidates.json").write_text(output, encoding="utf-8")
    except Exception:
        (stage_dir / "codebase_candidates.json").write_text("[]", encoding="utf-8")

    n_local = sum(1 for c in _filtered if str(c.get("repo_url", "")).startswith("local://"))
    n_remote = sum(1 for c in _filtered if c.get("download_status") == "success") - n_local
    n_total = len(_filtered)
    summary = f"Found {n_total} candidates ({n_local} local, {n_remote} remote downloaded, {_dropped} low-relevance dropped)"

    return StageResult(
        stage=Stage.CODEBASE_SEARCH,
        status=StageStatus.DONE,
        artifacts=("codebase_candidates.json",),
        evidence_refs=("stage-10/codebase_candidates.json",),
        decision=summary,
    )


def _extract_selected_repos(codebase_info_json: str) -> list[str] | None:
    """Extract repo directory names from codebase_candidates.json that S10 marked as relevant.

    Returns None (= copy all) if no candidates have relevance info,
    or a list of directory basenames for repos marked relevant.
    """
    try:
        import json as _json_sr
        candidates = _json_sr.loads(codebase_info_json)
        if not isinstance(candidates, list) or not candidates:
            return None
    except Exception:
        return None

    has_relevance = any(c.get("relevance") for c in candidates if isinstance(c, dict))

    selected: list[str] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        if c.get("download_status") != "success":
            continue
        local_path = c.get("local_path", "")
        if not local_path:
            continue
        repo_name = local_path.rstrip("/").rsplit("/", 1)[-1]
        rel = c.get("relevance", "")
        if has_relevance and rel not in ("high", "medium"):
            continue
        if not has_relevance and c.get("usability") == "reference":
            continue
        selected.append(repo_name)

    return selected if selected else None
