"""Read executable model requirements without imposing a model on other domains."""

from __future__ import annotations

import json
import re
from typing import Any


def executable_requirements(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {}
    return {key: plan[key] for key in (
        "model", "models", "methods", "proposed_methods", "baselines", "implementation_spec"
    ) if key in plan}


def requires_pretrained_model(plan: Any, checkpoints_dir: str = "") -> bool:
    if checkpoints_dir:
        return True
    if not isinstance(plan, dict):
        return False
    model = plan.get("model")
    if isinstance(model, dict) and type(model.get("pretrained")) is bool:
        return model["pretrained"]
    executable = executable_requirements(plan)
    text = json.dumps(executable, ensure_ascii=False).casefold()
    # Loading instructions are affirmative evidence. Mere terms such as
    # "diffusion" (a PDE) or "checkpoint" (saved solver state) are not.
    if re.search(r"\b(?:from_pretrained|load_state_dict|diffusionpipeline|stable diffusion)\b", text):
        return True
    if re.search(r"\b(?:from scratch|pretrained\s*[=:]\s*false|no pretrained|without pretrained)\b", text):
        return False
    return bool(re.search(r"\b(?:pretrained|pre-trained|resnet\d*|vit|bert|clip|llm|lstm|cnn)\b", text))
