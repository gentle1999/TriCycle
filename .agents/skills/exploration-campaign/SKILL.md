---
name: exploration-campaign
description: "Supervise a persistent multi-round Bohr exploration, hand off analysis to an agent, and recover from interruptions."
---

# 持续探索与子 agent 交接

## 统一采样并行实验

用户选择新的匹配采样比较时，使用
`python -m tricycle_sampling.replay.suite prepare --config configs/dpa-unified-sampling-suite.toml`。
prepare 仅生成本地产物，不读凭据或访问网络；核验 suite/preflight 的共同数据、初始集合、
辅助面板、训练与评价 hash。得到本实验执行授权后才以
`watch --run-dir <suite> --live --allow-submit` 推进。
`service-file --run-dir <suite> --allow-submit` 仅生成独立 launchd 文件，不自动安装。

比较方法读取套件的 `sampling_strategies`，基线由共同数值 adapter 执行，智能策略按所选协议制定规则。
从 `suite.json`、`preflight.json` 和各 `experiment.json` 核对初始集合、种子数、轮数、批量、epochs、
评价与并发上限。准备入口只接受当前实现支持的实验形状；这些限制属于所选套件，不作为其他 campaign 的默认预算。

协议 `autonomous-core-hours-mae-v1` 比较给定累计代理核时下的 GT MAE，评价只取预算内
最新已完成 checkpoint，不外推，不按 GT 优劣停止或切换。候选费用只用已揭示历史校准，
未知真实成本不用于采集；核时节点不是硬费用保证。配置指定的固定代理辅助面板反馈不等于主模型全池 GT 误差。种子启动门槛读取 `rollout.pilot_evaluations`；不按评价误差淘汰策略。

全部任务复用一个持久 Bohr 任务组；故障按 seed 隔离，保留 inflight、已接受决策和回执，
不得盲目重提。源码、配置、数据或 worker hash 不匹配时拒绝接续，旧实验不刷新 hash 迁移。
可选智能策略由 `replay.suite.AGENT_VARIANTS` 定义，比较集合以当前配置为准；任务数、审计
与绘图布局须匹配实际配置。seed-watch 按所选策略监督，公式恢复按各工具的协议范围执行。
智能 DSL 审计支持 cost-aware 参照与表达式，不能替代五条确定性基线的完整数值验收；
平台审计单页上限也须检查。详见
[统一实验与恢复指南](../../../docs/mae-intelligent-sampling.md)、
[方法与文章](../../../docs/sampling-baselines.md)和
[包接口](../../../packages/tricycle-sampling/README.md)。

本能力属于应用编排层，复用现有领域插件。输入为
`configs/example-exploration-campaign.toml`，状态为独立目录中的 `campaign.toml`。
无需保持聊天记忆；启动或接手时先读状态、当前任务及历史产物。

## 启动与监督

```bash
uv run --package tricycle-agent tricycle-agent campaign start campaign-request.toml
uv run --package tricycle-agent tricycle-agent campaign status --run-dir /absolute/campaign
uv run --package tricycle-agent tricycle-agent campaign tick --run-dir /absolute/campaign
uv run --package tricycle-agent tricycle-agent campaign watch --run-dir /absolute/campaign --live
```

`start` 只注册一个已经提交的批次，不连接服务。`tick/watch` 默认只读 dry-run，不加载凭据、
调用 agent 或推进阶段。`--live` 才查询、下载和执行配置的 agent 命令。
得到用户对本探索后续批次的明确授权后，使用 `--live --allow-submit`；授权限定在请求的
`max_new_batches` 和 `max_selected_candidates` 内。这两个限制不包含初始批次，也不代表费用额度。
请求中的 `initial_selected_ids` 应包含已有已采集合；完整结构、标签和模型信息通过
`context_paths` 中的不可变文件提供。不要修改已启动的请求来扩大预算。

只有一个进程可以推进同一 campaign，进程退出会释放锁。`watch` 定时检查，阶段转换后立即
继续，不为等待计算调用模型。`--max-ticks N` 可限制本次监督时长；状态由文件保存。
生产运行应让系统服务或现有监督进程重启 `watch`；TriCycle 不自动安装系统服务。

## 子 agent 执行协议

`agent_command = []` 时，监督者读取 `state.agent_task_path` 指向的任务，交给一个子 agent
执行；同一任务只交给一个执行者。子 agent 完成后原子写入任务指定的 `response_path`。
每次重新接手先检查已有 response 和 selection，不能重新生成已完成的决策。

也可注入 `agent_command = ["/absolute/path/to/agent-wrapper", "argument"]`。程序追加
`task.toml` 与 `response.toml` 的绝对路径，以 argv 执行，不经过 shell。wrapper 必须等待
agent 完成，退出码为零，并写响应。任何模型 SDK/账户接入都由 wrapper 明确配置；仓库不假设
某个平台的子 agent API。日志与 PID 保存在该阶段目录。命令启动后若中断，系统不会盲目重复启动。

### analyzing 阶段

读取 task 中的 batch、evidence、context 和之前的 rounds。必须遵循
[agent-authored-acquisition](../agent-authored-acquisition/SKILL.md)：检查全部 worker 下载，
整理有效标签和失败记录，完成必要的模型更新/推理与逐轮 RDKit 分析，检查分组误差，生成完整
`exploration.round` TOML 请求。独立模型任务只在已有授权范围内执行，并保存它们的 run 与产物。
不能把 planned checkpoint 当作已经训练完成的模型；不能提交新的 QM 候选批次。

请求的 `exploration_id` / `round_index` 必须等于 task；`budget` 不超过剩余候选额度。
`metadata.agent_acquisition.evidence` 中的 calculation_status/downloaded_results 必须与
`evidence.toml` 完全一致。完整候选池和标签历史来自上下文及以前各轮，不能只处理 top-N。

响应格式：

```toml
task_sha256 = "<task.toml 的 SHA-256>"
request_path = "/absolute/persistent/exploration-round-request.toml"
request_sha256 = "<请求文件的 SHA-256>"
```

若候选耗尽或有证据支持的精度目标已达到，可返回 `action = "stop"` 与非空 `reason`，
同时保留 task_sha256。分析和支持结论的原始报告必须保存在阶段目录，不依赖聊天文本。

### preparing 阶段

读取并核对 task 的 `selection_path` / `selection_sha256`。使用现有 prepare 能力，
将**全部选中候选**转换为一个新的独立 QM 批次；保留候选到实际计算输入的映射证据。
不得提交。准备时只生成非空 worker；当前智能采集证据契约不接受 SKIPPED_EMPTY。

```toml
task_sha256 = "<task.toml 的 SHA-256>"
batch_run_dir = "/absolute/path/to/new-prepared-batch"
plan_sha256 = "<submit-plan.tsv 的 SHA-256>"
selection_sha256 = "<task 中的 selection_sha256>"
candidate_ids = ["<选中候选 ID>"]
```

调度器核对 ID、选样 hash 和准备文件，冻结 worker 输入的 hash 后再提交。
候选到 Gaussian/DPA 等实际任务的化学语义映射由准备者负责，不能靠声明 ID 替代核对。

## 暂停、恢复与失败

```bash
uv run --package tricycle-agent tricycle-agent campaign pause --run-dir /absolute/campaign
uv run --package tricycle-agent tricycle-agent campaign resume --run-dir /absolute/campaign
uv run --package tricycle-agent tricycle-agent campaign watch --run-dir /absolute/campaign --live --allow-submit
```

pause 在当前操作返回后生效，不终止远程任务。resume 清除暂停/错误计数，不直接执行外部操作。
状态查询和下载错误按退避重试，达到 max_failures 后 blocked。修复原因后 resume。
Bohr 原始下载报告位于 `downloads/download-results.tsv`；编排器将其原子复制到批次根目录，
供采集证据校验，并在本轮 `download-source.toml` 保存原始路径/hash。
子 agent 响应必须写临时文件后 rename，避免读到半个 TOML。

若 agent 命令中断，先根据 `agent-process.toml`、日志和已生成产物确认进程状态，恢复 response。
确需重新启动时，先确认旧进程已停止，再显式删除该阶段的 `agent-invocation.toml`，然后 resume。
不要删除已接受的响应或已冻结的 selection 请求来重做决定。

若提交异常或提交时进程退出，`state.inflight` 会保留。先核对 Bohr 和各 worker 的 `submit.out`，
补全真实 `submit-results.tsv`；只有所有计划 worker 都有提交回执后才会自动转入监控。
不确定是否已提交时禁止清空 inflight 或直接重复提交。部分批次确需补交时，必须先完成远程核对，
再通过原有 Bohr 恢复流程处理。自动监督不会猜测缺失的远程 ID。

预算达到后仍监控并下载最后一个已提交批次，再标记 complete；不会再调用采集决策。
`history/*.toml`、各轮任务/响应/证据、选样 trace 与结果用于审计。

## 历史标签回放：新的 SAR 主动学习轨迹

用户明确选择历史标签回放时，新实验使用 `python -m tricycle_sampling.replay.suite prepare`
和 `configs/dpa-unified-sampling-suite.toml`；已有单实验通过 `python -m tricycle_sampling.replay.campaign`
读取其 run 目录内的配置恢复。操作和恢复见
[`docs/intelligent-replay.md`](../../../docs/intelligent-replay.md)。历史实验的具体操作记录属于本地研究归档，不是新 campaign 的默认流程。不要把它接入上述 QM preparing 阶段。

该入口按已核验的基线初始集合创建新轨迹，从基线初始集合**重新训练**，不会复用基线 checkpoint 或后续选样。
主模型和独立验证模型使用请求指定的 epochs。历史标签只按选中 pair
逐轮揭示；最新 ground truth 专用于最终评价。每轮完成 SAR 下载及 RDKit 分析后调用 agent
编写采集函数，通过包内 `tricycle_sampling.replay.selection.select_round` 确定性选出下一批并保存审计记录，然后训练本轨迹下一轮模型。通用 QM 探索仍使用 `exploration.round`；回放运行不反向依赖 exploration 或 tools。
默认 dry-run 不连接服务；真实启动仍需 --live --allow-submit，并限制在准备记录中的种子和轮次预算内。

历史回放实验的全部 Bohr 任务必须复用实验根目录 `job-group.json` 中的 CLI 任务组 ID，
包括五条轨迹和后续追加的轮次。首次提交前在共享文件锁内创建一次；不得逐 seed/逐轮另建组。
已提交任务如需取消重提，应取得明确授权并归档旧回执，不得仅修改本地 group 字段冒充已迁移。

### 决策故障隔离与自动修正

历史回放的各 seed 独立运行决策进程，不共享串行模型执行锁。模型与推理强度读取 `decision_agent` 配置，受 `agent_timeout_seconds` 限制。输出格式、任务 hash、采集公式或数值校验失败，以及模型进程失败或超时，会保存 rejected-attempts 并把具体错误反馈给模型；尝试次数读取 `max_decision_attempts`，耗尽后只阻塞该 seed。采集公式通过完整候选池预校验后才接受，不改写已完成的选择。远程提交状态不明确仍需核对回执，禁止盲目重提。

历史回放 Bohr 提交默认 max_reschedule_times = 2，可在 [bohr] 中设置非负整数。adapter 将它传给 CLI 的 --max_reschedule_times；每轮 job-spec.json 保留实际值。节点被回收或物理机异常时由平台最多重新调度两次，按整轮任务重新执行，不宣称支持本轮中途 checkpoint 续算。该策略不重试提交 API（submit_retry_mode 仍为 none），也不修改已提交任务；缺省值以 adapter 的当前接口为准，配置只影响后续提交。

### 回放决策交付与恢复

控制器生成当前 `decision-brief.json` 和 `response-schema.json`，模型审阅材料并返回公式与分析。
材料、响应字段、任务绑定和调用边界统一见
[采集技能的回放决策说明](../agent-authored-acquisition/SKILL.md#回放决策材料与响应)。
监督器核对完整原始证据、响应绑定与全池校验结果，再推进下一轮；保留已接受的决策和选择。
`decision_protocol` 的未知字段或矛盾目标值会被拒绝，恢复时按记录中的协议核对，不静默关闭反馈或复核。

模型认证失败（401、失效 API key 或过期 token）单独报告为认证问题，不进入公式纠错重试。恢复前检查失败调用启动时间与本地登录更新时刻；保留全部失败产物，只恢复尚未完成选择的决策阶段。不要重新训练已完成轮次或重提交已有 Bohr 任务。

余额告警由独立的 tools.balance_alert 提供，不消耗模型额度、不改变实验预算。配置实际余额 adapter、阈值与 SMTP 收件人后才可启用；详情见 docs/balance-alert.md。认证失败与余额耗尽分别处理，不能仅凭 401 判断余额。

### 单空间对照套件

`python -m tricycle_sampling.replay.suite prepare --config configs/dpa-unified-sampling-suite.toml`
在本地准备五条文献基线与 cost-aware 智能策略的统一套件。`suite.json` 记录根目录、配置 hash 和初始集合，
`preflight.json` 记录全部首轮输入准备与初始集合一致性。旧的硬编码 DPA 一次性准备脚本已移除。
用户授权启动后，使用包内 suite 的 `watch --live --allow-submit` 或 `service-file` 入口。

`sampling_strategy` 的确定性选择范围读取 `replay.baselines.STRATEGIES`，只包含当前文献基线；新准备会记录当前 `sampling_experiment` schema，缺失或不支持的旧 schema 不执行回退算法。旧记录保留，重新计算须准备新 run。通用 `candidate.select` 接口的其他策略不受影响。这些基线由程序确定性执行，
不调用模型，也不标记为 agent-authored。完整排名与选择请求分别保存在
baseline-ranking.json 和 baseline-selection-request.json；实现 hash 在准备时冻结。

套件配置 `bohr.shared_group_dir` 时，全部实验通过共享文件锁和持久回执创建、复用唯一任务组。
`rollout.pilot_evaluations=3` 使 seed01–04 等待本策略 seed00 的第三轮评价文件，再自动推进。
不得因某策略误差较高而跳过其余种子，或清除不确定提交标记重复提交。

用户明确要求各随机种子尽量并行时，可将 rollout.pilot_evaluations 设置为 0，
五条轨迹独立推进。已启动实验调整调度时，先暂停本地监督并等待各seed操作释放锁，
归档原配置和hash，再记录此次用户授权并更新配置hash；保留已有回执和轨迹状态。
此项只改变启动顺序，不扩大seed数量、轮数或采样预算；同一轨迹的轮次仍依次执行。

追加已授权的静态策略实验时，为每个空间/策略新建独立轨迹，不修改已启动实验的冻结配置。
rits-zero-shot-da 与 autode 的 aleatoric-uncertainty、predictive-uncertainty 沿用 DPA 的
确定性实现：对无标签先验产物路径的不确定度求和，降序排序，pair ID 决定并列顺序。
逐 seed 核对首轮训练输入、初始 pair 集合、固定验证面板、数据/hash、版本、epochs 和
核时评价节点与同空间已有对照一致；不得复用其他策略 checkpoint 或借用 GT 排名。
追加到采样计划和监督器清单后，同步总实验数、轨迹数、最大任务数及状态报告。

### 采样监督中的验证 MAE

`python -m tricycle_sampling.replay.validation_monitor --run-dir <包含 sampling-plan.json 的根目录>`
只读取已完成且通过 worker 输出 hash、源码版本、固定验证面板与无训练泄漏校验的轮次，
生成 `VALIDATION_MAE_REPORT.md`、最新值/逐轮历史 CSV 和含来源 hash 的 JSON。
监督器可调用其 `update(root)` 并将报告加入执行总览，不修改实验的训练协议或已完成决策，也不保存历史控制器源码。
固定初始面板上的辅助模型留出 MAE 和主模型完整 GT 评估 MAE 必须分别标明；
后者仅用于只读评价，不加入采样决策。逐 seed 显示最近完成轮次、当前阶段、MAE、
相较上一完成轮次的变化、样本数和累计核时；无完成结果显示缺失值，不写成零。
各 seed 最新值的策略均值只用于监控，轮次和核时不同，不作为公平策略排名。

用户要求采样成本与 GT 误差图时，使用 `analysis.sampling.replay_sampling_panels`：
从包含 sampling-plan.json 或 suite.json 的根目录冻结完成检查点，再在报告环境渲染快照。
按实际出现空间布局，上排 GT MAE，下排同 seed 同成本的“方法−参照”差值；
新集合参照 predictive uncertainty，历史快照含 random 时保留 random。
静态虚线、智能实线，误差带为样本标准差；未完成轨迹逐点记录可用 seed 数，
覆盖不足完整 seed 集合时按绘图接口标记，n=1 不画带。使用最新已完成且预算内 checkpoint，不超出实际成本覆盖，
不外推；下排只汇总配对 seed 差值。固定辅助面板 MAE 与全池 GT MAE 分开报告。
