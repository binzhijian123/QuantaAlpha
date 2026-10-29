# QuantaAlpha 论文 v3/v3.1 验证流程

## 目标

本流程验证 arXiv `2602.07085v3` 所述方法在锁定数据、代码和参数后是否产生可重复的样本外效果。它不会把一次成功回测等同于可持续收益，也不会在预检失败时启动付费因子挖掘。

锁定基准：

- 论文：`2602.07085v3`，2026-05-18 修订版；PDF SHA256 为 `75e9c2ef5e8bb7fed78d27409e8252208e8fdacf6d10e1495dbc5b8767481848`。
- 论文主表口径：GPT-5.2 的 IC `0.0472`、Rank IC `0.0459`、年化超额收益 `4.68%`、信息比率 `0.6453`、最大回撤 `11.80%`。
- 官方代码：提交 `b7ceb27b1001261d7a95b209a963664ae1f8ab23`。
- 数据：`investment_data_20260725_cn`，合约指纹见 `configs/paper_validation_v3.yaml`。
- 时间：训练 2016-2020，验证 2021，测试 2022-2025-12-26；代码使用已对 t+2 标签做截断的有效边界。

工作区首页曾保留旧提交中的 IC `0.1501`、年化超额收益 `27.75%` 和最大回撤 `7.98%`，现已按正式 v3 PDF 和锁定官方提交改正；旧值不属于本流程的论文复现目标。验证程序只接受配置中锁定路径、版本和 SHA256 的 PDF；若 arXiv 发布新版本，必须另建协议并重新预注册，不能覆盖本次 v3 基准。

## 阶段

1. **阶段 0：零 LLM 预检**
   检查论文与代码哈希、数据覆盖、字段、时间切分、控制器轮次、质量门槛、回测参数、模型认证和核心算法契约。任何硬失败都会阻止后续运行。
2. **阶段 1：小规模算法试跑**
   只在阶段 0 通过后执行。验证轨迹保存、变异、交叉、因子质量门槛、2021 因子池筛选和可恢复性，不用于宣称论文有效。
3. **阶段 2：锁定规模复现**
   固定模型、随机种子、成本和资源预算，生成完整因子池并在锁定测试集上一次性评估。
4. **阶段 3：稳健性与实用性**
   运行种子、成本、年份和滚动窗口检验；只有通过固定门槛的冻结策略才可进入模拟盘。

## 当前状态

### v3.1 validation-only pilot（当前）

v3.1 用新的 `paper_family_v1` 合约替代 v3.0 中含义冲突的“三个自由变体”：每条轨迹只能表达一个经济机制，并必须生成三个有明确角色的因子。

- `canonical`：直接实现家族假设，不改变机制或参数。
- `normalization_robustness`：只改变归一化/缩放，保持机制、方向、原始输入和预测周期。
- `horizon_robustness`：只改变一个回看或平滑窗口，保持机制、方向、原始输入和归一化家族。

因子输出和进化提示均要求严格 JSON；计数、角色、变化轴、表达式哈希和逐因子质量门证据缺一即失败。当前配置使用串行执行，因为本机的 DeepSeek 兼容接口在并发请求下出现过连接握手失败；串行模式还使每个成功任务后保存的 `evolution_state.json` 能确定性恢复。恢复时复用已完成的原始方向，不再次调用规划模型；如果轨迹池已含变异/交叉结果但状态文件缺失，则失败关闭。

最终试跑 `paper_v3_1_pilot_20260729_154805` 已跑完 2 条原始、2 条变异和 1 条交叉轨迹：

- 最新零生成预检 `paper_v3_1_preflight_final_20260729_200347` 为 `READY_FOR_PILOT`，硬失败 0、LLM 调用 0；保留“主干模型不是论文 GPT-5.2”和“论文未公开参数”两项声明一致性警告。
- 5 条轨迹均成功，每条 3 个同家族变体；因子库共 15 个，2021 验证池选出 4 个。
- 4 个入池因子的 2021 mean RankIC 为 `0.023808`、`0.019711`、`-0.017255`、`-0.018233`；来源反馈判定为 `false/true/false/false`。
- `NegReturn_VolumeScaled_EMA20` 是唯一反馈为真的候选：2021 无成本年化收益 `17.86%`、IR `1.342`，计入冻结成本后年化收益 `15.54%`、IR `1.168`。这是值得后续前向检验的单年研究信号，不是盈利证明。
- v2 审计失败项为 `selected_validation_rankic` 和 `selected_feedback`，最终状态严格保持 `RESEARCH_ONLY`，禁止生成可交易信号。

本次的评估、排序和组合回测都没有使用 2022-2025：实际生成的 6 份 Qlib 结果均为模型训练 2016-2019、模型验证 2020、研究验证/组合回测 2021。需要准确区分的是，DataHandler 配置覆盖到 2025-12-26，底层 Provider 也包含更晚日期，因此无法证明进程在物理层面零读取 2022-2025；v2 审计只声明这些日期没有进入评估、排序或回测证据。该测试区间已被早期 v3.0 运行查看，不能再作为独立样本外证据，后续只能使用冻结协议后的前向模拟盘。

### v3.0 历史运行（已停止晋级）

最新预检 `paper_preflight_20260727_153832` 状态为 `READY_FOR_PILOT`，硬失败为 0，预检 LLM 调用数为 0。项目 `.env` 的模型配置优先级已经与 `run.sh` 对齐，配置的 `deepseek-v4-flash` 模型也通过了无生成认证探测。该模型不是论文使用的 GPT-5.2，因此下面的结果只验证本地机制，不是论文的同模型复现。

低成本机制试验 `paper_v3_pilot_20260727_153839` 已完整跑通：

- 进化产物包含 2 条原始轨迹、1 条有结果的变异轨迹、1 条无结果变异轨迹和 1 条交叉轨迹。无结果轨迹 `8bee824bd9fc` 因调度缺陷被保存，但没有进入因子库或最终池。
- 因子库产生 12 个带真实质量门证据和 H5 缓存的候选；2021 验证期按冻结规则选出 6 个，来源为原始 1 个、变异 2 个、交叉 3 个。
- 锁定的 2022-01-04 至 2025-12-26 样本外回测得到：IC `0.020100`、Rank IC `0.016508`、年化收益 `3.54%`、信息比率 `0.452`、最大回撤 `-14.63%`、Calmar `0.242`。
- 6 个入池因子的 `feedback.decision` 全为 `false`，说明它们都没有达到相同条件下的基线改进要求。该试验不能晋级为 `PAPER_CANDIDATE`，也不能启用模型模拟盘信号。
- 12 个缓存的路径、大小和 SHA256 已全部复核；对本次和中止试跑的 972 个日志、工作区及 pickle 文件做凭据值扫描，匹配数为 0。

本地实现现在会：

- 记录每个五步工作流的结构化输出和带 SHA256 的真实 pickle 检查点。
- 变异时定位 `factor_propose`、`factor_construct` 或 `factor_calculate`，复用冻结前缀检查点，只执行目标动作及其后缀。
- 交叉时记录每个假设模板、构造模式和修复行为的父轨迹来源，要求至少来自两个高质量父轨迹。
- 只允许有逐因子真实质量门槛证据的因子进入池；并行写库使用文件锁和原子替换。
- 只在 2021 验证面板计算每日横截面 Spearman RankIC，按 RankIC 贪心准入，执行绝对相关性 `<0.7`、最多 150 个且不超过质量门槛合格因子数 50% 的限制。
- 有界进化任务若没有候选因子或实际回测结果会失败关闭；变异和交叉不会再选择空结果轨迹作为父轨迹。

v3.0 已停止晋级；v3.1 已修复三个因子变体与单一假设的冲突及结构化输出恢复问题，但新的验证池仍未通过门禁。因此仍不启动 141 因子的付费规模运行，也不得根据已查看的 2022-2025 结果继续调参。论文未公开 AST 重复阈值、每个交叉阶段的子代数量和相关性聚合公式，也未明确 50% 上限的分母；当前操作性定义均已在协议中锁定并作为声明一致性警告保留。

## 命令

只运行 v3.1 零成本预检：

```bash
source scripts/qa_env.sh
python scripts/run_paper_validation_preflight.py \
  --config configs/paper_validation_pilot_v3_1.yaml
```

运行受保护的 v3.1 小样本。未设置付费开关时只完成预检并退出：

```bash
./scripts/start_paper_validation_pilot_v3_1.sh
ALLOW_PAID_PAPER_PILOT_V31=1 ./scripts/start_paper_validation_pilot_v3_1.sh
```

恢复已有 v3.1 试跑时，必须沿用原实验 ID、数据绑定和轨迹目录，并显式关闭 fresh start。不要用启动器恢复，因为启动器会创建新 ID：

```bash
source scripts/qa_env.sh
export EXPERIMENT_ID=paper_v3_1_pilot_YYYYMMDD_HHMMSS
export CONFIG_PATH=configs/experiment_paper_pilot_v3_1.yaml
export QUANTAALPHA_LOG_TRACE_PATH="$PWD/log/$EXPERIMENT_ID"
export QUANTAALPHA_EVOLUTION_FRESH_START=0
export FACTOR_CoSTEER_DATA_FOLDER_OVERRIDE="$PWD/data/research/paper_validation/factor_data/investment_data_20260725"
export FACTOR_CoSTEER_DATA_FOLDER_DEBUG_OVERRIDE="$FACTOR_CoSTEER_DATA_FOLDER_OVERRIDE"
export QLIB_DATA_DIR_OVERRIDE="$PWD/data/market/a_share/qlib/investment_data_20260725_cn"
export FACTOR_CoSTEER_SYMBOL_LENGTH_THRESHOLD=250
export FACTOR_CoSTEER_BASE_FEATURES_THRESHOLD=6
export FACTOR_CoSTEER_FREE_ARGS_RATIO_THRESHOLD=0.5
export FACTOR_CoSTEER_UNIQUE_VARS_RATIO_THRESHOLD=1.0
export FACTOR_CoSTEER_DUPLICATION_THRESHOLD=5
./run.sh "CSI 300 paper-v3.1 validation-only mechanism pilot. Use only mechanism priors; do not mention or infer any 2022-2025 observations, metrics, winners, or test outcomes." "$EXPERIMENT_ID"
```

以下为 v3.0 历史命令，仅保留复核用途：

准备锁定的六字段因子数据：

```bash
source scripts/qa_env.sh
python scripts/prepare_paper_factor_data.py \
  --config configs/paper_validation_v3.yaml
```

付费运行入口默认加锁：

```bash
./scripts/start_paper_validation.sh
```

推荐先运行 2 个方向、1 个 Mutation+Crossover 周期、最多尝试 15 个因子的低成本机制试验：

```bash
ALLOW_PAID_PAPER_PILOT=1 ./scripts/start_paper_validation_pilot.sh
```

完整约 141 个候选因子的锁定配置：

```bash
ALLOW_PAID_PAPER_RUN=1 ./scripts/start_paper_validation.sh
```

只有预检通过并明确设置相应开关后，入口才允许调用模型。旧 v3.0 入口会执行已不再独立的 2022-2025 历史测试；v3.1 入口只构建 2021 验证池并生成审计，不运行 2022-2025 回测。不要把 API 密钥写入命令、日志或文档。

## 产物

- 协议：`configs/paper_validation_v3.yaml`
- v3.1 pilot 协议：`configs/paper_validation_pilot_v3_1.yaml`
- v3.1 挖掘配置：`configs/experiment_paper_pilot_v3_1.yaml`
- 挖掘配置：`configs/experiment_paper_v3.yaml`
- 回测配置：`configs/backtest_paper_v3.yaml`
- 低成本试验：`configs/experiment_paper_pilot_v3.yaml`
- 锁定数据：`data/research/paper_validation/factor_data/investment_data_20260725/`
- 预检结果：`data/results/paper_validation/paper_preflight_*/`
- 最终因子池：`data/results/paper_validation/*_factor_pool/`
- 锁定回测指标：`data/results/paper_validation/*_backtest_metrics.json`
- 本次试验因子池：`data/results/paper_validation/paper_v3_pilot_20260727_153839_factor_pool/`
- 本次试验指标：`data/results/paper_validation/paper_v3_pilot_20260727_153839_backtest_metrics.json`
- v3.1 最终轨迹：`log/paper_v3_1_pilot_20260729_154805/trajectory_pool.json`
- v3.1 最终因子库：`data/factorlib/all_factors_library_paper_v3_1_pilot_20260729_154805.json`
- v3.1 最终验证池：`data/results/paper_validation/paper_v3_1_pilot_20260729_154805_factor_pool/`
- v3.1 最终审计：`data/results/paper_validation/paper_v3_1_pilot_20260729_154805_audit_v2/`

每次预检目录包含 `preflight.json`、`manifest.json` 和 `report.md`。结论以机器可读的 `status`、`hardFailureIds` 和哈希为准；v3.1 pilot 的晋级结论以最新审计目录中的 `audit.json` 为准。
