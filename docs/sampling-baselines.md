# Sampling baselines: methods, references, and adaptations

This guide describes the five registered literature baselines and their sources, with one distinct primary reference per rule. Methods are registered in `packages/tricycle-sampling`. This document records the literature basis; interfaces are defined by the [sampling package](../packages/tricycle-sampling/README.md), and experimental conditions by the [unified experiment guide](mae-intelligent-sampling.md).

## Inclusion criteria

The source registry is based on verified original research, not a rolling publication window or journal ranking. Evidence must locate the main text, a figure caption, or an author-provided official full text showing that the paper actually used or compared the method. Mentions in introductions or reference lists are insufficient.

Here, a static baseline means its acquisition rule is fixed in advance. LHS is a design sequence without label feedback. The other four methods use updated model predictions each round, making them active sampling with fixed rules. LHS initialization and UCB optimization do not exactly match overall MAE reduction; interpretations must state those adaptation boundaries.

Primary references establish actual use, not necessarily invention. Current implementations explicitly adapt acquisition principles to TriCycle. A strategy name does not import the paper's models, data augmentation, human screening, or complete experimental workflow.

## Five registered literature baselines

| Curve label | Canonical strategy ID | Distinct primary reference | Use in the paper |
| --- | --- | --- | --- |
| Predictive uncertainty | `predictive-uncertainty` | Zhong et al., *Nature Communications*, 2025, [10.1038/s41467-025-59812-0](https://doi.org/10.1038/s41467-025-59812-0) | Iterative active learning for reaction-feasibility classification |
| Latin hypercube | `latin-hypercube` | Slattery et al., *Science*, 2024, [10.1126/science.adj1817](https://doi.org/10.1126/science.adj1817) | Initial experimental design for photocatalytic flow optimization |
| Bayes-UCB | `ucb` | Wang et al., *Nature*, 2024, [10.1038/s41586-024-07021-y](https://doi.org/10.1038/s41586-024-07021-y) | Bandit optimization of reaction-condition generality |
| Distance × uncertainty | `distance-uncertainty-product` | Shrestha et al., *Nature Communications*, 2024, [10.1038/s41467-024-49011-8](https://doi.org/10.1038/s41467-024-49011-8) | Iterative A Score acquisition for materials prediction |
| Uncertainty + Kriging Believer | `uncertainty-kriging-believer` | Souza et al., *JACS*, 2025, [10.1021/jacs.5c02218](https://doi.org/10.1021/jacs.5c02218) | Maximum uncertainty with temporary within-batch refitting |

Random is not a new comparison curve. Epistemic and aleatoric uncertainty are not two additional primary baselines from the same Zhong et al. paper. Their implementations and historical results remain available, as does shared random initialization.

## 1. Predictive uncertainty

**Reference:** Haowen Zhong, Yilan Liu et al. **Towards global reaction feasibility and robustness prediction with high throughput data and bayesian deep learning**. *Nature Communications* **16**, 4522 (2025), May 15, 2025. [Journal text](https://www.nature.com/articles/s41467-025-59812-0); [PubMed and captions](https://pubmed.ncbi.nlm.nih.gov/40374636/).

**Evidence:** The main-text *Fine-grained Uncertainty Disentanglement* section, Methods uncertainty estimation/decomposition, and Fig. 4e–g. The paper compares predictive, epistemic, aleatoric, and random selection for acid–amine reaction-feasibility classification, updating models with added data and reporting F1-macro learning curves across splits.

The original method uses natural-log entropy of posterior binary-classification probabilities `p_i`:

```text
predictive = H(mean(p_i))
aleatoric  = mean(H(p_i))
epistemic  = predictive - aleatoric
```

**Why predictive alone represents this paper:** The text reports broadly similar predictive and epistemic performance, both ahead of aleatoric. Epistemic estimates are unstable below 500 samples, giving predictive an early advantage; beyond 1000 samples, epistemic better identifies model knowledge gaps. Random performs worst across the three splits. The paper therefore does not establish one method as universally best at every budget and split. Selecting predictive to represent the paper is a project judgment over the full learning process, not evidence that it must minimize TriCycle regression MAE.

**Current implementation:** Sum RxnResid `predictive_variance` across prior routes for each substrate pair; select in descending order, breaking ties by ascending ID. Regression variance differs from the paper's classification entropy, and route aggregation is also an adaptation. The general API still supports the three classification entropies and older uncertainty fields, while the unified comparison fixes regression variance semantics.

## 2. Latin hypercube

**Reference:** Aidan Slattery et al. **Automated self-optimization, intensification, and scale-up of photocatalysis in flow**. *Science* **383**(6681), eadj1817 (2024), January 26, 2024. [DOI](https://doi.org/10.1126/science.adj1817); [official institutional full text](https://pure.uva.nl/ws/files/187156411/Automated_self-optimization_intensification_and_scale-up_of_photocatalysis_in_flow.pdf).

**Evidence:** *Model-based optimization* and *Materials and methods / Platform operation* describe LHS initialization for each campaign, followed by model-driven optimization. This establishes actual use of LHS, not superiority of full-trajectory LHS for pool-based MAE learning.

**Method:** Stratify each continuous input dimension, then randomly pair strata across dimensions to form a space-covering design.

**Current implementation:** Use the shared six-dimensional reactant descriptors, fixed `[0,1]` bounds, acquisition seed, and total design size after the initial batch. Map design points in order to the nearest unselected discrete candidate. Across rounds, only advance the offset; do not reestimate bounds or redesign. Substrate IDs are not continuous coordinates. Discrete mapping may alter stratification, and arbitrary batch prefixes need not remain strict LHS designs. Extending initialization to a full sampling trajectory is a TriCycle adaptation.

## 3. Gaussian Bayes-UCB

**Reference:** Jason Y. Wang et al. **Identifying general reaction conditions by bandit optimization**. *Nature* **626**, 1025–1033 (2024), published online February 28, 2024. [Journal text](https://www.nature.com/articles/s41586-024-07021-y); [author-provided full text](https://doyle.chem.ucla.edu/wp-content/uploads/2024/02/s41586-024-07021-y.pdf).

**Evidence:** *Model design and development*, Fig. 2, and Supplementary Information Section 8. The paper uses Bayes-UCB to optimize reaction-condition generality, with beta priors for binary metrics and Gaussian priors for continuous metrics, and discusses UCB1-Tuned.

**Method:** Select arms by an upper posterior quantile of expected reward. In the Gaussian case, this is `mu + Phi_inverse(q)*std`. Its interpretation depends on the reward posterior; arbitrary model standard deviations cannot be called the paper's posterior.

**Current implementation:** One candidate is one arm; `q=0.95` is fixed, and each batch selects top-k once. The unified experiment negates the mean predicted energy across the same prior routes to define reward direction and uses RxnResid epistemic variance for a Gaussian approximation:

```text
score = -mean(mu_j) + Phi_inverse(0.95) * sqrt(sum(v_epi_j)) / n
```

This assumes route independence. Epistemic variance represents uncertainty in expected reward; aleatoric noise in total variance is not automatically included. The shared RxnResid workflow updates predictions after real feedback. Fixed quantiles, candidate-level arms, route aggregation, and low-energy preference are adaptations. Optimizing lower energy is not equivalent to reducing full-pool MAE.

Li et al.'s 2024 *Nature Chemistry* paper uses GP-UCB, random beta, and human selection in a different workflow. The current version binds Wang et al.'s Gaussian Bayes-UCB and does not conflate the two reproductions.

## 4. Distance × uncertainty (A Score)

**Reference:** Snehi Shrestha et al. **Machine intelligence accelerated design of conductive MXene aerogels with programmable properties**. *Nature Communications* **15**, 4685 (2024), June 1, 2024. [Journal text](https://www.nature.com/articles/s41467-024-49011-8).

**Evidence:** *Constructing a prediction model via active learning loops, data augmentation, and collaborative robots*, Eq. (1), Fig. 2, and Supplementary Note 7 / Fig. 13–14 / Table 6. The paper runs active-learning loops with ongoing model updates and compares prediction MAE against random and LHS.

**Method:** `A Score = L_hat × sigma_hat`. The text describes Euclidean distance between in-model and model-targeted points and calls the uncertainty term predictive variance.

**Current implementation:** Compute nearest Euclidean distance from a candidate to the revealed valid-label set in the shared six-dimensional descriptor space, then multiply by summed total predictive route variance. The reference set remains fixed within a batch; no extra normalization is applied. The existing weighted-sum `uncertainty-diversity` and density-product `density-weighted-uncertainty` rules are different methods.

Nearest-reference distance, fixed representation scaling, and regression variance are explicit adaptations. The SI's original distance aggregation, normalization, and multi-output variance have not all been reproduced and checked individually. The original model, augmentation, and robotics workflow are not imported; its overall performance cannot be attributed to this scoring function.

## 5. Uncertainty + Kriging Believer

**Reference:** Lucas W. Souza et al. **Applying Active Learning toward Building a Generalizable Model for Ni-Photoredox Cross-Electrophile Coupling of Aryl and Alkyl Bromides**. *Journal of the American Chemical Society* **147**(22), 18747–18759 (2025), published online May 22, 2025. [DOI](https://doi.org/10.1021/jacs.5c02218); [full-text archive](https://pmc.ncbi.nlm.nih.gov/articles/PMC12517759/); [author-provided full text](https://doyle.chem.ucla.edu/wp-content/uploads/2025/05/souza-et-al-2025-applying-active-learning-toward-building-a-generalizable-model-for-ni-photoredox-cross-electrophile.pdf).

**Evidence:** *Batch Selection* and Fig. 4–6. The paper uses standard deviations across RF tree predictions as uncertainty, aggregates across eight aryl cores, and selects 12 alkyl building blocks sequentially. After each selection, current predictions become temporary labels for refitting before selecting the next item. Real experimental results then update the formal model.

**Method:** Preserve sequential within-batch updates so later scores reflect predictions temporarily treated as true. One static uncertainty top-k operation is not KB.

**Current implementation:** Selection operates at substrate-pair level. The unified experiment uses RxnResid marginal means and epistemic standard deviations, adds structural RBF correlation with fixed length scale 1, and applies sequential Gaussian conditioning. Correlation is an explicit modeling assumption, not a native RxnResid joint posterior. Temporary labels exist only within the selection session; they neither enter training labels nor add main-model training tasks.

The general sampling API also provides a full Gaussian covariance adapter and local-refit protocol. The default implementation does not reproduce the original RF tree count, cross-core aggregation, or refit recipe. The paper discusses correlation bias between absolute uncertainty and high predicted yield; this mechanism also does not guarantee lower full-pool MAE.

## Supplementary sources and alternatives

The entries below preserve located evidence of use. They are neither part of the final five-method comparison nor newly registered literature baselines. Additional articles in one family do not add curves.

| Method or use | Reference and DOI | Evidence and reason retained |
| --- | --- | --- |
| Batch A Score variant | Haochen Yang et al., **Predictive design of stretchable electrodes with strain-insensitive performance via robotics- and machine learning-integrated workflow**, *Nature Communications* **17**, 1778 (2026), [DOI](https://doi.org/10.1038/s41467-026-68484-3) | Eq. (2), SI Notes 7–8 combine sampled distance, candidate distance, and committee uncertainty; same family, full batch formula still requires verification |
| Heterogeneous QBC | Kangming Li et al., **Exploiting redundancy in large materials datasets for efficient machine learning with less data**, *Nature Communications* **14**, 7283 (2023), [DOI](https://doi.org/10.1038/s41467-023-42992-y) | *Uncertainty-based active learning* / Fig. 6, round-by-round RMSE using RF–XGB disagreement; needs a real committee and separately recorded acquisition-model cost; pruning by known residuals is not unlabeled acquisition |
| Target-directed AF_AL | Jules Schleinitz et al., **Designing Target-specific Data Sets for Regioselectivity Predictions on Complex Substrates**, *JACS* **147**(9), 7476–7484 (2025), [DOI](https://doi.org/10.1021/jacs.4c15902) | Section 2.2, Fig. 3–4, SI V.3: site similarity × predicted reactivity × uncertainty; needs a separately defined target-directed task |
| KS initialization and GP-UCB | Xiaobo Li et al., **Sequential closed-loop Bayesian optimization as a guide for organic molecular metallophotocatalyst formulation discovery**, *Nature Chemistry* **16**, 1286–1294 (2024), [DOI](https://doi.org/10.1038/s41557-024-01546-5) | *Selecting synthetic targets using BO*: KS selects six initial points, followed by UCB; KS is not included as an ongoing AL baseline |
| CVT/LHS initialization comparison | Jose Antonio Garrido Torres et al., **A Multi-Objective Active Learning Platform and Web App for Reaction Optimization**, *JACS* **144**(43), 19999–20007 (2022), [DOI](https://doi.org/10.1021/jacs.2c08592) | Fig. 4 / p. 20003 compares CVT, LHS, and random initialization followed by BO; it does not establish full-trajectory CVT learning |
| D-optimal comparison | Nathan J. Szymanski et al., **Autonomous and dynamic precursor selection for solid-state materials synthesis**, *Nature Communications* **14**, 6956 (2023), [DOI](https://doi.org/10.1038/s41467-023-42329-9) | Fig. 3a, SI Note 1, independent comparison under linear-design assumptions; incremental design, singularity, and regularization need separate fixed definitions |
| Sobol initialization and multi-objective BO | J. W. Sin et al., **Highly parallel optimisation of chemical reactions through automation and machine intelligence**, *Nature Communications* **16**, 6464 (2025), [DOI](https://doi.org/10.1038/s41467-025-61803-0) | Fig. 1b, *Benchmarking and evaluating*: Sobol initialization followed by TS-HVI, q-NEHVI, and q-NParEgo; requires joint posteriors and multi-objective definitions |
| Dynamic-trajectory QBC | **Exploring the frontiers of condensed-phase chemistry with a general reactive machine learning potential**, *Nature Chemistry* (2024), [DOI](https://doi.org/10.1038/s41557-023-01427-3) | Chemical evidence from multiround nanoreactor AL; dynamic trajectories and energy/force thresholds differ from a fixed substrate-pair pool |

Shrestha et al. also provides an LHS comparison, retained as supplementary evidence. Slattery et al. remains the LHS primary reference so the five principal curves correspond to five distinct papers.

## Citation requirements for implementations and results

[protocols.py](../packages/tricycle-sampling/src/tricycle_sampling/protocols.py) is the single code definition for the five source protocols. [TOML examples](../configs/sampling-baselines/) identify each DOI. Plans and results automatically record the paper, evidence, rule, adaptation, protocol and implementation versions, and hashes. Experiment archives must also retain parameters, input data, source, checkpoint chains, and per-round selections.

Unified comparisons freeze candidate pools, identities, initial sets, main training, evaluation sets, seeds, batch sizes, and core-hour accounting. Record representations, aggregation, distance/kernel choices, tie-breaking, and within-batch mechanisms under the current version. Future labels, residuals, calculation outcomes, and costs do not enter acquisition. Changes to acquisition definitions or main training require a new experiment/protocol version; historical source bindings must not be rewritten.

The recommended comparison order is the five methods in the main table. Effectiveness is established by matched-budget experiments. Interpretation must retain LHS's full-trajectory design adaptation, UCB's energy-optimization preference, A Score's distance-aggregation differences, and KB's Gaussian/RBF assumptions.
