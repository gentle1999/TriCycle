# 采样基线：方法、文章与适配

本文说明已注册的五种文献基线及其来源，每种规则绑定一篇主引用文章。方法已经在 `packages/tricycle-sampling` 注册；本文记录文献依据，接口以[采样包说明](../packages/tricycle-sampling/README.md)为准，实验条件以[统一实验指南](mae-intelligent-sampling.md)为准。

## 收录原则

来源登记以已核对的原始研究为依据，不以滚动时间窗口或期刊排名定义接口。来源要求能定位正文、图注或作者公开的正式全文，确认文章实际执行或比较该方法；引言提及和参考文献列举不足以作为使用证据。

这里的“静态基线”指采集规则预先固定。LHS 是无标签反馈的设计序列；其余四项使用每轮更新的模型预测，属于固定规则主动采样。LHS 的初始化用途和 UCB 的优化目标与整体 MAE 不完全一致，结果解释时应说明适配范围。

主引用提供实际使用的依据，不一概代表算法首创。当前实现是文章采集原则在 TriCycle 上的明确适配；文章中的模型、数据增强、人工筛选和整套实验流程没有随策略名称一起迁移。

## 已注册的五条文献基线

| 曲线标签 | 规范策略 ID | 唯一主引用 | 原文用途 |
| --- | --- | --- | --- |
| Predictive uncertainty | `predictive-uncertainty` | Zhong et al., *Nature Communications*, 2025，[10.1038/s41467-025-59812-0](https://doi.org/10.1038/s41467-025-59812-0) | 反应可行性分类的迭代主动学习 |
| Latin hypercube | `latin-hypercube` | Slattery et al., *Science*, 2024，[10.1126/science.adj1817](https://doi.org/10.1126/science.adj1817) | 光催化连续流优化的初始实验设计 |
| Bayes-UCB | `ucb` | Wang et al., *Nature*, 2024，[10.1038/s41586-024-07021-y](https://doi.org/10.1038/s41586-024-07021-y) | bandit 反应条件通用性优化 |
| Distance × uncertainty | `distance-uncertainty-product` | Shrestha et al., *Nature Communications*, 2024，[10.1038/s41467-024-49011-8](https://doi.org/10.1038/s41467-024-49011-8) | A Score 驱动材料预测模型的迭代采集 |
| Uncertainty + Kriging Believer | `uncertainty-kriging-believer` | Souza et al., *JACS*, 2025，[10.1021/jacs.5c02218](https://doi.org/10.1021/jacs.5c02218) | 最大不确定性与批内临时重拟合 |

random 不进入新比较曲线；epistemic 和 aleatoric 不再作为 Zhong et al. 同一篇文章的另外两条主基线。三者的代码和历史结果保留，共享随机初始集仍保留。

## 1. Predictive uncertainty

**文章：** Haowen Zhong, Yilan Liu et al. **Towards global reaction feasibility and robustness prediction with high throughput data and bayesian deep learning**. *Nature Communications* **16**, 4522 (2025)，2025-05-15。[期刊正文](https://www.nature.com/articles/s41467-025-59812-0)；[PubMed 与图注](https://pubmed.ncbi.nlm.nih.gov/40374636/)。

**证据：** 正文 *Fine-grained Uncertainty Disentanglement*、Methods 的不确定性估计与分解、Fig. 4e–g。文章在酸–胺反应可行性分类中比较 predictive、epistemic、aleatoric 选样和 random，按新增数据更新模型，报告不同划分下的 F1-macro 学习曲线。

原文用后验二分类概率 `p_i` 的自然对数熵：

```text
predictive = H(mean(p_i))
aleatoric  = mean(H(p_i))
epistemic  = predictive - aleatoric
```

**为何只保留 predictive：** 正文认为 predictive 与 epistemic 整体接近，均优于 aleatoric；少于 500 个样本时 epistemic 估计不稳定，predictive 有早期优势；超过 1000 个样本后 epistemic 更有助于定位模型知识缺口。random 在三个划分中均最差。因此文章不支持某一种在全部预算、划分中绝对最好。选择 predictive 代表该文，是结合完整学习过程的本项目判断，不能推断它在 TriCycle 回归 MAE 中必然最优。

**当前实现：** 对底物对先验路线的 RxnResid `predictive_variance` 求和，降序取样，同分按 ID 升序。回归方差与原文分类熵不同；路线聚合也是适配。通用 API 仍支持三种分类熵及旧不确定性字段，但新统一比较固定使用回归方差口径。

## 2. Latin hypercube

**文章：** Aidan Slattery et al. **Automated self-optimization, intensification, and scale-up of photocatalysis in flow**. *Science* **383**(6681), eadj1817 (2024)，2024-01-26。[DOI](https://doi.org/10.1126/science.adj1817)；[作者机构正式全文](https://pure.uva.nl/ws/files/187156411/Automated_self-optimization_intensification_and_scale-up_of_photocatalysis_in_flow.pdf)。

**证据：** *Model-based optimization*、*Materials and methods / Platform operation* 中每次 campaign 的 LHS 初始化，随后进入模型驱动优化。该文背书的是 LHS 的实际使用，未证明全程 LHS 在 pool-based MAE 学习中更优。

**方法：** 每个连续输入维度分层取点，再随机配对不同维度的层，形成空间覆盖设计。

**当前实现：** 使用相同六维反应物描述符、固定 `[0,1]` 边界、采集 seed 和初始批次之后的总设计规模；按设计点顺序映射到最近未选离散候选。跨轮仅推进 offset，不重估边界或重新设计。底物 ID 不当作连续坐标。离散映射可能改变分层，任意批次前缀也不保证仍是严格 LHS；从初始化扩展到完整采样轨迹是本项目适配。

## 3. Gaussian Bayes-UCB

**文章：** Jason Y. Wang et al. **Identifying general reaction conditions by bandit optimization**. *Nature* **626**, 1025–1033 (2024)，2024-02-28 在线发表。[期刊正文](https://www.nature.com/articles/s41586-024-07021-y)；[作者正式全文](https://doyle.chem.ucla.edu/wp-content/uploads/2024/02/s41586-024-07021-y.pdf)。

**证据：** *Model design and development*、Fig. 2、Supplementary Information Section 8。文章实际用 Bayes-UCB 优化反应条件的通用性，二值指标采用 beta prior，连续指标采用 Gaussian prior，并讨论 UCB1-Tuned。

**方法：** 按预期收益后验的上分位数选择 arm。Gaussian 情形可写为 `mu + Phi_inverse(q)*std`；这一公式的解释取决于收益后验的语义，不能把任何模型标准差直接称为原文后验。

**当前实现：** 一候选一 arm，固定 `q=0.95`，批次一次取 top-k。统一实验将同一组先验路线的预测能量均值取负作为收益方向，使用 RxnResid 认知方差构造 Gaussian 近似：

```text
score = -mean(mu_j) + Phi_inverse(0.95) * sqrt(sum(v_epi_j)) / n
```

此处假定路线独立；认知方差用于期望收益的不确定性，总体方差中的 aleatoric 噪声未自动加入。真实反馈后的预测更新由共同 RxnResid 流程完成。固定分位数、候选级 arms、路线聚合和低能量偏好均为适配；优化较低能量并不等同于优化全池 MAE。

Li et al. 的 *Nature Chemistry* 2024 文章使用 GP-UCB、随机 beta 及人工选择，属于另一流程；当前版本绑定 Wang et al. 的 Gaussian Bayes-UCB，不把二者混称为同一复现。

## 4. Distance × uncertainty（A Score）

**文章：** Snehi Shrestha et al. **Machine intelligence accelerated design of conductive MXene aerogels with programmable properties**. *Nature Communications* **15**, 4685 (2024)，2024-06-01。[期刊正文](https://www.nature.com/articles/s41467-024-49011-8)。

**证据：** *Constructing a prediction model via active learning loops, data augmentation, and collaborative robots*、Eq. (1)、Fig. 2、Supplementary Note 7 / Fig. 13–14 / Table 6。文章进行持续模型更新的 AL loops，并与 random、LHS 比较预测 MAE。

**方法：** `A Score = L_hat × sigma_hat`；正文将距离描述为 in-model 与 model-targeted 点的 Euclidean 距离，将不确定性项称为预测方差。

**当前实现：** 在共同六维描述符上计算候选到已揭示有效标签集合的最近 Euclidean 距离，再乘路线总体预测方差之和。同批参考集合固定，不额外归一化。既有 `uncertainty-diversity` 的加权和及 `density-weighted-uncertainty` 的密度乘积不是这条规则。

最近参考距离、固定表征缩放和回归方差为明确适配。SI 中原始距离聚合、归一化与多输出方差仍未逐项完成复现核对；原文的模型、数据增强和机器人流程未迁移，因此其整体表现不能归因于当前评分函数。

## 5. Uncertainty + Kriging Believer

**文章：** Lucas W. Souza et al. **Applying Active Learning toward Building a Generalizable Model for Ni-Photoredox Cross-Electrophile Coupling of Aryl and Alkyl Bromides**. *Journal of the American Chemical Society* **147**(22), 18747–18759 (2025)，2025-05-22 在线发表。[DOI](https://doi.org/10.1021/jacs.5c02218)；[全文存档](https://pmc.ncbi.nlm.nih.gov/articles/PMC12517759/)；[作者正式全文](https://doyle.chem.ucla.edu/wp-content/uploads/2025/05/souza-et-al-2025-applying-active-learning-toward-building-a-generalizable-model-for-ni-photoredox-cross-electrophile.pdf)。

**证据：** *Batch Selection*、Fig. 4–6。文章用 RF 树预测标准差表示不确定性，对 8 个 aryl cores 汇总后逐个选取 12 个 alkyl building blocks。每选一个，先用当前预测作临时标签重拟合，再选同批下一项；真实实验后更新正式模型。

**方法：** 保留批内顺序更新，使后续评分反映已经暂时“相信”的预测；不能用一次静态不确定性 top-k 替代 KB。

**当前实现：** 改为底物对级选择；统一实验使用 RxnResid 边际均值与认知标准差，加固定长度尺度为 1 的结构 RBF 相关性，逐步 Gaussian 条件化。相关性是显式建模假设，不是 RxnResid 原生联合后验。临时标签仅存在于本次选择会话，不写回训练标签，也不增加主模型训练任务。

通用采样 API 另提供完整 Gaussian 协方差 adapter 和本地 refit protocol。当前默认实现未复现原文 RF 树数、跨 cores 聚合或重拟合配方。原文讨论绝对不确定性与高预测收率的相关偏差；该机制也不保证降低全池 MAE。

## 补充来源与备选方法

下列条目保留已找到的使用证据。它们未加入最终五条比较，也未注册为新的文献基线；同一家族的额外文章不增加曲线数。

| 方法或用途 | 文章与 DOI | 证据及保留原因 |
| --- | --- | --- |
| A Score 批量变体 | Haochen Yang et al., **Predictive design of stretchable electrodes with strain-insensitive performance via robotics- and machine learning-integrated workflow**, *Nature Communications* **17**, 1778 (2026)，[DOI](https://doi.org/10.1038/s41467-026-68484-3) | Eq. (2)、SI Notes 7–8，结合已采距离、候选距离和委员会不确定性；同一家族，完整批次公式待核对 |
| 异构 QBC | Kangming Li et al., **Exploiting redundancy in large materials datasets for efficient machine learning with less data**, *Nature Communications* **14**, 7283 (2023)，[DOI](https://doi.org/10.1038/s41467-023-42992-y) | *Uncertainty-based active learning* / Fig. 6，RF–XGB 分歧的逐轮 RMSE；需真实委员会及单列采集模型开销，已知残差 pruning 不当作无标签采集 |
| 目标定向 AF_AL | Jules Schleinitz et al., **Designing Target-specific Data Sets for Regioselectivity Predictions on Complex Substrates**, *JACS* **147**(9), 7476–7484 (2025)，[DOI](https://doi.org/10.1021/jacs.4c15902) | Section 2.2、Fig. 3–4、SI V.3，位点相似性 × 预测反应性 × 不确定性；需另外定义目标定向任务 |
| KS 初始化及 GP-UCB | Xiaobo Li et al., **Sequential closed-loop Bayesian optimization as a guide for organic molecular metallophotocatalyst formulation discovery**, *Nature Chemistry* **16**, 1286–1294 (2024)，[DOI](https://doi.org/10.1038/s41557-024-01546-5) | *Selecting synthetic targets using BO*：KS 选初始六点，闭环主体为 UCB；KS 未作为持续 AL 主线纳入 |
| CVT、LHS 初始化对照 | Jose Antonio Garrido Torres et al., **A Multi-Objective Active Learning Platform and Web App for Reaction Optimization**, *JACS* **144**(43), 19999–20007 (2022)，[DOI](https://doi.org/10.1021/jacs.2c08592) | Fig. 4 / p. 20003，比较 CVT、LHS、random 初始化，随后 BO；不证明全程 CVT 学习 |
| D-optimal 对照 | Nathan J. Szymanski et al., **Autonomous and dynamic precursor selection for solid-state materials synthesis**, *Nature Communications* **14**, 6956 (2023)，[DOI](https://doi.org/10.1038/s41467-023-42329-9) | Fig. 3a、SI Note 1，线性设计假设下的独立比较；增量设计、奇异性与正则化需另行冻结 |
| Sobol 初始化及多目标 BO | J. W. Sin et al., **Highly parallel optimisation of chemical reactions through automation and machine intelligence**, *Nature Communications* **16**, 6464 (2025)，[DOI](https://doi.org/10.1038/s41467-025-61803-0) | Fig. 1b、*Benchmarking and evaluating*；Sobol 初始化，后续 TS-HVI、q-NEHVI、q-NParEgo，需联合后验与多目标定义 |
| 动态轨迹 QBC | **Exploring the frontiers of condensed-phase chemistry with a general reactive machine learning potential**, *Nature Chemistry* (2024)，[DOI](https://doi.org/10.1038/s41557-023-01427-3) | nanoreactor 多轮 AL 的化学使用证据；动态轨迹及能量/力阈值与固定底物对池不同 |

Shrestha et al. 也给出 LHS 对照，作为补充证据保留；LHS 主引用仍指定 Slattery et al.，保持五条主线对应五篇不同文章。

## 实现与结果中的引用要求

[protocols.py](../packages/tricycle-sampling/src/tricycle_sampling/protocols.py)是五条来源协议的唯一代码定义。[TOML 示例](../configs/sampling-baselines/)逐项注明 DOI；计划和结果自动保存文章、证据、规则、适配、协议与实现版本及 hash。实验归档应同时保存参数、输入数据、源码、checkpoint 链和逐轮选择。

统一比较冻结候选池、身份、初始集、主训练、评价集、seed、批量与核时口径；表征、聚合、距离/核、同分和批内机制按当前版本记录。未来真实标签、残差、计算成功与费用不参与 acquisition。变更采集定义或主训练需新实验/协议版本，不能改写历史结果的来源绑定。

推荐比较顺序即主表五项；是否有效由匹配预算实验决定。结果解释须保留 LHS 的全程设计适配、UCB 的能量优化偏好、A Score 的距离聚合差异，以及 KB 的 Gaussian/RBF 假设。
