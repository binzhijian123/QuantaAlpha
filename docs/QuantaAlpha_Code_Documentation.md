# QuantaAlpha 代码文档说明书

> 生成日期：2026-06-09  
> 适用项目：`/absolute/path/to/user/Documents/QuantaAlpha-main`  
> 参考结构：本地 checkout 与 GitHub 主分支 `https://github.com/QuantaAlpha/QuantaAlpha/tree/main`

## 1. 项目总览

QuantaAlpha 是一个 Python + React 项目，用于 LLM 驱动的量化因子挖掘。用户输入自然语言研究方向后，系统通过规划、轨迹演化、因子表达式生成、因子代码执行、Qlib 回测和反馈总结，持续产生并筛选 Alpha 因子。项目同时提供命令行入口和 Web UI。

核心链路：

```text
研究方向
  -> planning 生成多个探索方向
  -> evolution 组织 original / mutation / crossover 任务
  -> AlphaAgentLoop 五步循环
     -> factor_propose
     -> factor_construct
     -> factor_calculate
     -> factor_backtest
     -> feedback
  -> data/factorlib/all_factors_library*.json
  -> 独立 backtest_v2
  -> 回测指标和结果文件
```

## 2. 顶层结构

| 路径 | 功能与责任 |
| --- | --- |
| `README.md`, `README_CN.md` | 项目介绍、安装、数据准备、CLI 和 Web UI 使用说明。 |
| `pyproject.toml` | Python 包定义；注册命令行入口 `quantaalpha = quantaalpha.cli:app`。 |
| `requirements.txt`, `requirements/` | 主依赖和 docs/test/lint/package 分组依赖。 |
| `launcher.py` | 统一 Python 启动器；加载 `.env` 后调用 `quantaalpha.cli.app`。 |
| `run.sh` | 主实验启动脚本；加载 `.env`、激活 conda、校验 Qlib 数据、创建实验工作区并调用 `quantaalpha mine`。 |
| `scripts/qa_env.sh` | 共享环境 bootstrap；负责 conda 环境和 `PYTHONPATH`，被 CLI 与 Web 启动脚本复用。 |
| `configs/` | 实验与回测 YAML 配置。 |
| `quantaalpha/` | Python 核心包。 |
| `frontend-v2/` | React/Vite Web UI 和 FastAPI 后端。 |
| `docs/` | Sphinx/Markdown 文档、图片和用户指南。 |
| `data/`, `log/`, `mlruns/` | 生成数据、日志、实验 artifact 和缓存，不应随意清理。 |
| `cn_data/`, `git_ignore_folder/` | 本地市场数据、HDF5 源数据和临时缓存。 |

## 3. 配置文件

### 3.1 `configs/experiment.yaml`

控制主因子挖掘流程。

| 配置段 | 责任 |
| --- | --- |
| `planning` | 是否启用并行规划、生成几个方向、是否调用 LLM、失败时是否 fallback。 |
| `execution` | 每个任务循环数、每轮步骤数、是否本地回测、是否并行执行。 |
| `evolution` | 是否启用演化、mutation/crossover 开关、最大轮次、父代选择策略、轨迹池保存策略。 |
| `quality_gate` | 一致性、复杂度、冗余检查开关。 |
| `factor` | 每个假设生成几个因子、复杂度阈值、重复检测参数。 |
| `backtest` | mining 阶段内部回测方式、Qlib config 名称、超时。 |
| `llm` | 因子挖掘超时、重试、JSON 模式。 |
| `logging` | 日志等级、快照和轨迹池保存。 |

### 3.2 `configs/backtest.yaml`

控制独立回测 V2。

| 配置段 | 责任 |
| --- | --- |
| `experiment` | 回测实验名、recorder、输出目录和指标文件名。 |
| `factor_source` | 因子来源：`alpha158`、`alpha158_20`、`alpha360`、`custom`、`combined`。 |
| `data` | Qlib provider、region、market、时间范围。 |
| `dataset` | 标签表达式、处理器、train/valid/test 分段。 |
| `model` | LightGBM 参数。 |
| `backtest` | TopKDropout 策略、benchmark、交易成本、回测区间。 |
| `llm` | 自定义因子计算缓存和 LLM fallback 配置。 |
| `factor_calculation` | 自定义因子计算输出目录、并行数、数据文件。 |

## 4. Python 核心包 `quantaalpha/`

### 4.1 CLI 与入口

| 文件 | 功能 |
| --- | --- |
| `quantaalpha/cli.py` | Fire CLI 入口。命令包括 `mine`、`backtest`、`health_check`、`collect_info`。自动加载项目 `.env`。 |
| `launcher.py` | Python 启动器；检查 `.env` 后交给 CLI。 |
| `run.sh` | Shell 入口；加载 `.env`、source `scripts/qa_env.sh`，校验 Qlib 数据并调用 `quantaalpha mine`。 |

调用关系：

```text
run.sh
  -> quantaalpha mine --direction ... --config_path ...
  -> quantaalpha.cli.app
  -> quantaalpha.pipeline.factor_mining.main
```

### 4.2 `quantaalpha/pipeline/`

| 文件/目录 | 功能 |
| --- | --- |
| `factor_mining.py` | 主挖掘入口；加载配置、决定是否进入 evolution 模式、生成规划方向、运行任务。 |
| `loop.py` | 定义 `AlphaAgentLoop`，即因子挖掘的五步工作流；也包含 `BacktestLoop`。 |
| `planning.py` | 调用 LLM 或 fallback 模板，把用户初始方向扩展成多个探索方向。 |
| `settings.py` | 定义动态组件 class path，例如 hypothesis generator、coder、runner、summarizer。 |
| `base.py` | 通用 RDLoop 基类流程：propose -> exp_gen -> coding -> running -> feedback。 |
| `factor_backtest.py` | CLI backtest 兼容入口。 |
| `factor_from_report.py` | 从研报生成假设和因子实验。 |
| `prompts/` | planning/evolution prompt YAML。 |
| `evolution/` | 演化控制器、轨迹池、mutation、crossover。 |

`factor_mining.py` 关键函数：

- `main(...)`：加载 `configs/experiment.yaml`，读取 planning、execution、evolution、quality_gate、factor 配置；根据配置选择 evolution 模式或传统 planning 分支模式。
- `run_evolution_loop(...)`：生成初始方向，初始化 `EvolutionController`，按 phase 执行 original/mutation/crossover，保存 evolution 状态和轨迹池。
- `_run_evolution_task(...)`：构造 `AlphaAgentLoop`，运行一个小循环，返回 trajectory 数据。
- `_run_tasks_parallel(...)`：并行执行当前 phase 的多个任务。

`AlphaAgentLoop` 五步循环：

| 步骤 | 方法 | 输入 | 输出 |
| --- | --- | --- | --- |
| 1 | `factor_propose` | 历史 trace、研究方向 | 因子假设 |
| 2 | `factor_construct` | 假设 | 因子任务/表达式实验 |
| 3 | `factor_calculate` | 因子任务 | 生成并执行因子代码，得到因子值 |
| 4 | `factor_backtest` | 因子值 | Qlib 回测结果 |
| 5 | `feedback` | 回测结果和假设 | 反馈、trace 更新、写入统一因子库 |

`feedback` 阶段会调用 `FactorLibraryManager.add_factors_from_experiment(...)`，把因子名称、表达式、代码、回测指标、假设、演化阶段、轨迹 ID 等写入 `data/factorlib/all_factors_library*.json`。

### 4.3 `quantaalpha/pipeline/evolution/`

| 文件 | 功能 |
| --- | --- |
| `controller.py` | `EvolutionController`，决定下一步任务、phase 切换、父代选择、任务完成上报。 |
| `trajectory.py` | `StrategyTrajectory` 和 `TrajectoryPool`，记录假设、因子、回测指标、反馈和父代 lineage。 |
| `mutation.py` | 从父轨迹生成正交/变异探索 prompt suffix。 |
| `crossover.py` | 从多个父轨迹组合生成混合策略 prompt suffix。 |

调用顺序：

```text
factor_mining.run_evolution_loop
  -> EvolutionController.get_next_task / get_all_tasks_for_current_phase
  -> MutationOperator / CrossoverOperator 生成 strategy_suffix
  -> AlphaAgentLoop 运行一个小循环
  -> EvolutionController.create_trajectory_from_loop_result
  -> TrajectoryPool.add
```

关键数据结构：

| 类 | 责任 |
| --- | --- |
| `EvolutionConfig` | 配置方向数、最大轮次、mutation/crossover 开关、父代选择策略。 |
| `StrategyTrajectory` | 保存一次完整因子发现尝试：hypothesis、factors、metrics、feedback、parent_ids。 |
| `TrajectoryPool` | 按 direction、phase 索引轨迹，保存和加载 JSON 状态。 |

### 4.4 `quantaalpha/factors/`

| 文件/目录 | 功能 |
| --- | --- |
| `experiment.py` | Qlib 因子实验和场景类。 |
| `proposal.py` | LLM 假设生成、假设到因子表达式转换。 |
| `runner.py` | `QlibFactorRunner`，处理因子值并调用 Qlib 回测。 |
| `library.py` | 统一因子库 CRUD、H5 -> MD5 缓存同步、缓存状态检查。 |
| `feedback.py` | 把回测结果总结成反馈，用于下一轮 trace。 |
| `qlib_coder.py` | Qlib 因子代码生成/解析相关封装。 |
| `qlib_utils.py` | Qlib 数据目录生成和数据说明。 |
| `workspace.py` | Qlib 因子执行 workspace。 |
| `coder/` | 因子代码生成、表达式解析、函数库、评估器。 |
| `loader/` | JSON/PDF 因子加载。 |
| `regulator/` | 因子一致性、复杂度、冗余和质量门控。 |
| `prompts/` | 因子生成 prompt。 |
| `factor_template/` | Qlib 回测模板配置。 |

`proposal.py` 关键组件：

| 类/函数 | 责任 |
| --- | --- |
| `AlphaAgentHypothesisGen` | 根据用户方向和历史反馈生成新假设。 |
| `AlphaAgentHypothesis2FactorExpression` | 将假设转成可执行因子表达式任务。 |
| `QlibFactorHypothesisGen` | 传统 Qlib 因子假设生成。 |
| `QlibFactorHypothesis2Experiment` | 把 LLM JSON 转成 `QlibFactorExperiment`。 |
| `render_hypothesis_and_feedback` | 把有限历史 trace 渲染给 LLM，防止上下文过长。 |

`coder/` 关键文件：

| 文件 | 功能 |
| --- | --- |
| `factor.py` | `FactorTask` 和 `FactorFBWorkspace`；执行生成的 `factor.py`，读取 `result.h5`。 |
| `expr_parser.py` | 把因子表达式解析成 Python 可执行形式。 |
| `factor_ast.py` | 表达式 AST、相似度、复杂度、公共子树检测。 |
| `function_lib.py` | 因子表达式函数库，如 `TS_MEAN`、`TS_CORR`、`RANK`、`ZSCORE`、`MACD`、`RSI`。 |
| `evaluators.py`, `eva_utils.py` | 代码格式、输出格式、缺失值、相关性等评估器。 |
| `evolving_strategy.py` | 多进程生成、解析、运行因子代码的 CoSTEER 策略。 |

`QlibFactorRunner.develop(...)` 负责：

1. 执行每个因子 workspace，得到因子值 DataFrame。
2. 合并新因子和可选 SOTA 因子。
3. 保存 `combined_factors_df.parquet`。
4. 调用 Qlib workspace 执行回测。
5. 将结果 DataFrame 写回 experiment。
6. 返回包含结果的 `QlibFactorExperiment`。

`FactorLibraryManager` 负责：

- 读写统一因子库 JSON。
- 从 experiment 中提取因子名、表达式、代码、workspace cache、回测指标和 metadata。
- 把 workspace 的 `result.h5` 同步成表达式 MD5 `.pkl` 缓存。
- 统计 h5/md5/需计算数量。
- 批量预热因子缓存。

### 4.5 `quantaalpha/backtest/`

| 文件 | 功能 |
| --- | --- |
| `run_backtest.py` | 独立回测 CLI。支持 `--factor-source`、`--factor-json`、`--dry-run`、`--skip-uncached`。 |
| `runner.py` | `BacktestRunner`，加载因子、构造 Qlib dataset、训练 LightGBM、执行策略回测、保存指标。 |
| `factor_loader.py` | 加载 Qlib 官方因子集和自定义因子库 JSON。 |
| `custom_factor_calculator.py` | 使用表达式 parser/function_lib 计算自定义因子，优先读缓存。 |
| `factor_calculator.py` | 通用因子计算器，支持 parser 计算和 LLM fallback 生成代码。 |

独立回测调用关系：

```text
python -m quantaalpha.backtest.run_backtest -c configs/backtest.yaml --factor-source custom --factor-json ...
  -> BacktestRunner.run
  -> FactorLoader.load_factors
  -> CustomFactorCalculator.calculate_factors_batch
  -> BacktestRunner._create_dataset
  -> LightGBM train
  -> Qlib backtest
  -> data/results/backtest_v2_results/backtest_metrics.json
```

### 4.6 `quantaalpha/llm/`

| 文件 | 功能 |
| --- | --- |
| `config.py` | `LLMSettings`，从环境变量加载 API key、base URL、模型名、token、cache、timeout。 |
| `client.py` | `APIBackend`、聊天会话、缓存、JSON 修复、embedding 距离等。 |

依赖关系：

```text
planning.py / proposal.py / regulator / factor_calculator
  -> quantaalpha.llm.client.APIBackend
  -> quantaalpha.llm.config.LLM_SETTINGS
  -> .env 中 OPENAI_API_KEY / OPENAI_BASE_URL / CHAT_MODEL / REASONING_MODEL
```

### 4.7 `quantaalpha/core/`

| 文件 | 功能 |
| --- | --- |
| `experiment.py` | `Task`、`Workspace`、`Experiment`、loader 抽象。 |
| `proposal.py` | `Hypothesis`、`Trace`、假设生成和假设转实验抽象。 |
| `developer.py` | 代码生成/执行开发者抽象。 |
| `evaluation.py` | `Feedback` 和 `Evaluator` 抽象。 |
| `evolving_framework.py` | evolution/RAG 的抽象类。 |
| `conf.py` | Pydantic settings 和 RD_AGENT 全局配置。 |
| `scenario.py` | 场景抽象。 |
| `prompts.py` | prompt YAML 读取与单例缓存。 |
| `template.py` | 代码模板工具。 |
| `utils.py` | JSON 解析、动态 import、多进程、pickle cache。 |
| `exception.py` | 因子、代码、runner 异常类型。 |

Core 是架构底座，pipeline 和 factors 通过动态 class path 调用这些抽象。

### 4.8 其他 Python 模块

| 路径 | 功能 |
| --- | --- |
| `quantaalpha/coder/costeer/` | CoSTEER 代码演化框架，包含 task、scheduler、evaluator、strategy、agent。 |
| `quantaalpha/coder/knowledge/` | 向量库和图谱知识库。 |
| `quantaalpha/contrib/model/` | 可选模型实验扩展，结构类似 factor pipeline。 |
| `quantaalpha/components/` | proposal、runner、benchmark 等复用组件。 |
| `quantaalpha/utils/` | env、workflow、loader、document_reader、agent 工具。 |
| `quantaalpha/app/utils/health_check.py` | 环境健康检查。 |
| `quantaalpha/app/utils/info.py` | 收集系统信息。 |
| `quantaalpha/docker/` | Dockerfile 和 Docker 相关初始化。 |
| `quantaalpha/log/` | logger 和耗时统计。 |

## 5. Web UI `frontend-v2/`

### 5.1 启动与构建

| 文件 | 功能 |
| --- | --- |
| `frontend-v2/start.sh` | 一键启动脚本；激活 conda、检查 Node/Python、安装依赖、启动 FastAPI 后端和 Vite 前端。 |
| `package.json` | `npm run dev`、`npm run build`、`npm run backend`。 |
| `vite.config.ts` | Vite 配置，通常包含 `/api` 和 WebSocket proxy。 |
| `tailwind.config.js`, `postcss.config.js`, `index.css` | 样式系统。 |

启动链路：

```text
bash frontend-v2/start.sh
  -> source scripts/qa_env.sh
  -> python frontend-v2/backend/app.py  # port 8000
  -> npm run dev                        # port 3000
  -> 用户访问 http://localhost:3000
```

### 5.2 FastAPI 后端 `frontend-v2/backend/app.py`

职责：

- 提供 REST API 和 WebSocket。
- 启动 mining 子进程。
- 启动独立 backtest 子进程。
- 读取/筛选因子库。
- 管理任务状态、日志、诊断、进度和指标。
- 遮蔽日志中的 API key。

关键 API：

| API | 功能 |
| --- | --- |
| `GET /api/health` | 后端健康检查。 |
| `POST /api/v1/mining/start` | 创建 mining task。 |
| `GET /api/v1/mining/{task_id}` | 查询 mining 状态。 |
| `DELETE /api/v1/mining/{task_id}` | 取消 mining。 |
| `GET /api/v1/mining/tasks/list` | 列出任务。 |
| `GET /api/v1/factors` | 分页、搜索、按质量筛选因子。 |
| `GET /api/v1/factors/libraries` | 列出因子库 JSON。 |
| `GET /api/v1/factors/cache-status` | 检查因子缓存状态。 |
| `POST /api/v1/factors/warm-cache` | 预热因子缓存。 |
| `GET /api/v1/factors/{factor_id}` | 查询因子详情。 |
| `POST /api/v1/backtest/start` | 创建独立回测 task。 |
| `GET /api/v1/backtest/{task_id}` | 查询回测状态。 |
| `DELETE /api/v1/backtest/{task_id}` | 取消回测。 |
| `GET /api/v1/system/config` | 读取系统配置。 |
| `PUT /api/v1/system/config` | 更新系统配置。 |
| `WS /ws/mining/{task_id}` | 推送进度、日志、指标和结果。 |

后端 mining 调用链：

```text
POST /api/v1/mining/start
  -> start_mining
  -> asyncio.create_task(_run_mining)
  -> 写临时 experiment_override.yaml
  -> python -m quantaalpha.cli mine --direction ... --config_path ...
  -> 逐行读取 stdout
  -> _sanitize_log_line
  -> _record_diagnostic
  -> WebSocket broadcast
```

后端 backtest 调用链：

```text
POST /api/v1/backtest/start
  -> start_backtest
  -> _run_backtest
  -> 解析 factorJson 到 data/factorlib/*.json
  -> python -m quantaalpha.backtest.run_backtest -c configs/backtest.yaml ...
  -> 读取输出 metrics
  -> WebSocket broadcast
```

### 5.3 React 前端 `frontend-v2/src/`

| 文件/目录 | 功能 |
| --- | --- |
| `main.tsx` | React 挂载入口。 |
| `App.tsx` | 页面切换和 `TaskProvider` 包裹；通过 `display:none` 保持页面不卸载，避免任务状态丢失。 |
| `services/api.ts` | 统一 API client；封装 mining、factor、cache、backtest、system config、health、WebSocket。 |
| `context/TaskContext.tsx` | 全局任务状态；管理 mining/backtest task、WebSocket、轮询、日志、曲线数据。 |
| `types/index.ts` | 前端类型定义。 |
| `utils/index.ts` | 通用工具。 |
| `utils/miningDirections.ts` | 默认 mining 方向。 |
| `pages/HomePage.tsx` | 首页/输入研究方向。 |
| `pages/MiningDashboardPage.tsx` | 实时挖掘仪表盘。 |
| `pages/FactorLibraryPage.tsx` | 因子库浏览、搜索、筛选。 |
| `pages/BacktestPage.tsx` | 独立回测页面。 |
| `pages/SettingsPage.tsx` | `.env` 和运行参数设置页面。 |
| `components/layout/Layout.tsx` | 主导航布局。 |
| `components/ui/` | Button、Card、Badge、Progress、Textarea 等基础 UI。 |
| `components/FactorList.tsx` | 因子列表展示。 |
| `components/ProgressSidebar.tsx` | 任务进度侧栏。 |
| `components/LiveCharts.tsx` | 实时曲线图。 |
| `components/MetricsPanel.tsx` | 指标面板。 |
| `components/ExecutionPanel.tsx` | 执行日志/状态面板。 |

前端状态流：

```text
用户在 HomePage 输入方向
  -> TaskContext.startMining
  -> services/api.startMining
  -> FastAPI /api/v1/mining/start
  -> 返回 taskId
  -> connectMiningWs(taskId)
  -> TaskContext 接收 progress/log/metrics/result
  -> MiningDashboardPage / MetricsPanel / LiveCharts 更新
```

## 6. 数据流

### 6.1 输入数据

| 数据 | 来源 | 用途 |
| --- | --- | --- |
| 研究方向文本 | CLI 参数或 Web UI 输入 | planning 和 hypothesis generation。 |
| `.env` | 项目根目录 | Qlib 数据路径、结果路径、LLM API、模型名、conda env。 |
| `configs/experiment.yaml` | 配置文件 | mining/planning/evolution/quality gate 参数。 |
| `configs/backtest.yaml` | 配置文件 | 独立回测参数。 |
| Qlib 市场数据 | `QLIB_DATA_DIR` 或 `~/.qlib/qlib_data/cn_data` | 回测、标签、官方因子计算。 |
| HDF5 价量数据 | `git_ignore_folder/factor_implementation_source_data*/daily_pv.h5` | mining 阶段因子代码执行。 |
| 因子库 JSON | `data/factorlib/*.json` | 因子浏览和独立回测。 |

### 6.2 Mining 阶段处理

```text
用户方向
  -> generate_parallel_directions
  -> EvolutionController 生成任务
  -> AlphaAgentHypothesisGen 生成假设
  -> AlphaAgentHypothesis2FactorExpression 生成因子表达式
  -> QlibFactorParser / CoSTEER 生成或解析 factor.py
  -> FactorFBWorkspace.execute 计算 result.h5
  -> QlibFactorRunner 合并因子值并回测
  -> feedback 生成反馈
  -> FactorLibraryManager 写入因子库
```

### 6.3 独立回测阶段处理

```text
factor library JSON
  -> FactorLoader 分离官方因子和自定义因子
  -> CustomFactorCalculator 优先读 cache，必要时用表达式 parser 计算
  -> BacktestRunner 创建 Qlib DatasetH
  -> LightGBM 训练预测
  -> TopkDropoutStrategy 回测
  -> 保存 metrics 和 artifact
```

### 6.4 输出数据

| 输出 | 位置 | 内容 |
| --- | --- | --- |
| 因子库 | `data/factorlib/all_factors_library*.json` | 因子名、表达式、代码、metadata、回测指标、反馈。 |
| 因子执行结果 | workspace 下 `result.h5` | 单因子值。 |
| 因子缓存 | `data/results/factor_cache/*.pkl` | 表达式 MD5 对应的因子值。 |
| mining workspace | `data/results/workspace_<EXPERIMENT_ID>` | 本次实验工作区、临时配置、日志。 |
| pickle cache | `data/results/pickle_cache_<EXPERIMENT_ID>` | Python 缓存。 |
| evolution 状态 | log/workspace 下 `trajectory_pool.json`, `evolution_state.json` | 轨迹、phase、父代、指标。 |
| 独立回测结果 | `data/results/backtest_v2_results/` | `backtest_metrics.json` 和相关结果文件。 |
| 日志 | `log/`, workspace `run.log` | pipeline、LLM、Qlib、Web 后端日志。 |

## 7. 回测指标生成

Mining 内部回测由 `QlibFactorRunner` 调用 Qlib workspace 执行模板配置。新因子值保存为 `combined_factors_df.parquet`，Qlib config 通常来自 `quantaalpha/factors/factor_template/conf_baseline.yaml` 或 `conf_combined_factors.yaml`。回测结果写入 experiment 的 `result`，`feedback.py` 从结果中提取 IC、RankIC、ICIR、RankICIR、收益、回撤等指标并生成下一轮反馈。`FactorLibraryManager` 把指标保存到因子库的 `backtest_results`。

独立回测 V2 由 `BacktestRunner` 构造 train/valid/test dataset，训练 LightGBM，生成预测信号，用 `TopkDropoutStrategy` 做组合回测，并保存指标。典型指标包括：IC、ICIR、Rank IC、Rank ICIR、annualized return、information ratio、max drawdown、benchmark/excess return 相关指标。

## 8. 模块依赖关系

```text
用户输入研究方向
  -> run.sh / quantaalpha.cli / Web UI
  -> pipeline.factor_mining
  -> pipeline.planning
  -> pipeline.evolution.EvolutionController
  -> pipeline.loop.AlphaAgentLoop
  -> factors.proposal
  -> factors.qlib_coder / factors.coder / CoSTEER
  -> factors.runner.QlibFactorRunner
  -> factors.feedback
  -> factors.library.FactorLibraryManager
  -> data/factorlib/*.json
  -> backtest.run_backtest / BacktestRunner
  -> data/results/backtest_v2_results
```

Web 依赖关系：

```text
frontend-v2 React
  -> services/api.ts
  -> frontend-v2/backend/app.py
  -> quantaalpha CLI 子进程
  -> data/factorlib/*.json
  -> WebSocket 返回 progress / log / metrics / result
```

## 9. 整体运行步骤

### 9.1 CLI mining

1. 用户准备 `.env`、Qlib 数据和 HDF5 数据。
2. 执行 `./run.sh "Price-Volume Factor Mining"`。
3. `run.sh` 加载 `.env`，source `scripts/qa_env.sh`。
4. 校验 `QLIB_DATA_DIR` 包含 `calendars/`、`features/`、`instruments/`。
5. 设置 `EXPERIMENT_ID`、workspace、pickle cache。
6. 调用 `quantaalpha mine`。
7. pipeline 读取 `configs/experiment.yaml`。
8. planning 生成方向。
9. evolution 执行 original/mutation/crossover。
10. `AlphaAgentLoop` 执行五步因子工作流。
11. 因子写入 `data/factorlib/all_factors_library*.json`。
12. 日志和轨迹状态写入 workspace/log。

### 9.2 Web UI mining

1. 用户执行 `cd frontend-v2 && bash start.sh`。
2. 后端启动在 `localhost:8000`。
3. 前端启动在 `localhost:3000`。
4. 用户在首页输入方向并启动实验。
5. FastAPI 生成临时 override config。
6. 后端启动 `python -m quantaalpha.cli mine ...` 子进程。
7. 后端通过 WebSocket 推送日志、进度、诊断、指标。
8. 前端 `TaskContext` 保持任务状态，页面切换不丢失连接上下文。
9. 因子库页面读取 `data/factorlib/*.json`。

### 9.3 独立回测

1. 用户选择因子库 JSON。
2. CLI 或 Web UI 调用 backtest。
3. `FactorLoader` 加载官方/自定义因子。
4. `CustomFactorCalculator` 从缓存或表达式计算自定义因子。
5. `BacktestRunner` 构造 Qlib dataset。
6. LightGBM 训练并生成预测。
7. Qlib 策略回测生成指标。
8. 结果保存到 `data/results/backtest_v2_results`。

## 10. 关键脚本说明

| 脚本 | 执行任务 |
| --- | --- |
| `run.sh` | 主实验入口；校验环境和 Qlib 数据，隔离实验工作区，调用 `quantaalpha mine`。 |
| `scripts/qa_env.sh` | 长期环境入口；负责 conda 激活和项目 `PYTHONPATH`，被 CLI 和 Web 启动脚本复用。 |
| `frontend-v2/start.sh` | Web UI 一键启动；启动 FastAPI 后端和 Vite 前端，复用已有端口服务。 |
| `launcher.py` | Python 入口替代方案；加载 `.env` 后交给 Fire CLI。 |
| `quantaalpha/backtest/run_backtest.py` | 独立回测命令行入口。 |

## 11. 开发注意事项

- 不要把 `data/`、`log/`、`mlruns/`、`git_ignore_folder/`、`cn_data/` 当成普通源码清理；这些包含用户数据、实验输出和缓存。
- 修改 backend API 时，需要同步 `frontend-v2/src/services/api.ts` 和 `frontend-v2/src/types/index.ts`。
- 修改 mining 参数时，需要同步 `configs/experiment.yaml`、FastAPI `MiningStartRequest`、前端启动表单和 `TaskContext`。
- 修改因子库 JSON schema 时，需要同步 `FactorLibraryManager`、FastAPI `/api/v1/factors`、`FactorLibraryPage` 和独立回测 `FactorLoader`。
- 修改启动脚本时，应验证非交互 shell、screen/automation 和当前交互 shell，因为 conda/PATH 问题经常依赖启动上下文。
- 日志进入 WebSocket 前必须经过 secret masking，避免泄露 API key。
- 回测相关变更应确认 Qlib 数据路径、LightGBM/OpenMP 依赖和缓存路径。

## 12. 本文档生成说明

本 PDF 是只读分析产物，没有修改项目源码，没有改动 generated data、日志、缓存或因子库。
