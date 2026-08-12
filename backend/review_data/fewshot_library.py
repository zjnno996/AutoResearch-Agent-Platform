"""Expanded few-shot review examples — auto-generated via LLM.

Generated using deepseek-v4-pro from curated seed reviews.
Contains 66 diverse examples across all 7 review dimensions.

Score reference:
  9-10/10 = exceptional (strong accept)
  7-8/10  = good (accept)
  5-6/10  = marginal (borderline)
  3-4/10  = weak (reject)
  1-2/10  = poor (strong reject)
"""

from __future__ import annotations

from .schema import Review, ReviewDataset, DimensionReview


def _make(
    dim_id: str,
    score: float,
    summary: str,
    strengths: list[str],
    weaknesses: list[str],
    suggestions: list[str],
    paper_title: str = "",
    paper_venue: str = "",
) -> Review:
    """Build a Review object quickly."""
    strengths_fixed = [s[:200] for s in strengths[:3]]
    while len(strengths_fixed) < 3:
        strengths_fixed.append("(See paper for details)")
    weaknesses_fixed = [w[:200] for w in weaknesses[:3]]
    while len(weaknesses_fixed) < 3:
        weaknesses_fixed.append("(See paper for details)")
    suggestions_fixed = [s[:200] for s in suggestions[:3]]
    while len(suggestions_fixed) < 3:
        suggestions_fixed.append("(See paper for details)")

    return Review(
        source="curated",
        paper_title=paper_title,
        paper_venue=paper_venue,
        overall_score=score,
        comment_to_author=summary,
        strengths=strengths_fixed,
        weaknesses=weaknesses_fixed,
        suggestions=suggestions_fixed,
        dimensions=[DimensionReview(dimension_id=dim_id, score=score, summary=summary)],
    )

# =============================================================================
# Methodology
# (10 examples)
# =============================================================================

METHODOLOGY_EXAMPLES = [
    _make(
        dim_id="methodology",
        score=9,
        summary="The methodological core of the paper is rigorously developed, with a novel continuous-depth attention mechanism and a solid theoretical foundation. The approach is appropriately chosen and clearly connects to the problem of modeling irregularly-sampled time series.",
        strengths=[
            "The continuous-time attention module (Eq. 5–7) elegantly generalizes dot-product attention to an integral form, and its connection to neural ODEs is formally established in Theorem 1, providing a prin",
            "The design of the adjoint-based training in §3.2 avoids the memory bottleneck of naive backpropagation; the proof of gradient correctness (Appendix B) is thorough and covers edge cases with jumps in t",
            "Ablation experiments in Table 2 systematically isolate the effect of the solver choice (Euler, Dopri5, adaptive) and the attention span, showing that the performance gains are robust across configurat",
        ],
        weaknesses=[
            "The analysis in §4.1 assumes Lipschitz continuity of the attention kernel, but the practical kernel used in Eq. 5 is not globally Lipschitz; the paper does not discuss when this assumption might fail ",
            "While the attention module operates in continuous time, the input embeddings are still obtained from a discrete RNN in §2.3, creating a potential mismatch between the continuous and discrete component",
            "The theoretical memory cost analysis (Table 1) only considers constant step-size solvers; for adaptive solvers (which are recommended), the actual memory usage can vary widely, making the claimed O(1)",
        ],
        suggestions=[
            "Add a discussion of how non-Lipschitz kernels could lead to stiffness or solution explosion, and propose a regularization term or smoothing technique to mitigate this.",
            "Unify the discrete encoder with the continuous attention module by designing a continuous-time encoder, or justify empirically why the current hybrid approach does not degrade the theoretical benefits",
            "Provide a memory analysis for adaptive-step ODE solvers, including worst-case bounds and experimental measurements of peak memory during training with Dopri5.",
        ],
    ),
    _make(
        dim_id="methodology",
        score=8,
        summary="The paper introduces a well-motivated graph rewiring method based on Ricci curvature, and the technical derivation is largely sound. The experimental validation is convincing, though some design choices lack full theoretical underpinning.",
        strengths=[
            "The use of Ollivier-Ricci curvature as a principled metric for detecting over-squashing is creatively adapted to graphs, and Lemma 2 in §3.1 correctly establishes the relationship between negative cur",
            "The proposed curvature-aware diffusion process (Eq. 9–11) elegantly integrates discrete curvature into a continuous diffusion framework, and the spectral analysis in §4.2 provides insight into how the",
            "Extensive experiments on long-range graph benchmarks (Peptides, PascalVOC-SP) in Table 3 include statistical significance tests and show consistent improvements over strong baselines like GCNII and DI",
        ],
        weaknesses=[
            "The rewiring step in §3.2 operates only on existing edges and cannot add new long-range connections; this limits its ability to fully alleviate over-squashing for graphs with very large diameters, yet",
            "The curvature computation in Eq. 5 relies on a local connectivity threshold τ set to 3 without any ablation or theoretical justification, and the sensitivity analysis in Fig. 4 shows large variance fo",
            "The method is applied only to homogeneous graphs; the paper does not address whether the curvature definition remains meaningful for heterophilic or signed graphs, which limits the claimed generality.",
        ],
        suggestions=[
            "Extend the rewiring to allow adding a small number of non-local edges selected via a surrogate curvature proxy, and analyze how many long-range edges are needed for a given diameter reduction.",
            "Include a theoretical bound or heuristic for choosing τ, and conduct an ablation over a wider range of τ to demonstrate robustness.",
            "Discuss the applicability to heterophilic graphs, possibly by adapting the curvature definition to handle negative attention, and test on one heterophilic dataset (e.g., Chameleon).",
        ],
    ),
    _make(
        dim_id="methodology",
        score=9,
        summary="The paper proposes a novel attention mechanism with rigorous probabilistic grounding and a well-designed variational lower bound, making the methodology both principled and effective.",
        strengths=[
            "The formulation in §3.1 derives a Bayesian attention that cleanly separates prior and likelihood terms, yielding a principled alternative to softmax (Eq. 5–7).",
            "The use of the reparameterisation trick in §3.2 to enable end-to-end training of the latent attention variables is technically sound and non-trivial for discrete-like structures.",
            "Ablation experiments in §4.3 systematically isolate the effect of the prior regularisation term (Fig. 5, Table 3) and demonstrate that the model does not degrade when the prior is removed, confirming ",
        ],
        weaknesses=[
            "The theoretical analysis in §3.3 only considers asymptotic convergence of the ELBO under the assumption of independent attention heads; the finite-sample behaviour and impact of head interaction remai",
            "The proposed sampling-based inference (Eq. 12) is computationally expensive during training, yet no complexity analysis or runtime profiling is provided to compare against standard self-attention.",
            "The model relies heavily on hyperparameter τ in the Gumbel relaxation (Eq. 9), but no sensitivity analysis is presented beyond a single value mentioned in §4.2.",
        ],
        suggestions=[
            "Add a non-asymptotic convergence guarantee or empirical convergence diagnostics to strengthen the theoretical contribution in §3.3.",
            "Report training throughput and memory usage relative to a vanilla Transformer baseline, and discuss trade-offs for practical adoption.",
            "Sweep τ over a meaningful range and report performance variance, ideally with a justification for the default value in the final model.",
        ],
    ),
    _make(
        dim_id="methodology",
        score=8,
        summary="The method introduces a spectral normalisation scheme for self-attention layers that is theoretically motivated by Lipschitz constant control, and the overall design is clean and well-integrated.",
        strengths=[
            "The adaptation of power iteration to self-attention in §3.2 is clever and preserves the original architecture’s computational graph, as shown in the algorithmic description of Alg. 1.",
            "The theoretical bound on the Lipschitz constant of the full transformer block (Theorem 1) is correctly derived and provides a concrete justification for the chosen normalisation point within the atten",
            "Experiments on image generation (Table 2) include a careful controlled comparison where the only difference is the normalisation strategy, ruling out confounding factors such as learning rate scheduli",
        ],
        weaknesses=[
            "Theorem 1 assumes the activation function is 1-Lipschitz, but the paper uses GELU (§4.1) which is not exactly 1-Lipschitz, causing a mismatch between theory and implementation that is not addressed.",
            "The spectral normalisation is applied only to the query/key projections (Fig. 2), leaving out the value projection and feed-forward layers without a clear justification for this asymmetry.",
            "Evaluation is limited to a single GAN architecture (StyleGAN2); the generality to other attention-intensive models such as vision transformers is not probed.",
        ],
        suggestions=[
            "Either extend Theorem 1 to accommodate GELU under a corrected Lipschitz constant, or explicitly discuss why the approximation is safe in practice.",
            "Ablate the normalisation of value projections and feed-forward layers separately, or provide a theoretical argument for why they can be omitted from the bound.",
            "Include at least one additional backbone (e.g., ViT on image classification) to demonstrate broader applicability of the methodology.",
        ],
    ),
    _make(
        dim_id="methodology",
        score=5,
        summary="The proposed method combines standard contrastive learning with a specialized augmentation strategy for molecular graphs. While it performs well empirically, the technical novelty is limited and several design choices lack rigorous justification.",
        strengths=[
            "The introduction of functional-group-aware graph augmentations (§3.1) is a sensible domain-specific addition, and the ablation in Fig. 3 confirms that random masking alone is insufficient.",
            "The training protocol in §4.2 carefully follows existing best practices (teacher–student distillation, moving-average update) and the hyperparameter search in Appendix C is thorough.",
            "The benchmark results on Tox21 and SIDER (Table 2) are competitive, and the authors provide runtime comparisons that favor their method over more complex SE(3)-equivariant alternatives.",
        ],
        weaknesses=[
            "The core contrastive framework (Eq. 4) is identical to SimCLR with a projection head, and the augmented views simply replace standard random augmentations with functional group deletions; no new loss ",
            "The motivation for using functional groups as augmentation units is based on a heuristic argument in §2.2 (“chemists think in functional groups”), without any formal connection to the learning objecti",
            "The experiments in §5 rely exclusively on ROC-AUC, but for imbalanced datasets like Tox21, precision-recall curves or F1-scores would be more informative; the paper neither reports these metrics nor j",
        ],
        suggestions=[
            "Derive a connection between functional-group dropout and specific chemical invariances (e.g., scaffold invariance), and potentially add a regularizer to enforce such invariance in the latent space.",
            "Provide a theoretical justification for why this augmentation is optimal, perhaps by analyzing the mutual information between views as in MV-Link.",
            "Report PR-AUC and F1 scores alongside ROC-AUC, especially for highly imbalanced datasets, and compare with baseline methods under the same metrics.",
        ],
    ),
    _make(
        dim_id="methodology",
        score=6,
        summary="The paper adapts a transformer architecture for RL state representation by embedding temporal sequences, which is a reasonable idea. The method is mostly sound but lacks thorough analysis of its key components and makes strong assumptions.",
        strengths=[
            "The temporal positional encoding scheme in §3.1 (positional encoding over steps) is clearly described and integrates naturally with the transformer, and the ablation in Fig. 4 demonstrates that it is ",
            "The use of gated residual connections in §3.2 to handle varying sequence lengths is a practical design choice, and the authors test it on multiple Atari games (Table 1) showing improved sample efficie",
            "The algorithmic contribution is straightforward to reproduce, with pseudocode in Appendix A and explicit hyperparameters in Table A1.",
        ],
        weaknesses=[
            "The motivation for why standard attention over RL trajectories is sufficient is missing; the paper does not discuss whether the Markov property assumed by many RL algorithms is violated when attending",
            "The training procedure in §4 mixes offline pre-collected data with online rollouts but does not control for distribution shift, and the analysis in Fig. 5 shows that performance degrades when the buff",
            "The spatial complexity (O(T^2) memory) is mentioned as a drawback, but the paper does not propose any reduction strategy beyond gradient checkpointing, limiting scalability to longer episodes.",
        ],
        suggestions=[
            "Provide a rigorous analysis of how using a transformer encoder interacts with the Markov assumption, and experimentally verify that the trained Q-function still satisfies Bellman consistency.",
            "Design a buffer management strategy that controls the proportion of online data, and test the method's sensitivity to non-stationary distributions.",
            "Explore efficient attention variants (e.g., Linformer) in the context of RL trajectories, and measure wall-clock time and memory for episodes of length >100.",
        ],
    ),
    _make(
        dim_id="methodology",
        score=6,
        summary="The proposed RL exploration bonus based on random network distillation is a reasonable heuristic, but lacks formal justifications and the design choices are not sufficiently ablated.",
        strengths=[
            "The core idea in §3.1 of using prediction error on a fixed randomly initialised network as an intrinsic reward is novel and empirically shown to improve exploration in sparse-reward environments (Fig.",
            "The training procedure for the target and predictor networks is clearly described in §3.2 and correctly decouples the exploration signal from the policy network.",
            "The ablation in §4.2 compares the proposed distillation target with a learned target, confirming that keeping the target fixed is critical for stability.",
        ],
        weaknesses=[
            "No theoretical justification is given for why prediction error on a random network should correlate with state novelty; the mechanism relies on an unproven topology preservation property that is never",
            "The hyperparameters for the random network architecture (depth, width) are chosen ad-hoc (Table 1) and may heavily influence the bonus scale, yet no sensitivity analysis is performed.",
            "The method is only tested on MuJoCo and Atari suites; the exploration bonus could trivialise tasks with dense rewards or backfire in stochastic environments, but these scenarios are not discussed.",
        ],
        suggestions=[
            "Provide a preliminary theoretical argument or at least a controlled synthetic experiment demonstrating that random network prediction error increases monotonically with state distance in a known metri",
            "Conduct a systematic study varying the random network capacity and report to what extent results are robust; this would greatly strengthen the methodology.",
            "Include a negative result section or discuss failure modes on environments where random network distillation might be harmful, to establish boundaries of the approach.",
        ],
    ),
    _make(
        dim_id="methodology",
        score=5,
        summary="The distributed training algorithm that overlaps gradient communication with backward computation is a practical engineering contribution, but the methodological novelty is limited and the evaluation lacks necessary rigor.",
        strengths=[
            "The reformulation of backward pass scheduling in §3 avoids gradient staleness using a precise dependency analysis (Fig. 2), which is a clean improvement over naive overlap techniques.",
            "The convergence proof in §3.3 under bounded delays is technically correct and gives theoretical backing for the communication batching strategy.",
            "Empirical speedup measurements in Table 2 show a 1.3–1.5× improvement over the standard all-reduce pattern, which is a meaningful practical gain.",
        ],
        weaknesses=[
            "The dependency analysis reuses a well-known topological sort from prior work (Ref [12]), and the algorithmic contribution is essentially a pipelining trick without new theoretical insight.",
            "The convergence proof incorrectly assumes all layers have identical output dimensions, which is violated in the ResNet-50 used in experiments (Table 1); the mismatch is not acknowledged.",
            "Ablations are insufficient: the paper never isolates the effect of the communication schedule from the overlapping of the optimizer step, making it unclear which component drives the improvement.",
        ],
        suggestions=[
            "Cite and differentiate more clearly from the prior pipelining approach in Ref [12], and explicitly state what aspect of the dependency graph is exploited for the first time.",
            "Generalise Theorem 1 to handle varying tensor dimensions typical of vision models, or restrict experiments to architectures that satisfy the assumption.",
            "Perform an ablation that disables the optimizer overlap but keeps the communication schedule, and vice versa, to provide a clear attribution of gains.",
        ],
    ),
    _make(
        dim_id="methodology",
        score=2,
        summary="The methodology description is incomplete and lacks essential details. The approach is not clearly presented, and many aspects are unjustified or seem ad-hoc.",
        strengths=[
            "The idea of using generative models for data augmentation (§2) is generally reasonable, and the examples shown in Fig. 1 look plausible.",
            "The authors run experiments on CIFAR-10 and report some improvement in accuracy (Table 1).",
            "The paper mentions that they use a pretrained StyleGAN, which is a well-known model.",
        ],
        weaknesses=[
            "The sampling procedure in §3.1 is described only verbally, with no equations or algorithm; it is unclear how many synthetic samples are generated per class, how they are filtered, or what latent space",
            "The paper claims that a 'diversity criterion' is applied to keep only novel samples (§3.2), but no mathematical definition of diversity is given, and the threshold is set manually without any sensitiv",
            "The evaluation baseline is a vanilla classifier with no data augmentation (Table 1), which is an unfair comparison; standard methods like AutoAugment or CutMix are not included, making it impossible t",
        ],
        suggestions=[
            "Provide a formal algorithm with exact sampling steps, diversity metric (e.g., FID-based filtering), and pseudocode.",
            "Perform a hyperparameter study on filtering thresholds and number of synthetic samples, showing the trade-off between added data and compute.",
            "Include comparisons with at least two state-of-the-art data augmentation methods (e.g., RandAugment, CutMix) to isolate the contribution of generative augmentation.",
        ],
    ),
    _make(
        dim_id="methodology",
        score=3,
        summary="The paper proposes a novel clustering loss for unsupervised domain adaptation, but the methodological justification is weak and relies on unverified assumptions. The experimental setup is flawed, undermining the claims.",
        strengths=[
            "The clustering regularization term in Eq. 3 is written in a clear form, and the authors motivate it with the goal of aligning cluster structures across domains.",
            "The visualization in Fig. 2 shows that t-SNE embeddings seem more separable with the proposed method.",
            "The theoretical section (§3.2) attempts to derive an upper bound on target error.",
        ],
        weaknesses=[
            "The bound in Eq. 6 assumes the existence of a universal cluster centroid that is shared across domains, which is not justified and likely violated when domains have large shifts; no experiment verifie",
            "The optimization involves two hyperparameters λ₁, λ₂ that are set to 0.1 and 1.0 ‘following prior work’ (§4.1), without any tuning or ablation on the target domain, making it likely that the reported ",
            "The experiment in §5 uses a single random train/test split with no error bars, and the Office-31 dataset is evaluated only on two domain pairs (A→W and W→A), which is too limited to draw general concl",
        ],
        suggestions=[
            "Prove a relaxed bound that does not require a shared centroid, or empirically test the assumption by measuring cluster centroid distances across domains.",
            "Conduct a thorough hyperparameter sensitivity analysis on λ₁ and λ₂ using validation subsets, and report results with standard deviations over multiple runs.",
            "Evaluate on all domain pairs of Office-31, as well as on a more challenging benchmark like Office-Home, and report per-class F1 scores alongside average accuracy.",
        ],
    ),
]

# =============================================================================
# Novelty
# (10 examples)
# =============================================================================

NOVELTY_EXAMPLES = [
    _make(
        dim_id="novelty",
        score=8,
        summary="The paper introduces a genuinely novel approach to few-shot relation extraction by reformulating it as a meta-graph completion problem, which is a marked departure from standard embedding-based methods. The conceptual shift and the supporting theoretical framework are well-argued and substantial.",
        strengths=[
            "The core idea of casting relation extraction as a graph completion problem over labeled and unlabeled examples (§2.2) is original and not a trivial combination of existing components; prior work (e.g.",
            "The theoretical justification in §3.3 (Theorem 2) derives a lower bound on the number of support examples needed for reliable transduction, which is a non-trivial extension of spectral graph theory re",
            "Figure 1 provides a clean, intuitive illustration of how the meta-graph construction differs from conventional instance-level classification, making the novelty accessible even to non-experts.",
        ],
        weaknesses=[
            "While the overall paradigm is novel, the actual architecture used to encode the graph (a standard GCN) is off-the-shelf (§3.1). The paper would benefit from a discussion of whether the novelty lies in",
            "The related work section (§5) understates a closely related line of work on graph-based semi-supervised learning for NLP (e.g., [35, 36]), which also builds graphs over examples. The distinction is no",
            "The experiments are conducted on two standard benchmarks (Table 1), but the paper does not analyze how the graph structure's novelty contributes beyond simply adding more unlabeled data; a synthetic e",
        ],
        suggestions=[
            "Explicitly state in §1 whether the contribution is primarily the problem reformulation or the specific architecture; if the latter, justify why a vanilla GCN suffices.",
            "Expand §5 to include a dedicated subsection contrasting your meta-graph approach with graph-based semi-supervised methods, highlighting conceptual differences and not just empirical comparisons.",
            "Add a controlled experiment where you vary the graph construction heuristics to show that the specific transductive graph (and not just extra data) is responsible for gains.",
        ],
    ),
    _make(
        dim_id="novelty",
        score=9,
        summary="This paper proposes a fundamentally new way to train diffusion models by directly modeling the noise-to-flow mapping with a continuous-time neural ODE, breaking away from step-based prediction. The contribution is both theoretically elegant and empirically impactful.",
        strengths=[
            "The shift from predicting noise at discrete timesteps to learning the underlying vector field of the SDE (Eq. 7) is a conceptual leap, not an incremental tweak; it unifies several existing diffusion v",
            "The theoretical derivation in §3 connecting the Fisher divergence to the path length in function space (Theorem 1) is novel and provides a principled justification for the design, going beyond heurist",
            "The visualizations in Figure 3 clearly demonstrate that the learned vector field captures semantically meaningful directions, a property absent in standard denoising models, which qualitatively unders",
        ],
        weaknesses=[
            "The continuous formulation relies heavily on recently developed techniques for neural ODEs; the paper does not fully clarify which theoretical components are entirely new versus adaptations from the O",
            "The comparison with concurrent work on flow matching ([34], cited only in a footnote in §6) is underdeveloped. The authors should more explicitly articulate the differences, as the two approaches emer",
            "The empirical gains over strong baselines are modest on CIFAR-10 (Table 1, FID 2.91 vs. 3.01), raising the question whether the architectural novelty translates to significant practical advancements o",
        ],
        suggestions=[
            "In §2, clearly delineate which equations are directly borrowed from the neural ODE framework and which are genuinely new derivations specific to diffusion models.",
            "Include a detailed comparison with flow matching in §6, perhaps in a separate paragraph, highlighting conceptual and technical distinctions.",
            "Report metrics beyond FID that might better showcase the benefits of a continuous vector field, such as trajectory smoothness or interpolation quality.",
        ],
    ),
    _make(
        dim_id="novelty",
        score=8,
        summary="The paper introduces a genuinely novel cross-modal contrastive learning paradigm for 3D point clouds, which advances beyond unimodal SSL methods and shows strong empirical results, though some aspects build on known ideas.",
        strengths=[
            "The core idea of contrasting 2D images with 3D point cloud views (Sec. 3.2, Alg 1) is a fresh and well-motivated pretext task not previously explored in this exact form, filling a gap in self-supervis",
            "The theoretical motivation via an information maximization framework (Sec. 4, Theorem 1) provides a principled justification for why cross-modal objectives lead to better representations than multimod",
            "Extensive experiments across five benchmarks (Table 2, Fig. 3) demonstrate consistent and significant improvements over strong unsupervised and even some supervised baselines, underpinning the practic",
        ],
        weaknesses=[
            "The general idea of cross-modal contrastive learning has been explored in vision-language models (e.g., CLIP) and audio-visual tasks; the paper does not sufficiently differentiate its 3D-specific form",
            "The evaluation is confined to synthetic-to-real transfer from ShapeNet to ScanObjectNN; testing on real-world robotic or autonomous driving point clouds would strengthen the claim of generalizability.",
            "A contemporaneous work arXiv:2205.09876 proposes a very similar 2D-3D contrastive framework; lacking a comparison or discussion makes it unclear how the contributions differ.",
        ],
        suggestions=[
            "Explicitly highlight the unique technical challenges of 3D point clouds (irregularity, sparsity) that distinguish this work from prior cross-modal contrastive methods, and include a dedicated related ",
            "Validate the method on at least one real-world out-of-domain dataset (e.g., KITTI or nuScenes) to demonstrate practical novelty beyond synthetic settings.",
            "If the concurrent paper cannot be experimentally compared, add a qualitative analysis of the differences in training objectives or architecture to clarify the incremental novel aspects.",
        ],
    ),
    _make(
        dim_id="novelty",
        score=9,
        summary="The paper proposes an adaptive attention span mechanism that learns per-head window sizes, which is a highly original and elegant solution to a long-standing limitation in sequence modeling, supported by thorough theoretical and empirical backing.",
        strengths=[
            "The learned adaptive span formulation (Sec. 3.1, Eq. 5-7) is a conceptually novel departure from fixed-pattern sparse attention, allowing the model to tailor the receptive field to the head's function",
            "The information-theoretic motivation in Sec. 2.2 (Lemma 1) elegantly connects entropy of attention weights to the need for variable spans, providing a theoretical foundation rarely seen in attention m",
            "The experimental evaluation on four long-document summarization benchmarks (Table 4) not only shows state-of-the-art ROUGE-L but also introduces novel targeted faithfulness metrics (Fig. 3), proving t",
        ],
        weaknesses=[
            "The adaptive span is applied within a standard Transformer encoder-decoder; the novelty lies in the span learning itself, and the overall architecture remains largely incremental, which could dilute t",
            "The mechanism resembles ideas from adaptive computation time (Graves, 2016) and dynamic halting; the paper does not discuss this connection, missing an opportunity to sharpen the claimed originality.",
            "The ablation in Sec. 5.2 compares only fixed-span baselines; without controlling for the increased parameter count from the span predictor, it's unclear how much of the gain stems from added capacity ",
        ],
        suggestions=[
            "Clearly position the adaptive span learning within the context of adaptive computation methods, and emphasize the key differences (e.g., no halting, learned continuous span) to clarify the novelty.",
            "In the ablation, include a non-adaptive baseline with an equivalent number of parameters (e.g., additional dense heads) to isolate the benefit of adaptivity.",
            "Consider releasing span visualizations for a wider set of tasks to further demonstrate the novel behavior and potential interpretability of the learned spans.",
        ],
    ),
    _make(
        dim_id="novelty",
        score=5,
        summary="The paper combines several existing techniques for multi-agent reinforcement learning (attention, curiosity, and centralized critics) in a way that is new but does not present a bold conceptual advance. The integration is well-executed but the novelty is somewhat limited.",
        strengths=[
            "The integration of intrinsic curiosity with a multi-head attention mechanism over agent histories (§3.2) is a non-obvious combination that yields improved exploration, as shown in the ablation study i",
            "The paper introduces a novel regularization term in the attention module (Eq. 5) that encourages diverse information access, which is a small but genuine addition to the literature.",
            "The evaluation on the StarCraft multi-agent benchmark (§4.1) demonstrates that the proposed combination outperforms standard baselines, confirming that the mixing of components is effective where othe",
        ],
        weaknesses=[
            "The core components (centralized critic [6], intrinsic reward [22], attention [31]) are all well-established; the paper does not sufficiently argue why the specific way of combining them is non-trivia",
            "The ablation study (Table 2) only removes one component at a time, but does not test alternative ways of combining the same ingredients (e.g., different attention types or curiosity formulations), lea",
            "The theoretical analysis in §3.4 is a straightforward application of standard convergence proofs for policy gradient methods; it does not provide any new insight into the proposed architecture's behav",
        ],
        suggestions=[
            "Rewrite §1 to explicitly state the intellectual challenge of integrating these components (e.g., the interaction between curiosity and attention dynamics) and what was non-obvious.",
            "Add a comparative experiment where you replace the proposed attention with a simpler graph attention network to demonstrate that the design choices in §3.2 are crucial.",
            "Either deepen the theoretical contribution by analyzing the effect of the exploration bonus on convergence guarantees, or move the current analysis to the appendix and focus the paper on empirical ins",
        ],
    ),
    _make(
        dim_id="novelty",
        score=6,
        summary="The paper extends a standard Transformer with a memory-augmented layer that uses sparse attention and a differentiable key-value store. While the idea is not entirely new, the specific sparse retrieval mechanism is technically sound and leads to tangible gains.",
        strengths=[
            "The proposed sparse hashing technique for memory retrieval (§3.1) is distinct from prior memory-augmented Transformers (e.g., [9, 17]) that use dense retrieval or LSH; the use of learnable locality-se",
            "Figure 2 clearly visualizes the sparsity patterns achieved during training, demonstrating that the model learns to attend to distant tokens in a structured way, which is not observed in standard spars",
            "The experiments on long-range document understanding (Table 1, §4.2) show a 4-point F1 improvement over the strongest baseline, indicating that the memory design offers a practical benefit not capture",
        ],
        weaknesses=[
            "The high-level concept of augmenting Transformers with external memory is well-explored (e.g., Transformer-XL, Compressive Transformer); the paper does not adequately distinguish its contribution from",
            "The ablation study only examines the presence/absence of the memory module, but does not investigate simpler alternatives such as using a dense memory with a retrieval bottleneck, which could be equal",
            "The theoretical analysis in §5.1 of the retrieval error is a direct extension of known bounds for LSH; the novelty in the analysis is marginal, as acknowledged in the paper ('following [4] we derive..",
        ],
        suggestions=[
            "Add a paragraph in §1 that clearly states the limitations of prior memory-augmented Transformers and why a fundamentally different retrieval mechanism is needed (not just a speed improvement).",
            "Conduct an additional baseline that uses standard LSH without learnable hash functions to isolate the benefit of the learnable aspect.",
            "Strengthen the theoretical contribution by showing how the learnable hashing improves not just efficiency but also expressiveness beyond what random projections provide.",
        ],
    ),
    _make(
        dim_id="novelty",
        score=6,
        summary="The paper applies a known combination of PPO and intrinsic curiosity to continuous control tasks, yielding solid empirical results, but the algorithmic novelty is limited and the claims overstate the contribution.",
        strengths=[
            "The integration is well-engineered, with clear implementation details in Sec. 3.2 that enable reproduction, which is a practical contribution for practitioners.",
            "The empirical study in Fig. 2 and Table 1 convincingly shows improved sample efficiency over vanilla PPO across several MuJoCo and DM Control tasks, demonstrating the combination's merit.",
            "The simplicity of the approach means it can be easily adopted, which has value in applied RL.",
        ],
        weaknesses=[
            "The core idea of using intrinsic motivation (ICM) with on-policy algorithms like PPO is not new—Pathak et al. (2017) originally used TRPO/PPO, and Burda et al. (2019) scaled it—yet the paper cites nei",
            "The only novelty appears to be a particular set of hyperparameters (learning rates, reward scaling); no new algorithmic component or theoretical insight is introduced beyond the existing ICM module.",
            "The paper claims a 'novel exploration method' in the abstract, but the curiosity formulation is identical to the original ICM, with no modification to address known issues like the noisy-TV problem.",
        ],
        suggestions=[
            "Significantly tone down the novelty claims and reframe the paper as a rigorous empirical study of PPO+ICM under modern implementation tricks, which still holds value.",
            "Thoroughly review and cite prior work that combined PPO/TRPO with curiosity, and explicitly state what, if anything, is different in this version (e.g., code-level optimizations).",
            "If any insights emerged from the experiments, such as why the combination works better now, highlight those as the contribution rather than the combination itself.",
        ],
    ),
    _make(
        dim_id="novelty",
        score=5,
        summary="The paper provides a tighter convergence bound for SGD with Nesterov momentum, but the improvement is incremental and lacks a clear argument for its practical significance, limiting its perceived novelty.",
        strengths=[
            "The proof technique that unifies analyses of several momentum variants (Sec. 4, Lemmas 2-4) is elegantly constructed and may serve as a useful framework.",
            "The new bound in Eq. (12) improves the constant factor compared to the best known result (Ghadimi & Lan, 2016) by a factor of 2, which is a non-trivial technical improvement.",
            "The analysis is rigorous and the assumptions (smooth non-convex, bounded variance) are standard, making the result easily comparable.",
        ],
        weaknesses=[
            "The improvement over existing bounds is only a constant factor; without demonstrating practical impact (e.g., better learning rate schedules) or new insights, the theoretical contribution feels increm",
            "The paper does not discuss the practical implications of the tighter constant—whether it enables significantly faster convergence in real training runs—leaving the significance of the novelty unclear.",
            "The proof extends a recent result by a single inequality (Eq. 8 to Eq. 9), which reviewers may see as a minor technical extension rather than a novel conceptual advance.",
        ],
        suggestions=[
            "Conduct a small set of experiments on a realistic deep learning task (e.g., training ResNet-18) to show that the bound informs actionable improvements in hyperparameter selection, demonstrating practi",
            "Reframe the contribution to emphasize the proof technique itself as a novel unifying framework, rather than solely the tighter constant, and discuss its potential for analyzing other algorithms.",
            "Explicitly compare the bound numerically with existing bounds on a simple test function to highlight when the factor of 2 difference matters.",
        ],
    ),
    _make(
        dim_id="novelty",
        score=3,
        summary="The paper proposes a few tweaks to the training process of GANs, but overall the contribution feels incremental. I'm not convinced there is enough novelty for a top-tier venue.",
        strengths=[
            "The new loss function in §2 seems to help stabilize training a bit.",
            "Figure 1 shows some nice generated images.",
            "The authors compare to a few standard GANs in Table 1.",
        ],
        weaknesses=[
            "The method is essentially a combination of existing tricks (spectral norm, two time-scale update rule) with a reweighted loss, which is a minor change.",
            "The paper does not discuss any theoretical justification for the new loss, just an intuition.",
            "The improvement over WGAN-GP on CIFAR-10 is only 0.02 in FID, which could be noise.",
        ],
        suggestions=[
            "The authors should show a more significant gap over baselines to claim novelty.",
            "Provide a clear theoretical motivation, not just heuristics.",
            "Maybe try a different task where the advantage is more obvious.",
        ],
    ),
    _make(
        dim_id="novelty",
        score=2,
        summary="This paper is about a new normalization layer that is basically a blend of LayerNorm and BatchNorm. The idea is pretty straightforward and I didn't see much that is surprising.",
        strengths=[
            "The authors test on a few popular image models (Table 2).",
            "The method is easy to implement as per §3.",
            "They provide code in the supplementary.",
        ],
        weaknesses=[
            "The method is just an affine combination of two existing normalization methods (Eq. 2).",
            "The paper does not compare to many recent alternatives like InstanceNorm or GroupNorm in the main experiments.",
            "The theoretical analysis in §4 is very trivial.",
        ],
        suggestions=[
            "The paper needs a stronger motivation for why blending these two specific methods is non-obvious.",
            "Add a thorough comparison with at least 3 other normalization schemes.",
            "Maybe analyze failure cases where the blend fails.",
        ],
    ),
]

# =============================================================================
# Experiment
# (10 examples)
# =============================================================================

EXPERIMENT_EXAMPLES = [
    _make(
        dim_id="experiment",
        score=8,
        summary="The experimental evaluation is thorough and well-structured, with strong comparisons across multiple datasets and careful ablation designs. The main limitations are the lack of confidence intervals for some key results and limited analysis of computational efficiency.",
        strengths=[
            "Comprehensive cross-dataset evaluation (Table 2) covering 5 diverse text classification benchmarks, including both English and multilingual settings, gives strong evidence for generalization.",
            "The ablation study in §4.2 systematically isolates the effect of prompt templates, answer prefixes, and training examples, with a clear Fig 4 showing performance sensitivity.",
            "Paired statistical tests (Wilcoxon signed-rank) are reported in §4.3 for the main comparisons against the best baseline, confirming significance at p < 0.05.",
        ],
        weaknesses=[
            "Fig 3 reports average accuracy over 10 random seeds but does not show any uncertainty (error bars or confidence intervals), making it hard to assess result stability.",
            "The hyperparameter search space is mentioned in §3.4 but not fully enumerated; important details like the number of trials or the range of learning rates tested are omitted, raising reproducibility co",
            "No runtime or memory footprint analysis is provided, even though the proposed method uses additional inference-time computation (see §3.2), which could be a practical barrier.",
        ],
        suggestions=[
            "Add 95% confidence intervals to Fig 3, or at least report the standard deviation explicitly in Table 2.",
            "Include a complete table of hyperparameter search ranges in an appendix, and report the number of tuning trials per dataset.",
            "Provide a wall-clock time and GPU memory comparison for inference in §4.4, perhaps normalised by dataset size, to help practitioners assess adoption cost.",
        ],
    ),
    _make(
        dim_id="experiment",
        score=9,
        summary="An exemplary experimental section that combines scale, rigor, and reproducibility. It is hard to find notable gaps, though out-of-distribution testing and failure analysis could add further depth.",
        strengths=[
            "Evaluation spans two large-scale detection benchmarks, COCO and Cityscapes (Table 1, Table 3), with all standard metrics (AP, AP50, AP75) and fine-grained per-category breakdowns in §4.2.",
            "Ablations in §3.3 and Fig 5 carefully decouple the contribution of each novel module (cross-scale attention, deformable sampling), and include negative controls (e.g., replacing attention with bilinea",
            "All experiments are run with 5 random initializations, and §4.1 reports both mean and standard deviation; additionally, the authors have released training code and pretrained models, greatly enhancing",
        ],
        weaknesses=[
            "While COCO and Cityscapes are standard, there is no evaluation on a more domain-shifted dataset such as BDD100K or nighttime variants, so the robustness to distribution shift remains unproven.",
            "The paper lacks a qualitative failure analysis, which could reveal systematic errors (e.g., small object misses, occlusion cases) that aggregate metrics hide.",
            "Only baselines up to 2022 are considered, omitting recent 2023 transformer-based detectors (e.g., DINO, Co-DETR) that could provide a stronger contrast.",
        ],
        suggestions=[
            "Include a zero-shot or fine-tuning evaluation on an additional dataset with significant domain shift (e.g., weather, illumination) to assess robustness.",
            "Add a subsection with visual examples of failure predictions and a short discussion of shared failure modes, perhaps in an appendix.",
            "Run the proposed method against at least one very recent detector (e.g., Co-DETR) and report the comparison in Table 1, even if results are not ahead, to show up-to-date positioning.",
        ],
    ),
    _make(
        dim_id="experiment",
        score=8,
        summary="The experimental evaluation is comprehensive and well-structured, though missing statistical significance reporting and broader linguistic coverage.",
        strengths=[
            "Extensive comparison against 8 strong baselines spanning pre-tranformers and LLMs in Table 2 covers the state of the art well.",
            "Thorough ablation study in §4.2 and Fig. 3 systematically isolates the contribution of each architectural component.",
            "Detailed error analysis in §5 with qualitative examples provides insight beyond aggregate metrics.",
        ],
        weaknesses=[
            "No statistical significance testing across multiple runs; all results in Table 2 are point estimates without confidence intervals.",
            "Evaluation is restricted to English-only datasets; no cross-lingual generalization is tested, limiting claims of generality.",
            "Hyperparameter sensitivity is only briefly mentioned; the paper lacks a systematic grid search or stability analysis.",
        ],
        suggestions=[
            "Report mean ± std over 5 seeds and apply paired bootstrap tests or similar to validate claims of superiority.",
            "Extend experiments to at least one non-English language (e.g., Chinese, German) to assess cross-lingual transfer.",
            "Provide a thorough hyperparameter study for key parameters (learning rate, dropout, layer count) with ranges and their effect on performance.",
        ],
    ),
    _make(
        dim_id="experiment",
        score=7,
        summary="The experiments are solid and reproducible, but lack evaluation on distribution shift and recent competitor comparisons.",
        strengths=[
            "Well-designed ablation on architecture choices (depth, width, attention type) with clear visualizations in Fig. 4.",
            "Evaluation on three standard benchmarks (CIFAR-10, CIFAR-100, ImageNet) in Table 1 follows community practice.",
            "Reproducibility effort: code, pre-trained models, and configuration files are publicly provided.",
        ],
        weaknesses=[
            "No robustness evaluation on common corruptions or out-of-distribution data (e.g., ImageNet-C, Stylized ImageNet).",
            "Missing a recent strong baseline from 2023 (e.g., EfficientFormerV2); comparisons only go up to 2022.",
            "Failure case analysis is anecdotal (a few cherry-picked images) and not quantitatively categorized.",
        ],
        suggestions=[
            "Run the model on ImageNet-C to measure robust accuracy and compare with baselines under corruption.",
            "Include EfficientFormerV2 or a similarly recent efficient architecture to ensure temporal relevance.",
            "Add a systematic failure breakdown (e.g., per-class error, confusion pairs, feature-level error modes).",
        ],
    ),
    _make(
        dim_id="experiment",
        score=6,
        summary="The experiments demonstrate feasibility on a standard multi-agent scenario, but the evaluation is too narrow and lacks statistical rigor. Additional scenarios and proper uncertainty reporting would strengthen the paper significantly.",
        strengths=[
            "Comparisons in Table 2 include five MARL baselines (QMIX, COMA, IPPO, MAPPO, and QTRAN), covering value-based and policy-gradient methods, which is a reasonable set.",
            "An ablation on the reward-sharing scheme (§5.1) varies the mixing coefficient from 0.2 to 0.8 and shows a clear trend, supporting the design choice.",
            "Learned coordination behaviours are visualised in Fig 4 via t-SNE embeddings of agent actions, giving qualitative insight beyond raw scores.",
        ],
        weaknesses=[
            "The entire evaluation is conducted on a single SMAC map (3s5z_vs_3s6z), severely limiting external validity; performance on this map alone does not demonstrate general multi-agent cooperation.",
            "Fig 2 shows mean episode reward curves over 5 seeds, but no variance (no shaded regions or error bars) and no mention of standard deviation or statistical tests.",
            "Baseline hyperparameters appear to be taken from their original papers without any tuning for the specific map, which may lead to an unfair disadvantage (the proposed method is tuned, as described in ",
        ],
        suggestions=[
            "Evaluate on at least three additional SMAC maps with different difficulty levels (e.g., 2c_vs_64zg, MMM2) and report aggregate results.",
            "Add standard deviation shading to Fig 2 and include a statistical test (e.g., a t-test on final returns) in the caption or text.",
            "Perform a hyperparameter tuning protocol for each baseline using the same number of trials as the proposed method, or clearly justify why the original settings are already optimal.",
        ],
    ),
    _make(
        dim_id="experiment",
        score=5,
        summary="The empirical study covers large-scale distributed training setups and includes useful ablation, but critical practical metrics like wall-clock time are missing and baseline coverage is incomplete.",
        strengths=[
            "Experiments scale up to 64 GPUs (Fig 3), demonstrating that the proposed compression technique maintains training stability and convergence speed at scale.",
            "Ablation over compression ratio in §4.2 (Table 2) clearly shows the trade-off between communication bandwidth and final accuracy, with ratios from 4× to 32×.",
            "Comparison with two recent gradient compression methods (PowerSGD and ECQ-SGD) in Table 1 provides a credible baseline set.",
        ],
        weaknesses=[
            "All convergence results are plotted against training steps, not wall-clock time; this omission hides the actual end-to-end speedup, which is the central claim of communication compression.",
            "The study does not include a baseline without any compression (vanilla distributed SGD) in the larger-scale experiments (only appears in the 8-GPU setting), making it impossible to quantify the overhe",
            "Gradient staleness caused by asynchronous updates is mentioned in §3.1 but never measured or analysed empirically, leaving a key theoretical concern unaddressed.",
        ],
        suggestions=[
            "Replot the main convergence curves (Fig 2, Fig 3) using wall-clock time as the x-axis, or at least add a table of per-epoch wall-clock times for each method.",
            "Add the uncompressed baseline to all experimental configurations (especially at 32 and 64 GPUs) so that the raw communication overhead can be assessed.",
            "Include a simple staleness analysis, e.g., computing the average number of stale steps per worker in an asynchronous run, and relate it to the theoretical analysis in §3.1.",
        ],
    ),
    _make(
        dim_id="experiment",
        score=5,
        summary="The evaluation has good experimental hygiene but is limited in baseline recency and environmental diversity.",
        strengths=[
            "Rigorous multiple-seed evaluation (10 seeds) shown in Fig. 2 with shaded standard deviation.",
            "Ablation of reward components in Table 3 cleanly demonstrates the impact of each shaping term.",
            "Training curves are provided for all methods, allowing inspection of convergence behavior.",
        ],
        weaknesses=[
            "Baselines are outdated (PPO 2017, SAC 2018, TD3 2018) and do not include modern off-policy algorithms (DrQ-v2, DreamerV3).",
            "Evaluation is limited to MuJoCo locomotion tasks; no tests on more complex domains (Meta-World, image-based DMControl).",
            "No sensitivity analysis for critical hyperparameters (entropy coefficient, replay buffer size), only default values used.",
        ],
        suggestions=[
            "Add at least two state-of-the-art baselines from 2022-2023 to demonstrate relative performance.",
            "Test on image-based environments or at least a broader suite (e.g., Meta-World) for generality.",
            "Include a hyperparameter sensitivity study for top-3 critical parameters, e.g., via grid search over realistic ranges.",
        ],
    ),
    _make(
        dim_id="experiment",
        score=4,
        summary="The experiments show speedup but lack competitive baselines, scalability analysis, and standard reproducibility metrics.",
        strengths=[
            "Speedup over a naive implementation is clearly shown in Fig. 1(b) with varying query complexity.",
            "Ablation on the number of parallel workers appears in Fig. 3, giving some insight into scalability.",
            "Runtime breakdown in Table 2 provides a coarse view of where time is spent.",
        ],
        weaknesses=[
            "No comparison with any established system (e.g., Spark SQL, Ray); only a simple single-threaded baseline.",
            "Only a single dataset size (10M rows) is tested; scalability to larger data (100M, 1B) is not demonstrated.",
            "No memory consumption profiling, no multiple-run error bars, and no mention of fault tolerance or recovery behavior.",
        ],
        suggestions=[
            "Compare against at least one mature industrial system on the same setting to contextualize the speedup.",
            "Evaluate on multiple dataset scales (1M, 10M, 100M rows) and report throughput vs. data size.",
            "Report memory usage, run each experiment at least 5 times with error bars, and discuss failure recovery characteristics.",
        ],
    ),
    _make(
        dim_id="experiment",
        score=3,
        summary="The empirical evaluation is minimal and insufficient for validating the theoretical claims. Only a synthetic problem is used, and basic experimental standards are unmet.",
        strengths=[
            "The convergence rates derived in §3 are illustrated on a simple quadratic optimization problem (Fig 1), showing that the observed rate matches the theoretical bound.",
            "The experiment includes a direct comparison with vanilla SGD (Table 1), and the proposed method achieves a lower final loss as predicted.",
            "The optimizer hyperparameters follow the theoretical settings exactly, providing a controlled test of the convergence statement.",
        ],
        weaknesses=[
            "The entire experimental validation is limited to a single synthetic problem (10-dimensional quadratic with a known condition number); no real-world dataset is used, which severely limits the generaliz",
            "There is no sensitivity analysis on the step size or other hyperparameters; the experiments only use the theoretically optimal settings, so robustness to misspecification is unknown.",
            "Only one random seed is used (mentioned in §4), and no error bars or confidence intervals are reported, making it impossible to assess result variability.",
        ],
        suggestions=[
            "Apply the method to at least two standard non-convex benchmarks (e.g., CIFAR-10 with ResNet, GLUE fine-tuning) and report convergence curves.",
            "Conduct a grid search over learning rates around the theoretical optimum, showing the performance landscape in a figure or table, to address practical tuning needs.",
            "Run every experiment with 10 different random initializations and report the mean and standard deviation of final loss in Table 1.",
        ],
    ),
    _make(
        dim_id="experiment",
        score=2,
        summary="The experiments are extremely limited and do not support the claimed contributions; the evaluation is superficial.",
        strengths=[
            "Accuracy on Cora is recorded in Table 1.",
            "A comparison with the basic GCN baseline is present.",
            "Some hyperparameter settings are mentioned in the text.",
        ],
        weaknesses=[
            "Only one small dataset (Cora) is used, which is insufficient to show generalizability.",
            "No ablation study of any model components (message-passing layers, aggregation, attention mechanisms).",
            "No error bars or multiple-run statistics; only a single deterministic number is reported, casting doubt on stability.",
        ],
        suggestions=[
            "Evaluate on a standard set of at least 5-6 graph benchmarks (Citeseer, Pubmed, ogbn-arxiv, etc.).",
            "Conduct an ablation study that isolates each proposed component and quantifies its impact.",
            "Repeat experiments over 10 random seeds and report mean ± std.",
        ],
    ),
]

# =============================================================================
# Writing
# (10 examples)
# =============================================================================

WRITING_EXAMPLES = [
    _make(
        dim_id="writing",
        score=9,
        summary="The paper is exceptionally well-written, with a clear logical flow, precise notation, and effectively designed figures. A few minor notation inconsistencies remain, but overall readability is outstanding.",
        strengths=[
            "The introduction (§1) motivatively frames the problem and clearly states contributions in bullet points, making the scope immediately understandable.",
            "The method description (§3) is accompanied by a well-annotated Figure 2 that visually breaks down the architecture, substantially aiding comprehension.",
            "Notation is introduced systematically (§2.2) and used consistently throughout, with Equation (5) elegantly capturing the loss function with all terms clearly defined.",
        ],
        weaknesses=[
            "In §4.1, the symbol \\( \\mathbf{h} \\) is reused for both hidden states and hyperparameters, leading to momentary confusion.",
            "Figure 3 omits axis labels on the right subplot, requiring the reader to infer from the caption.",
            "The discussion of related work (§6) groups methods thematically but lacks a summarizing diagram or table, making it dense to navigate.",
        ],
        suggestions=[
            "Differentiate notation for hidden states vs. hyperparameters, e.g., use \\( \\mathbf{h} \\) and \\( \\boldsymbol{\\theta} \\).",
            "Add explicit axis labels to all subplots in Figure 3, and expand the caption to explain dashed vs. solid lines.",
            "Consider adding a summary table of related methods categorized by key property to conclude §6.",
        ],
    ),
    _make(
        dim_id="writing",
        score=8,
        summary="The manuscript is well-structured and mostly clear, with effective visualizations. However, some sections are overly dense and a few terminological choices could be refined for precision.",
        strengths=[
            "The paper uses a consistent three-part organization (problem, method, experiments) that guides the reader logically through the material.",
            "Table 1 provides a comprehensive glossary of symbols before the technical sections, greatly reducing cognitive load.",
            "Figure 4's side-by-side comparison of qualitative results is intuitive and uses consistent color mappings across examples.",
        ],
        weaknesses=[
            "Section 3.2 introduces \\( \\lambda \\) as a regularization coefficient without defining it until the next paragraph, causing a brief mental backtrack.",
            "The caption of Figure 5 describes the experiment but does not label the training/test split indicator, leaving the hatched region ambiguous.",
            "In §5.1, the phrase 'significantly outperforms' is used colloquially without a statistical backing, which could mislead readers about the strength of the result.",
        ],
        suggestions=[
            "Define \\( \\lambda \\) in the same sentence it first appears, or at least on the same page.",
            "Revise Figure 5's caption to explicitly mention that hatched bars represent test-set results.",
            "Replace absolute language like 'significantly outperforms' with comparative phrases (e.g., 'achieves higher mean accuracy') to match the descriptive nature of the section.",
        ],
    ),
    _make(
        dim_id="writing",
        score=9,
        summary="The paper is exceptionally well-written, with crisp organization, clear notation, and well-designed figures that make it a pleasure to read. Only minor notational ambiguities and figure captioning issues detract from an otherwise superb exposition.",
        strengths=[
            "The logical flow from problem statement (§1) through related work (§2) and method (§3) is seamless; each section ends with a forward-looking transition that guides the reader.",
            "Notation is introduced with exceptional care: every symbol is defined upon first use, and Table 1 provides a compact glossary of all conventions, which greatly aids comprehension of Eq.(3)–(7).",
            "Figure 2 uses consistent color coding and clear annotations to summarize the multi-stage architecture, making complex interactions immediately graspable.",
        ],
        weaknesses=[
            "In §3.2, the query and key projection matrices are both denoted by 'W', leading to ambiguity when discussing Eq.(4) on p.5; distinct subscripts would prevent confusion.",
            "Figure 3 and Figure 4 display error bands without any explanation in the captions; the meaning (e.g., standard deviation vs. confidence interval) must be inferred from the main text.",
            "The abbreviation 'OOD' appears in the abstract and §1 but is only defined in §5, which may puzzle readers unfamiliar with the term.",
        ],
        suggestions=[
            "Use distinct notation such as W_q and W_k for query and key projections in §3.2.",
            "Expand the captions of Figures 3 and 4 to explicitly describe the shaded regions as 95% confidence intervals.",
            "Define all acronyms at first use; for OOD, introduce it in the abstract or introduction with a complete expansion.",
        ],
    ),
    _make(
        dim_id="writing",
        score=8,
        summary="The writing is clear and engaging on the whole, with well-structured experiments and strong visual aids. However, some sections suffer from dense presentation and a lack of explicit cross-referencing that would improve scannability.",
        strengths=[
            "The introduction (§1) uses a worked example to motivate the segmentation problem, striking a perfect balance between accessibility and technical precision.",
            "Table 2 presents results with columns for each dataset and metric combination, including standard deviations, which makes the performance landscape immediately clear.",
            "Notational conventions from the main body are meticulously maintained in the appendix, fostering a unified reading experience.",
        ],
        weaknesses=[
            "The data augmentation pipeline description in §4.1 runs as a single 18-line paragraph, burying critical hyperparameter choices; readers scanning for specifics will struggle.",
            "Figure 5 (qualitative examples) appears on page 6 but is never referenced until §4.3 on page 8, creating a confusing disjoint for those browsing the PDF.",
            "In §3.3, the gradient symbol '∇θ L' appears without indicating the dimensionality of θ, which could trip up readers less familiar with the parameterization.",
        ],
        suggestions=[
            "Convert the augmentation pipeline in §4.1 into a bullet list or table that catalogs each transform and its parameters.",
            "Insert a forward reference to Figure 5 at the end of §3.2 or move the figure closer to its first detailed discussion in §4.3.",
            "Explicitly define the parameter space (e.g., θ ∈ ℝ^d) in §3.3 or add a brief notation paragraph after the method overview.",
        ],
    ),
    _make(
        dim_id="writing",
        score=6,
        summary="The writing is generally acceptable, but several sections are hard to follow due to missing transitions and inconsistent terminology. The figures are adequate but not fully explanatory.",
        strengths=[
            "The abstract succinctly captures the core idea and main result, giving a good initial overview.",
            "The algorithm box (Algorithm 1) is cleanly presented with line-by-line comments that mirror the equations.",
            "The tables in the experimental section (§5) are well-formatted with clear header rows and consistent decimal places.",
        ],
        weaknesses=[
            "The transition from the problem formulation in §2 to the proposed method in §3 is abrupt; there is no bridging sentence explaining why the formulation leads to the design choices.",
            "In §4.2, the term 'embedding' is used interchangeably for both input and output representations without disambiguation.",
            "Figure 2 uses a color scale that is indistinguishable when printed in grayscale (e.g., red and green lines map to similar gray levels).",
        ],
        suggestions=[
            "Add a short paragraph at the end of §2 that explicitly connects the problem formulation to the high-level motivation of the method.",
            "Reserve 'input embedding' and 'output embedding' throughout, and check for consistent usage in the text and captions.",
            "Make Figure 2 accessible by adding different line styles (dashed, dotted) and providing a grayscale-friendly legend.",
        ],
    ),
    _make(
        dim_id="writing",
        score=4,
        summary="The paper contains many grammatical errors and awkward phrasings that hinder readability. The organization is roughly logical, but the poor writing quality obscures technical contributions.",
        strengths=[
            "Section headers follow a conventional numbering scheme, making it easy to locate topics.",
            "The use of a running example in §3.1 helps ground abstract concepts, though the example itself is poorly written.",
            "The experimental setup in §5.2 lists all hyperparameters, which adds transparency.",
        ],
        weaknesses=[
            "Numerous sentences in the method section (§3.3) are run-ons or missing verbs, e.g., 'The module which we integrate to the pipeline then output transformed feature.'",
            "The caption of Table 2 refers to 'top-1 accuary' (misspelled) and does not define the abbreviation 'acc' used in the table body.",
            "Inferior figure quality: Figure 4's text labels are in a tiny font size (~6pt) that is illegible without zooming in a printed version.",
        ],
        suggestions=[
            "Thoroughly proofread the manuscript, ideally with a native English speaker, paying special attention to verb agreement and sentence fragments.",
            "Correct the typo in Table 2's caption and spell out 'accuracy' or define the abbreviation in the table notes.",
            "Increase the font size of all axis labels and tick marks in figures to at least 8pt to ensure readability in standard print.",
        ],
    ),
    _make(
        dim_id="writing",
        score=6,
        summary="The paper communicates the core ideas but suffers from a disorganized related work section and inconsistent presentation that occasionally obscures the message. The writing is serviceable but would benefit from structural revision.",
        strengths=[
            "The abstract concisely captures the motivation and main contribution, giving a clear snapshot of the paper.",
            "Algorithm 1 is neatly typeset and annotated, providing a reliable implementation reference.",
            "The main result figure in Fig.3 effectively uses color to distinguish methods, and the legend is readable.",
        ],
        weaknesses=[
            "§2 (related work) reads as a sequence of disconnected one-paragraph summaries; it lacks a synthesizing narrative that links prior work to the gaps addressed by the proposed GNN.",
            "Notation for graph convolutions is inconsistent: graph Laplacian is L in §3.1 but later becomes Δ in §3.4 without comment, and vectors sometimes lose their boldface.",
            "Figure 4's caption simply states 'Ablation results', omitting what is being ablated, on which dataset, and what metric is shown.",
        ],
        suggestions=[
            "Rewrite §2 to group papers thematically and clearly state how the proposed architecture overcomes specific limitations of previous approaches.",
            "Standardize all graph operators to a single symbol set and check for boldface consistency in all mathematical typesetting.",
            "Annotate Figure 4's caption with the ablation factor, dataset name, and reported metric (e.g., 'Ablation of neighborhood aggregation depth on Cora, showing F1-score').",
        ],
    ),
    _make(
        dim_id="writing",
        score=5,
        summary="The paper is mostly understandable but the writing style is verbose and notation wanders, making the reading experience unnecessarily laborious. Better cross-referencing and discipline in symbol usage would raise clarity considerably.",
        strengths=[
            "The problem setup in §1 gives a concrete example from robotic manipulation, which helps ground the subsequent RL formulation.",
            "Figure 1 provides a clean schematic of the agent-environment loop with relevant variables overlaid.",
            "Pseudocode (Algorithm 1) succinctly captures the training procedure and is easy to follow.",
        ],
        weaknesses=[
            "The state space symbol changes from S in §3.1 to X in §3.3 with no explanation, causing confusion about whether a transformation has occurred.",
            "Figure 2 is first mentioned in the text on page 7, yet it appears on page 4; readers encounter it out of context and may misinterpret the depicted architecture.",
            "The experimental setup in §4.2 is a wall of text; critical hyperparameters like learning rate and batch size are obscured in prose rather than tabulated.",
        ],
        suggestions=[
            "Audit the notation across all sections and either unify the state-space symbol or clearly state when a mapping is applied.",
            "Re-order figures so each appears after its first in-text citation, or add a brief descriptor ('as previewed in Fig.2') at first occurrence.",
            "Create a hyperparameter table in §4.2 to make tuning details scannable.",
        ],
    ),
    _make(
        dim_id="writing",
        score=2,
        summary="The paper is poorly written and extremely difficult to follow. It lacks a coherent narrative, contains inconsistent notation, and the figures are mostly unlabeled.",
        strengths=[
            "The overall topic is mentioned in the title, which is clear enough.",
            "A few equations (e.g., Eq. (2)) appear to be correctly typed, though their derivation is absent.",
            "(See paper for details)",
        ],
        weaknesses=[
            "The paper lacks a clear structure: §2 is titled 'Related Work' but contains mostly method preliminaries, while the actual related work is scattered across the introduction and §4.",
            "Notation is chaotic: on page 3, \\( \\mathbf{x} \\) is both a raw input and a transformed feature without redefinition, and \\( \\mathbf{W} \\) is used for two different weight matrices in Eq. (3) and Eq. (",
            "Figure 1 is introduced in §1 but is never referenced again; its components (a) and (b) are not mentioned in the text, and the caption simply says 'System overview' without explanation of the diagram.",
        ],
        suggestions=[
            "Reorganize the paper with a dedicated Preliminaries section, a self-contained Related Work, and a clear method section that follows a single line of development.",
            "Introduce and strictly adhere to a notation table; each symbol must have exactly one meaning and should be defined at first use.",
            "Redraw Figure 1 with detailed annotations and reference each panel explicitly in the text with a description of what the reader should notice.",
        ],
    ),
    _make(
        dim_id="writing",
        score=3,
        summary="While the core ideas might be sound, the writing is below the bar for publication. Pervasive language issues, missing definitions, and illegible figures make the paper nearly inaccessible.",
        strengths=[
            "The abstract attempts to summarize the contribution, albeit with grammatical errors.",
            "The attempt to use a consistent font for mathematical symbols is noticed.",
            "(See paper for details)",
        ],
        weaknesses=[
            "Many crucial terms are never defined, such as 'latent alignment' in §3, leaving the reader to guess the meaning.",
            "The captions for Figures 3 and 4 are swapped; Figure 3 shows loss curves but is captioned as 'Accuracy comparison', and vice versa.",
            "Sentence structure is frequently broken: e.g., in §2.1, 'Because the data has high dimension so we apply PCA first then the clustering is performed.'",
        ],
        suggestions=[
            "Define every technical term at its first occurrence; consider a background section that introduces core concepts.",
            "Carefully verify that all figure captions correspond to their actual content, and that cross-references match.",
            "Enlist a professional editing service or a colleague to correct grammatical mistakes and improve sentence flow throughout the manuscript.",
        ],
    ),
]

# =============================================================================
# Related Work
# (10 examples)
# =============================================================================

RELATED_WORK_EXAMPLES = [
    _make(
        dim_id="related_work",
        score=9,
        summary="The related work section is exceptionally thorough, providing a nuanced survey of DP-FL and clearly positioning the paper's contributions. However, one recent seminal work is omitted and the comparison with certain methods could be sharpened.",
        strengths=[
            "In §2.1, the authors systematically survey DP mechanisms for FL from Abadi et al. (2016) to recent adaptive approaches, covering the full spectrum of noise addition schemes.",
            "Figure 2 presents a clear taxonomy of privacy-utility trade-off methods, which helps readers contextualize the paper's approach relative to existing paradigms.",
            "Table 1 provides a detailed feature comparison of 12 prior works, explicitly noting differences in threat models, aggregation methods, and communication efficiency.",
        ],
        weaknesses=[
            "The section overlooks Xu et al. (NeurIPS 2023) on adaptive clipping with per-layer sensitivity analysis, which is a direct competitor and should be discussed to establish novelty.",
            "The comparison with Li et al. (2022) in §2.3 is limited to qualitative claims; no quantitative benchmarks or theoretical bounds are referenced, making it difficult to assess relative merit.",
            "The review predominantly cites works from 2018–2022, with only one 2023 reference, missing the rapid recent advances in user-level DP amplification methods that appeared at recent conferences.",
        ],
        suggestions=[
            "Include a discussion of Xu et al. (2023) and explain how your clipping mechanism differs or improves upon their adaptive scheme.",
            "Add a quantitative comparison sub-section or table that reports key metrics (e.g., privacy budget, test accuracy) for the most comparable methods.",
            "Update the literature to incorporate 2023–2024 works on DP-FL, especially those in top venues (e.g., ICLR 2024), to strengthen the claim of novelty.",
        ],
    ),
    _make(
        dim_id="related_work",
        score=8,
        summary="The related work section on vision transformers for segmentation is well-structured and comprehensively covers the main transformer architectures and medical benchmarks. However, it misses a crucial hybrid architecture and could benefit from a more critical discussion of positional encoding choices.",
        strengths=[
            "§2.2 offers a comprehensive review of CNN-based segmentation baselines (U-Net, nnU-Net, DeepLab) with a clear delineation of their limitations that motivate the transformer approach.",
            "The authors reference all major medical image segmentation benchmarks (BraTS, ACDC, ISLES) in §2.1, demonstrating domain awareness and proper context.",
            "Table 2 contrasts the computational complexity and receptive field properties of pure transformers, Swin transformers, and hybrid models, aiding the reader's understanding of architectural trade-offs.",
        ],
        weaknesses=[
            "The discussion overlooks TransUNet (Chen et al., 2021), a pioneering hybrid CNN-transformer for medical segmentation, which should be explicitly cited and compared against.",
            "In §2.3, the analysis of positional encoding strategies (learned vs. sinusoidal) lacks a critical evaluation of their impact on medical image properties; for instance, the argument that relative posit",
            "The timeline in Figure 3 stops at 2021, omitting significant 2022–2023 advances like nnFormer, MISSFormer, and dynamic convolution hybrids, making the review seem slightly dated.",
        ],
        suggestions=[
            "Add a comparative discussion with TransUNet and explain how your method improves upon its design choices.",
            "Expand the positional encoding discussion to include recent studies on medical domain-specific biases and justify your choice with empirical or theoretical support, potentially citing Dalmaz et al.",
            "Update Figure 3 to include 2022–2023 milestones in medical transformers to show you have surveyed the latest landscape.",
        ],
    ),
    _make(
        dim_id="related_work",
        score=9,
        summary="The Related Work section offers an exceptionally thorough and well-organized synthesis of the instruction tuning literature, clearly delineating where this work fits and what gaps it fills. The authors demonstrate deep familiarity with both foundational and very recent (mid-2024) contributions.",
        strengths=[
            "The taxonomy in §2.1 that partitions prior work into 'data-centric,' 'model-centric,' and 'evaluation-centric' approaches is analytically sharp and immediately clarifies the landscape — this framing i",
            "Table 1 provides a rigorous feature-by-feature comparison across 14 methods, including columns for training data scale, whether human feedback was used, and multi-turn support, making the gap that LLa",
            "The §2.3 discussion of concurrent work (e.g., LLaVA-1.6 and InternVL-1.5 released March–April 2024) is remarkably current and honestly acknowledges overlapping ideas rather than sidelining them, which",
        ],
        weaknesses=[
            "The review focuses almost exclusively on English-language instruction tuning; there is a growing body of work on multilingual instruction following (e.g., Bactrian-X, BayLing) that receives no mention",
            "In §2.2, the authors treat self-instruct and evol-instruct as independent lines of work without discussing the important critique literature on data contamination and synthetic data quality degradatio",
            "The comparison with parameter-efficient methods in §2.4 lumps together adapter-based, LoRA-based, and prefix-tuning methods without noting the efficiency–expressivity tradeoffs documented in He et al.",
        ],
        suggestions=[
            "Add a brief paragraph (3–4 sentences) acknowledging the multilingual instruction tuning literature and explicitly note this as future work for extending the method beyond English.",
            "Cite and briefly engage with the model collapse / data contamination critique literature when describing the self-instruct lineage, and consider discussing what safeguards (if any) were used when gene",
            "In §2.4, add a sentence or two comparing adapter-based methods with LoRA-based alternatives specifically on the criteria of parameter count vs. downstream performance, and justify the architectural ch",
        ],
    ),
    _make(
        dim_id="related_work",
        score=8,
        summary="The Related Work section is well-structured and covers the core areas of diffusion-based video generation comprehensively, though it overstates the novelty of the proposed temporal attention mechanism relative to two closely related prior works. The intellectual lineage is mostly accurate but could ",
        strengths=[
            "The chronological narrative in §2 from score-based diffusion (Song & Ermon, 2019) through latent diffusion (Rombach et al., 2022) to video-specific architectures is pedagogically effective and helps r",
            "Figure 2, which maps the design space of video diffusion models along the axes of temporal modeling strategy (3D conv, factorized, attention-based, recurrent) and conditioning mechanism, is an excelle",
            "The authors correctly identify a genuine gap in §2.3: most cascaded video generation frameworks apply temporal modules only in the low-resolution stage, and the detailed comparison with Imagen Video a",
        ],
        weaknesses=[
            "The claim in §2.2 that 'no prior work has explored factorized spatiotemporal attention with learnable temporal position encodings' is too strong. VideoFusion (Luo et al., 2023) uses a very similar fac",
            "The discussion of conditioning mechanisms in §2.4 omits the important line of work on instruction-based video editing and generation (e.g., InstructVideo, Gen-1 by Runway), which would strengthen the ",
            "Several citations in §2.1 are purely ceremonial — the text name-drops PNDM, DEIS, and DPM-Solver++ as 'other fast sampling methods' without explaining how they differ or why they were not considered a",
        ],
        suggestions=[
            "Soften the novelty claim in §2.2 and move the VideoFusion distinction from the footnote into the main text, explicitly stating what the proposed temporal encoding contributes beyond factorized attenti",
            "Add a paragraph in §2.4 on instruction-based and multi-modal conditioning approaches for video generation (Gen-1, InstructVideo, TokenFlow), even if to demarcate the paper's focus on text-only conditi",
            "Either provide a substantive sentence explaining why each fast sampling method cited in §2.1 is relevant, or prune the list to only those methods that are directly compared against or that inform the ",
        ],
    ),
    _make(
        dim_id="related_work",
        score=6,
        summary="The related work section adequately covers major GNN architectures and molecular datasets but lacks critical depth in analyzing why existing methods fall short, and it omits key recent work on equivariant networks.",
        strengths=[
            "§2.1 provides a solid overview of message-passing GNNs (GCN, GAT, MPNN) and cites the foundational molecular property prediction datasets (QM9, Tox21) in Table 2.",
            "The distinction between 2D and 3D GNNs in §2.3 is a useful organizational structure and helps set the stage for the paper's 3D approach.",
            "The authors correctly position their work relative to standard baselines like SchNet and DimeNet++ and acknowledge the general challenge of modeling long-range interactions.",
        ],
        weaknesses=[
            "The section does not critically analyze failure modes of message-passing, such as over-smoothing and expressiveness limitations; a discussion of why certain architectures fail on equilibrium molecules",
            "Notable omission: equivariant neural networks like E(n)-GNN and SEGNN are entirely absent, yet they represent the state of the art in 3D molecular modeling and are direct comparators.",
            "The positioning statement in §2.4 merely says 'we improve upon prior work' without quantifying the gap; there is no table or metric indicating the typical error reduction offered by the new method ove",
        ],
        suggestions=[
            "Add a paragraph on limitations of MPNNs (over-smoothing, limited expressivity) explicitly referencing relevant theoretical results, to better justify the proposed architecture.",
            "Include and compare with recent equivariant GNNs, and highlight differences in invariance, model complexity, or performance through a side-by-side table or performance summary.",
            "Quantify the improvement over the closest prior work in the related work section by providing a percentage error reduction or ranking, even if preliminary, to concretely position the contribution.",
        ],
    ),
    _make(
        dim_id="related_work",
        score=5,
        summary="The related work section mentions a few prompting methods but is notably shallow, missing major recent developments such as chain-of-thought prompting and lacking systematic organization.",
        strengths=[
            "The paper cites PET and LM-BFF in §2, correctly identifying the early work on pattern-exploiting training for few-shot NLI.",
            "A brief contrast between prompting and traditional fine-tuning is provided in §2.1, setting the stage.",
            "The mention of zero-shot settings is timely and appropriate.",
        ],
        weaknesses=[
            "The section entirely omits chain-of-thought prompting (Wei et al., 2022; Kojima et al., 2022), which has become a dominant paradigm and is highly relevant for complex reasoning tasks like NLI.",
            "The comparison with in-context few-shot learning (§2.3) is limited to one sentence stating 'our approach is better', with no evidence, table, or citation of prior few-shot NLI results.",
            "There is no taxonomy, figure, or table organizing the diverse prompting variants (discrete prompts, continuous prompts, instruction tuning), making it difficult to see where the paper fits in the broa",
        ],
        suggestions=[
            "Integrate chain-of-thought and related reasoning-aware prompting methods, and discuss how your approach relates to or differs from them.",
            "Create a comparative table summarizing key features and performance of recent prompting methods on NLI benchmarks, to clearly position your work.",
            "Add a structured taxonomy (e.g., in a diagram) that categorizes prompting methods and positions your contribution.",
        ],
    ),
    _make(
        dim_id="related_work",
        score=4,
        summary="The related work section covers some offline RL algorithms but is narrow in scope, overlooking entire subfields like model-based offline RL and lacking a critical discussion of underlying assumptions.",
        strengths=[
            "The paper correctly lists CQL and BCQ in §2.2 as prominent model-free offline RL methods, acknowledging the conservative Q-learning approach.",
            "The issue of distribution shift is mentioned in §2.1, which is central to offline RL.",
            "The authors make an attempt to contrast their method with these algorithms in §2.3.",
        ],
        weaknesses=[
            "Model-based offline RL (e.g., MOPO, COMBO) is completely ignored, which is a major omission given that model-based methods often achieve state-of-the-art results and are highly relevant to the paper's",
            "Behavior cloning is not cited or discussed, despite being a fundamental baseline in offline settings and the natural starting point for many methods; the section should explain why more complex RL is ",
            "The comparison in §2.3 is purely descriptive—no discussion of the theoretical assumptions (support constraints, uncertainty quantification) that underpin CQL and similar methods, nor how the proposed ",
        ],
        suggestions=[
            "Add a subsection on model-based offline RL, citing MOPO, COMBO, and MBPO, and explain how your approach differs from or builds upon these.",
            "Include a discussion of behavior cloning and its limitations, possibly with a graph showing its performance relative to RL methods on the benchmark, to strengthen motivation.",
            "Elaborate the theoretical underpinnings: explicitly state the assumptions made by CQL, BCQ, etc., and contrast them with your method’s assumptions, perhaps in a table.",
        ],
    ),
    _make(
        dim_id="related_work",
        score=5,
        summary="The Related Work section provides a reasonable high-level map of graph neural network architectures but lacks analytical depth and does not adequately distinguish the proposed method from closely related prior work on topology-aware message passing. Several important sub-areas are entirely absent.",
        strengths=[
            "The three-part structure in §2 separating 'spectral GNNs,' 'spatial GNNs,' and 'topology-aware GNNs' is logical and helps readers navigate a large literature, and the inclusion of ChebNet and CayleyNe",
            "The authors correctly identify that most spatial GNNs assume a fixed input graph and do not adapt message passing to local topology, which is the central claim that differentiates their work (§2.2, pa",
            "The brief mention of graph rewiring methods (DIGL, SDRF) in §2.3 acknowledges an alternative approach to the same problem and helps frame the contribution as complementary rather than competing.",
        ],
        weaknesses=[
            "The comparison between the proposed topology-aware convolution and prior methods is entirely qualitative: there is no summary table, no complexity analysis, and no enumeration of which existing method",
            "The section completely ignores the important literature on equivariant GNNs and geometric deep learning (e.g., the geometric deep learning blueprint by Bronstein et al. 2021, EGNNs, or tensor field ne",
            "The coverage of scalability-focused GNN literature is missing: graph sampling methods (GraphSAINT, Cluster-GCN), linear GNNs (SGC, APPNP), and positional encoding schemes (Laplacian PE, Random Walk PE",
        ],
        suggestions=[
            "Construct a summary table (even in the appendix if space is limited) comparing at least 6–8 methods on: (i) whether they adapt to local topology, (ii) computational complexity per layer, (iii) number ",
            "Add a paragraph connecting the proposed topology-adaptive weight scheme to the broader equivariance and geometric deep learning framework, even if the connection is informal; this demonstrates awarene",
            "Include citation and brief discussion of at least GraphSAINT or Cluster-GCN to contextualize the scalability claims, and mention whether the proposed method's topology computation is compatible with m",
        ],
    ),
    _make(
        dim_id="related_work",
        score=2,
        summary="The Related Work section is essentially a list of cited papers with little synthesis or critical analysis. It reads as a perfunctory obligation rather than a genuine effort to situate the work within the literature.",
        strengths=[
            "The authors cite a reasonable number of papers (approximately 25), covering some of the well-known object detection frameworks.",
            "The YOLO lineage is mentioned from YOLOv3 through YOLOv7, which at least acknowledges the most popular single-stage detection family that the paper builds upon.",
            "A brief sentence in §2 distinguishes anchor-based from anchor-free detectors, which is a meaningful, if underdeveloped, conceptual distinction.",
        ],
        weaknesses=[
            "The section is outdated — the most recent citation in the detection architecture subsection is from 2021, completely omitting the transformer-based detection revolution (DETR, Deformable DETR, DINO, Y",
            "The discussion of each cited paper is a single sentence of the form 'Author et al. (year) proposed Method X which achieved good results on Dataset Y,' with no analysis of what specific design choices ",
            "There is no comparison table, no taxonomy, and no structured framework for understanding the design space — readers cannot determine what aspects of prior work the proposed method inherits vs. innovat",
        ],
        suggestions=[
            "Completely restructure §2 around a taxonomy of detection design choices (e.g., backbone, neck, head, label assignment, loss function, post-processing) rather than a chronological listing, and populate",
            "Add a comparison table with at least 10 methods listing key design features (anchor-based vs. anchor-free vs. query-based, NMS-free, multi-scale feature fusion strategy, training paradigm) to make the",
            "For each cited paper, add at least one sentence explaining either (a) what specific limitation the proposed method overcomes, or (b) what component the proposed method adopts and why it was chosen ove",
        ],
    ),
    _make(
        dim_id="related_work",
        score=3,
        summary="The Related Work section attempts breadth by touching on multiple sub-areas of prompt engineering, but the treatment of each is too shallow to be informative. Key methodological distinctions are not drawn, and the positioning of the paper's contribution is vague as a result.",
        strengths=[
            "The section's scope is appropriately broad, spanning hard prompt tuning (§2.1), soft prompt tuning (§2.2), and in-context learning (§2.3), which covers the three main paradigms relevant to the paper's",
            "The authors correctly cite seminal works in each sub-area (AutoPrompt, Prefix-Tuning, and the original GPT-3 in-context learning paper), demonstrating baseline awareness of the field's foundations.",
            "The mention of calibration issues in §2.3 (citing Zhao et al. 2021 on recency bias) identifies a real problem with in-context learning that the paper attempts to address.",
        ],
        weaknesses=[
            "The distinction between the proposed method and prior hybrid prompt methods is never made precise — §2 mentions several works that combine soft prompts with discrete templates (e.g., PTR, DPT) but the",
            "The section treats 'prompt engineering' and 'prompt tuning' as interchangeable terms in several places (§2.1, paragraph 1; §2.2, paragraph 2), which conflates the manual design problem with the gradie",
            "No quantitative comparisons with prior methods are provided in the Related Work section (or referenced from results elsewhere) — even a range of reported accuracies or a pointer to Table 3 would help ",
        ],
        suggestions=[
            "Add a clear, specific sentence (or short paragraph) enumerating the exact technical differences between the proposed method and PTR/DPT — e.g., whether the difference is in the prompt representation, ",
            "Standardize the terminology throughout §2: use 'prompt engineering' for manual/hand-crafted prompt design and 'prompt tuning' for learned continuous prompt optimization, and add a footnote clarifying ",
            "Include approximate performance ranges or reference specific result tables from the paper when discussing prior methods, so readers can immediately gauge whether a cited work is competitive, surpassed",
        ],
    ),
]

# =============================================================================
# Reproducibility
# (10 examples)
# =============================================================================

REPRODUCIBILITY_EXAMPLES = [
    _make(
        dim_id="reproducibility",
        score=9,
        summary="The reproducibility of this work is outstanding, with fully open-sourced code, containerization, and meticulously documented training configurations, leaving almost no barrier to exact replication.",
        strengths=[
            "A complete, well-structured codebase with a Dockerfile is hosted on GitHub (see §5.1), allowing one-command reproduction of all experiments, including environment setup.",
            "Appendix B provides an exhaustive 10-page hyperparameter manifest covering every model variant, optimizer, learning rate schedule, batch size, and even random seed values for all reported numbers in T",
            "The data preprocessing pipeline is fully scripted and documented in the repository, with a README explaining each step and the exact data splits used (see §4.1 and the code's data/ directory).",
        ],
        weaknesses=[
            "For the low-resource language pair (XX↔YY), the random seed for shuffling the training data is not reported (§4.3), which could lead to slightly different order-dependent results.",
            "Dependency versions are not pinned with a requirements.txt or conda-lock file, only a high-level list in the README; this risks future breakage due to library updates (e.g., transformers, tokenizers).",
            "No pre-trained model checkpoints are released, meaning verification of the headline 72-hour training result in Table 1 requires a full, expensive retraining.",
        ],
        suggestions=[
            "Explicitly report the data-shuffling seed for the low-resource experiments in §4.3 to ensure exact replication across runs.",
            "Add a frozen requirements file, or better an environment.yml, with strict version numbers to guarantee long-term reproducibility, as recommended by the ML reproducibility checklist.",
            "Upload the final model weights to a persistent service like Zenodo or Hugging Face Hub so that reviewers and future users can validate inference metrics without repeating the costly training.",
        ],
    ),
    _make(
        dim_id="reproducibility",
        score=8,
        summary="The paper provides clear algorithmic descriptions and comprehensive hyperparameter tables, but the absence of public code and incomplete environment details prevent full reproducibility.",
        strengths=[
            "Algorithm 1 in §3 gives a precise, line-by-line pseudocode of the gated attention mechanism, making the novel contribution unambiguous and implementable.",
            "Table 3 exhaustively lists all training hyperparameters (optimizer, learning rate, batch size, weight decay, dropout rates) for both the proposed model and all baselines.",
            "The dataset preparation is described in sufficient detail in §4.1, including patient-level separation for train/val/test splits to avoid leakage, which is critical for medical image segmentation.",
        ],
        weaknesses=[
            "Code is declared as 'available upon request' but no link is given, and no supplementary material was provided during submission (see §5); this is a major barrier to immediate verification.",
            "The data augmentation pipeline in §3.3 is described only qualitatively (e.g., 'random rotation and scaling'), lacking the exact parameter ranges (angle, scale factor, probability) that heavily influen",
            "No hardware or software environment information is mentioned—GPU model, CUDA version, deep learning framework version, or even approximate training time per epoch—making it impossible to reproduce or ",
        ],
        suggestions=[
            "Release the full code on a public repository (e.g., GitHub under an MIT license) and provide the link in the camera-ready version; remove the 'upon request' barrier entirely.",
            "Add a supplementary table specifying all augmentation parameters: rotation degree range, zoom factors, elastic deformation sigma/alpha, and their probabilities.",
            "Include a brief 'Hardware and Software' section in the appendix listing the exact GPU, driver version, PyTorch/TensorFlow version, and OS to contextualize training time and memory usage.",
        ],
    ),
    _make(
        dim_id="reproducibility",
        score=9,
        summary="Reproducibility is nearly exemplary, with comprehensive environment and training specifications, but minor omissions in hardware and evaluation details prevent a perfect score.",
        strengths=[
            "All MuJoCo environment versions and specific reward functions are detailed in Appendix B, including termination conditions.",
            "The full hyperparameter search space and best configurations for each task are provided in Table 3, with random seeds used for search.",
            "Code repository includes exact commit hash, dependency versions (requirements.txt), and scripts to reproduce all figures.",
        ],
        weaknesses=[
            "Hardware specifications (GPU model, CPU, RAM) are never mentioned anywhere in the paper or appendix, affecting ability to estimate runtime reproducibility.",
            "Evaluation episodes per trial not reported; §5 only says 'we evaluated every 10k steps' without number of episodes or aggregation.",
            "Final performance numbers in Table 1 are based on 5 runs, but the seeds for those runs are not listed, only stating '5 random seeds'.",
        ],
        suggestions=[
            "Add a hardware specification section detailing GPU/CPU/RAM in the appendix or a footnote.",
            "Clarify evaluation protocol in §5: episode count per evaluation and how returns are averaged.",
            "Provide the exact random seeds used for the final results to enable precise reproduction checks.",
        ],
    ),
    _make(
        dim_id="reproducibility",
        score=8,
        summary="The paper provides extensive implementation details and released code, though a few crucial reproducibility elements like random seeds and computational requirements are missing.",
        strengths=[
            "Hyperparameters for all models meticulously tabulated in Appendix A, including learning rate schedules and batch sizes.",
            "Complete training pipeline code released on GitHub with a clear README and environment specification (Dockerfile).",
            "Dataset preprocessing steps described in §3.2 with reproducible scripts provided in the repository.",
        ],
        weaknesses=[
            "No random seed is specified for data splits or model initialization in §4.1, making exact replication impossible.",
            "Approximate training time per experiment is never reported, a critical practical consideration missing from §5.",
            "Ablation study in §4.3 uses a subset of hyperparameters without listing the configurations, only stating 'we varied X' without values.",
        ],
        suggestions=[
            "Clearly state all random seeds used for splitting, initialization, and any other stochasticity in §4.1.",
            "Add a table or note in §5 with approximate GPU hours for key experiments to aid resource estimation.",
            "Provide the specific hyperparameter values for each ablation configuration, perhaps as supplementary table.",
        ],
    ),
    _make(
        dim_id="reproducibility",
        score=6,
        summary="While the authors leverage well-known baselines and provide a hyperparameter table, they do not release code for their own modifications, which severely hampers independent reproduction of the proposed method.",
        strengths=[
            "The architecture is explicitly described in §3.2 with layer sizes, activation functions, and the structure of the adaptive entropy module, so a competent practitioner could attempt a reimplementation.",
            "Table 1 summarizes the key hyperparameters (policy learning rate, entropy coefficient range, buffer size) for the main experiments, giving a concrete target for reproduction.",
            "The paper builds on the publicly documented Stable-Baselines3 library (cited in §4), providing a reference implementation for the baseline and environment interactions.",
        ],
        weaknesses=[
            "No code or configuration files for the adaptive entropy adjustment are provided, not even as a diff against the Stable-Baselines3 codebase, leaving the exact implementation details to guesswork (see §",
            "Random seeds for environment resets, network initialization, and action sampling are not reported anywhere; without these, exact numerical reproduction of Figure 2's learning curves is impossible.",
            "The evaluation is limited to a single MuJoCo environment (HalfCheetah-v3); the method's reproducibility and stability on other standard benchmarks (Hopper, Walker2d) is completely untested.",
        ],
        suggestions=[
            "Publish a minimal, self-contained implementation on GitHub that shows how the entropy coefficient update is integrated into the policy gradient loop, with clear comments.",
            "Report all random seeds used (numpy, torch, environment) in a table and, ideally, fix the global seed in the main script so that results are exactly reproducible.",
            "Extend the evaluation to at least three additional continuous-control benchmarks (e.g., Hopper, Walker2d, Ant) to demonstrate that the method is not overfitted to a single task and can be reliably rep",
        ],
    ),
    _make(
        dim_id="reproducibility",
        score=5,
        summary="The mathematical formulation is clear, but several implementation details essential for reproducing the training protocol are missing, leaving a wide gap between theory and practice.",
        strengths=[
            "Equation (4) precisely defines the DropEdge stochastic masking operation, leaving no ambiguity about the core algorithmic contribution.",
            "The training objective in §2.3 is explicitly written as cross-entropy loss with an L2 penalty term, specifying what is minimized during optimization.",
            "Dataset splits and node/edge statistics are provided in §4.1, which is useful for anyone attempting to recreate the data ingestion pipeline.",
        ],
        weaknesses=[
            "The learning rate schedule is completely absent—no mention of initial value, warmup steps, decay type (step, cosine), or final value—yet it is well known to critically affect convergence of graph mode",
            "Weight initialization for the attention layers and linear transformations is never discussed; different default initializations (Xavier, Kaiming) can lead to vastly different results.",
            "No pseudocode or reference code is given for the custom message-passing function that implements DropEdge, making it extremely difficult to verify the method on a new dataset without extensive trial a",
        ],
        suggestions=[
            "Add a paragraph or table detailing the optimizer (e.g., Adam), learning rate schedule (e.g., 0.001 for first 100 epochs, decay by 0.5 every 50), and number of training epochs.",
            "Explicitly state the initialization scheme used for all learnable parameters (e.g., Xavier uniform with gain 1.0) and whether bias terms were zero-initialized.",
            "Provide a succinct pseudocode for the DropEdge forward pass and, ideally, release a minimal PyTorch Geometric or DGL script on GitHub that replicates the main result in Table 2.",
        ],
    ),
    _make(
        dim_id="reproducibility",
        score=5,
        summary="The paper describes the architecture and key hyperparameters, but lacks the code release and full data preprocessing details needed for straightforward reproduction.",
        strengths=[
            "The U-Net architecture is described layer-by-layer in Table 1, with filter counts and padding explicitly given.",
            "Learning rate, batch size, and number of epochs are clearly stated in §3.2.",
            "Pretrained backbone weights are publicly available and cited (ResNet-50 from torchvision).",
        ],
        weaknesses=[
            "No code repository or link is provided, relying solely on textual description; a significant barrier to reproduction.",
            "Data augmentation pipeline in §3.1 mentions 'random crops, flips, and color jitter' but omits parameters like crop size, flip probability, jitter strength.",
            "Optimizer is given as 'Adam' but missing epsilon and betas (only learning rate specified), which can affect training dynamics.",
        ],
        suggestions=[
            "Release the full training and evaluation code on GitHub, with a permissive license.",
            "Provide the exact augmentation configurations (e.g., random crop size 256x256, horizontal flip p=0.5) in a table.",
            "Specify Adam optimizer betas, epsilon, and any weight decay, as these are crucial for exact replication.",
        ],
    ),
    _make(
        dim_id="reproducibility",
        score=4,
        summary="The theoretical components are fully laid out, but the empirical validation is under-documented, making experimental reproduction difficult.",
        strengths=[
            "All assumptions (A1-A5) are listed in §2 with precise mathematical notation, enabling verification of conditions.",
            "Proofs in Appendix C are step-by-step and include intermediate lemma statements, facilitating re-derivation.",
            "For the synthetic data experiment, the data generation process is mathematically defined in Eq. (12) with parameters like variance and dimensionality.",
        ],
        weaknesses=[
            "No code is provided for the simulations in §4, despite them being simple Python scripts; only a high-level description of the algorithm.",
            "The stochastic gradient method uses a 'random mini-batch' but does not state the random seed or batch ordering, crucial for stochastic results.",
            "Experimental results are reported as single numbers without error bars, and no mention of multiple runs or variance.",
        ],
        suggestions=[
            "Release a self-contained Python notebook that reproduces all synthetic experiments.",
            "Specify the random seed for the mini-batch sampling in §4 to ensure exact reproducibility.",
            "Run multiple trials and report confidence intervals to capture stochastic variance.",
        ],
    ),
    _make(
        dim_id="reproducibility",
        score=2,
        summary="The paper does not provide any code, data, or detailed experimental setup, making the experimental claims essentially irreproducible.",
        strengths=[
            "Theorem 1 in §3 is stated with clearly enumerated assumptions.",
            "The synthetic data generator is mentioned in passing in §4.1.",
            "The paper uses a standard MNIST dataset for one of the experiments.",
        ],
        weaknesses=[
            "No code or link to a repository is provided anywhere in the paper, despite a statement that it would be made public (see §5).",
            "The hyperparameters for the synthetic experiment and the MNIST classifier are not aggregated; they are scattered across paragraphs in §4.2 without a single table.",
            "No random seed is specified for the synthetic data generation, and no environment details (hardware, Python version) are reported.",
        ],
        suggestions=[
            "Release the actual scripts used to generate the synthetic data and the MNIST training loop, even as supplementary material.",
            "Collect all hyperparameters into a concise table in the appendix, and explicitly state all random seeds.",
            "Add a short reproducibility section listing the hardware, library versions, and runtime for each experiment.",
        ],
    ),
    _make(
        dim_id="reproducibility",
        score=3,
        summary="The authors claim reproducibility is a goal but provide only superficial descriptions; critical components like code and exact configurations are missing.",
        strengths=[
            "Figure 2 gives a high-level diagram of the system architecture, which gives a rough idea of the components.",
            "The training procedure is outlined in §4.2 in a step-by-step fashion.",
            "Some parameter values like compression ratio are mentioned in the text of §3.3.",
        ],
        weaknesses=[
            "No source code is shared, not even as a supplementary archive, despite the paper stating it 'will be open-sourced'—a broken promise at submission.",
            "Key hyperparameters (learning rate, momentum, gradient clipping) are scattered across different paragraphs and never consolidated into a table, making them easy to miss.",
            "The hardware specifications for the distributed training experiments (GPU type, inter-node network, number of nodes) are not reported, yet they are crucial for interpreting throughput numbers in Table",
        ],
        suggestions=[
            "Upload the full training code to GitHub with a clear README before the next review cycle.",
            "Add a hyperparameter table in §4 that summarizes all optimizer settings, batch sizes, and system-level parameters.",
            "Provide a complete specification of the computing environment, including CPU/GPU models, CUDA version, and interconnect bandwidth, in a dedicated Hardware/Software subsection.",
        ],
    ),
]

# =============================================================================
# Ethics
# (6 examples)
# =============================================================================

ETHICS_EXAMPLES = [
    _make(
        dim_id="ethics",
        score=9,
        summary="The paper demonstrates exemplary ethical consideration, embedding fairness constraints and privacy guarantees directly into the algorithm, and provides a comprehensive societal impact analysis. It excels in concrete, evidence-anchored ethical reasoning, but lacks intersectional analysis and deployme",
        strengths=[
            "Explicit fairness constraints incorporated into the MDP formulation, balancing efficiency and equity, as shown in Eq. 4 in §3.2.",
            "Thorough sensitivity analysis of fairness-accuracy trade-offs with Pareto frontier in Fig. 5, demonstrating robustness to choice of fairness metric.",
            "Societal impact statement in §7 engages with potential for misuse, includes consultation with domain experts from underserved communities, and proposes a monitoring framework.",
        ],
        weaknesses=[
            "No intersectional fairness analysis (e.g., race × income) despite multidimensional sensitive attributes listed in Table 1; fairness metrics only consider single dimensions.",
            "Privacy implications of the patient-level data used for training are not addressed; the paper assumes aggregated statistics, but re-identification risks remain if model outputs are queried (see discus",
            "Deployment risks in low-resource settings (e.g., rural clinics) are only briefly mentioned in §7; no empirical study on how fairness constraints may interact with data quality degradation in these con",
        ],
        suggestions=[
            "Conduct subgroup fairness analysis for at least two intersecting attributes (e.g., race and socioeconomic status) and report group-wise utility.",
            "Discuss privacy-preserving learning techniques (e.g., differential privacy) and estimate the privacy budget for the reward function, or clarify why they are not needed.",
            "Include a more detailed deployment roadmap, perhaps through simulation with synthetic data mimicking low-resource scenarios, to assess generalizability of fairness guarantees.",
        ],
    ),
    _make(
        dim_id="ethics",
        score=7,
        summary="The paper includes a solid ethics section with a detailed bias audit and careful data handling, but the fairness evaluation is too narrow and lacks actionable safeguards for identified risks. It is a good start but needs deeper intersectional and practical analysis.",
        strengths=[
            "The paper includes a dedicated ‘Ethical Considerations’ section (§5) that thoroughly analyzes potential for harm, including over-reliance and triggering content.",
            "Bias audit in §4.3 uses the BOLD dataset to measure toxicity and sentiment across gender and race, with per-group scores in Table 3.",
            "Clear documentation of data sources and filtering steps in §3.1, with explicit statement that no private messages were used, addressing privacy concerns.",
        ],
        weaknesses=[
            "The evaluation of fairness is limited to binary gender and broad racial categories, ignoring non-binary identities and intersectional biases (e.g., Black women), which could be especially harmful in m",
            "While the paper acknowledges potential for misuse (e.g., malicious actors generating manipulative content), it does not propose any safeguards or detection mechanisms, making the discussion somewhat u",
            "There is no mention of transparency about the model’s limitations to end-users; in a deployed chatbot, failing to disclose that it's an AI could violate ethical guidelines for informed consent.",
        ],
        suggestions=[
            "Extend the bias evaluation to include non-binary gender and intersectional groups, and report disaggregated results for mental health-related prompts.",
            "Propose concrete mitigations for malicious use, such as a public benchmark for detecting generated mental health advice or a watermarking system for synthetic text.",
            "Discuss user-facing disclosure requirements and suggest a template for informed consent when deploying such a system in real-world settings.",
        ],
    ),
    _make(
        dim_id="ethics",
        score=5,
        summary="The paper mentions fairness but provides only superficial evaluation, failing to deliver per-group metrics or address privacy and dual-use risks. The ethical coverage is incomplete and lacks depth, though the acknowledgment of the problem is a minimal positive.",
        strengths=[
            "Acknowledges the importance of fairness in the introduction (§1) and cites prior work on racial bias in face recognition.",
            "Reports overall accuracy on the IJB-C dataset, which includes diverse subjects (Table 2).",
            "The method does not require collecting new biometric data, as it uses existing public benchmark datasets.",
        ],
        weaknesses=[
            "Fairness evaluation is minimal: Fig. 3 reports only overall ROC curves without any per-group breakdown, despite datasets having demographic labels. The paper states ‘we observe no significant bias’ ba",
            "No ethical review board (IRB) approval or mention of data usage agreement for the facial images, which could have privacy implications if derived from web-scraped sources (e.g., VGGFace2).",
            "Potential for dual-use in surveillance is completely ignored; as a face recognition system, the technology could be deployed for mass monitoring, yet the paper fails to discuss this societal risk.",
        ],
        suggestions=[
            "Compute and report accuracy/error rates across demographic subgroups (gender, race) using the provided annotations, and test for statistical differences.",
            "Include a statement on data provenance and IRB review, or justify why it was not required, and discuss privacy of training data.",
            "Add a ‘Broader Impact’ section that explicitly addresses the risks of surveillance applications and suggests possible technical or policy safeguards.",
        ],
    ),
    _make(
        dim_id="ethics",
        score=6,
        summary="The work uses differential privacy and acknowledges data heterogeneity, which are positive initial steps for ethics in federated health learning. However, it lacks concrete fairness analysis across participant subgroups and a substantial broader impact discussion, leaving important ethical dimension",
        strengths=[
            "Uses differential privacy (DP) with a clearly stated privacy budget ε=4.0 in §4.2, and provides analysis of utility-privacy tradeoff in Fig. 3.",
            "Acknowledges data heterogeneity across hospitals as a challenge in §1, and uses FedProx to address it.",
            "The study is conducted on a real-world multi-hospital dataset with de-identified electronic health records (Table 1), which is more realistic than simulated splits.",
        ],
        weaknesses=[
            "No fairness analysis across hospitals: Fig. 4 reports only global AUC and loss, without disaggregating performance by hospital or patient demographics (e.g., race, insurance status). Heterogeneity mig",
            "The broader impact statement in §6 is generic and does not discuss potential harms from false predictions or unequal access to quality diagnostics; differential privacy alone does not guarantee equita",
            "Privacy risks of model inversion or membership inference are mentioned only in passing; the paper does not evaluate these attacks or discuss whether the chosen ε provides meaningful protection against",
        ],
        suggestions=[
            "Report per-hospital and per-demographic-group performance metrics, and investigate whether the federated model introduces disparate impact.",
            "Conduct an ablation study on the effect of differential privacy noise on different subgroups; it's possible that noise disproportionately degrades performance for minority subgroups.",
            "Expand the broader impact section with a stakeholder-informed risk assessment, and discuss mitigations beyond DP, such as model auditing and transparency reports.",
        ],
    ),
    _make(
        dim_id="ethics",
        score=2,
        summary="The paper lacks any meaningful ethical reflection, with a dismissive one-sentence statement and no consideration of bias, privacy, or societal impact. Despite its theoretical nature, the authors fail to engage with even minimal responsible research practices.",
        strengths=[
            "The studied problem (low-rank matrix completion) is abstract and does not inherently involve sensitive attributes, as noted in §1, which limits direct ethical risks.",
            "The experiments use synthetic data and publicly available MovieLens dataset (Table 1), which are well-established and do not expose private user information.",
            "The paper does not propose deployment in a high-stakes application, staying within the theoretical contributions.",
        ],
        weaknesses=[
            "Despite using MovieLens, there is no discussion of potential biases in the dataset (e.g., imbalanced demographics) and how these might affect the algorithm's practical performance if applied to recomm",
            "The ‘Ethics Statement’ in §6 is a single sentence asserting ‘This work has no ethical concerns,’ which is an oversimplification; any algorithmic improvement can have downstream societal impacts when a",
            "No mention of dual-use: the improved efficiency could be employed by malicious actors for large-scale user profiling without considering privacy implications.",
        ],
        suggestions=[
            "Expand the ethics discussion to acknowledge that recommendation systems can amplify biases; at minimum, note that the algorithm does not address fairness and is not ready for deployment in sensitive c",
            "Even in a theoretical paper, include a brief analysis of potential negative societal consequences drawn from analogous applications, and propose directions for future work on fairness in matrix factor",
            "Replace the dismissive ethics statement with a more nuanced reflection on the responsibilities of algorithm designers.",
        ],
    ),
    _make(
        dim_id="ethics",
        score=3,
        summary="The paper hardly addresses ethics, with only a superficial mention of physical safety and no broader societal impact analysis. The review highlights glaring omissions but fails to provide deep critique or concrete suggestions, reflecting a low-effort ethical assessment.",
        strengths=[
            "The abstract mentions a ‘safety module’ that halts the robot upon detecting human proximity, showing basic attention to physical safety.",
            "Experiments are conducted in a controlled laboratory environment with no risk to human subjects (simulation and robot cells, as per §3.1).",
            "The system uses only synthetic object data (CAD models) and does not require real-world data collection that might violate privacy.",
        ],
        weaknesses=[
            "No ethical considerations section at all; the paper entirely omits discussion of broader societal impact, such as workforce displacement, misuse for weaponization, or economic inequality exacerbated b",
            "The safety module is only mentioned in one sentence; there is no analysis of its failure modes, reliability, or how it handles edge cases (e.g., visually obscured humans), which is insufficient for a ",
            "No mention of fairness across object types or users; if the grasping system is deployed in assistive settings (e.g., for people with disabilities), biases in grasping certain objects could have differ",
        ],
        suggestions=[
            "Add a dedicated ‘Ethics and Societal Impact’ section that discusses potential job displacement, dual-use risks, and the need for responsible deployment guidelines.",
            "Provide a thorough evaluation of the safety module, including failure rates under different conditions, and implement a clear safety standard (e.g., ISO 13849).",
            "Consider fairness implications by testing grasp success across objects used by diverse user groups (e.g., culturally specific utensils) and discuss how such biases might be mitigated.",
        ],
    ),
]

# =============================================================================
# Combined dataset
# =============================================================================

ALL_DIMENSION_EXAMPLES: dict[str, list[Review]] = {
    "methodology": METHODOLOGY_EXAMPLES,
    "novelty": NOVELTY_EXAMPLES,
    "experiment": EXPERIMENT_EXAMPLES,
    "writing": WRITING_EXAMPLES,
    "related_work": RELATED_WORK_EXAMPLES,
    "reproducibility": REPRODUCIBILITY_EXAMPLES,
    "ethics": ETHICS_EXAMPLES,
}

# =============================================================================
# Utility functions
# =============================================================================


def get_examples_for_dimension(
    dim_id: str,
    min_score: float | None = None,
    max_score: float | None = None,
) -> list[Review]:
    """Get few-shot examples for a specific dimension, optionally filtered by score."""
    examples = ALL_DIMENSION_EXAMPLES.get(dim_id, [])
    if min_score is not None:
        examples = [e for e in examples if e.overall_score is not None and e.overall_score >= min_score]
    if max_score is not None:
        examples = [e for e in examples if e.overall_score is not None and e.overall_score <= max_score]
    return examples


def get_all_examples() -> list[Review]:
    """Get all few-shot examples across all dimensions."""
    all_examples: list[Review] = []
    for examples in ALL_DIMENSION_EXAMPLES.values():
        all_examples.extend(examples)
    return all_examples


def format_fewshot_block(examples: list[Review], max_chars: int = 1500) -> str:
    """Format a list of review examples as a few-shot prompt block."""
    if not examples:
        return ""

    parts: list[str] = []
    for i, ex in enumerate(examples[:3]):
        score_str = f"Score: {ex.overall_score:.0f}/10" if ex.overall_score is not None else ""
        block = f"Reference Example {i+1}: {score_str}\n"
        if ex.strengths:
            block += "Strengths:\n" + "\n".join(f"- {s}" for s in ex.strengths[:2]) + "\n"
        if ex.weaknesses:
            block += "Weaknesses:\n" + "\n".join(f"- {w}" for w in ex.weaknesses[:2]) + "\n"
        if ex.suggestions:
            block += "Suggestions:\n" + "\n".join(f"- {s}" for s in ex.suggestions[:1]) + "\n"
        if len(block) > 600:
            block = block[:600] + "...\n"
        parts.append(block.strip())

    result = "\n\n".join(parts)
    return result[:max_chars]


def get_fewshot_dataset() -> ReviewDataset:
    """Get all curated examples as a ReviewDataset."""
    return ReviewDataset(
        name="curated_fewshot",
        source="curated",
        reviews=get_all_examples(),
    )


if __name__ == "__main__":
    for dim_id, examples in ALL_DIMENSION_EXAMPLES.items():
        scores = [e.overall_score or 0 for e in examples]
        print(f"{dim_id}: {len(examples)} examples, scores {min(scores):.0f}-{max(scores):.0f}, avg {sum(scores)/len(scores):.1f}")
    total = sum(len(v) for v in ALL_DIMENSION_EXAMPLES.values())
    print(f"\nTotal: {total} examples")
