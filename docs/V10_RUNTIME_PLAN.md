# V10 自动运行接线与验收

本文件记录独立 V10 接线，不代表自动影子盘恢复、真实校准通过或 GitHub 覆盖验收完成。接线代码及隔离测试属于工程准备；实际来源资格、完整交易日校准和连续运行验收仍是独立门禁。不得从测试生成的 PASS 推断真实运行合格。

## 已实现入口与工具

- `market_data/slot_aligned_capture_runtime.py`：真实分钟网格调度，V10运行合同和实现清单绑定其源码。跨分钟返回只重新采当前槽，不补历史；永久时钟故障或调度状态不可核验时readiness为503。完整242仍须真实未来交易日验收。

- `frontend-v2/backend/v10_app.py` / `v10_runtime.py`：独立启动、三 worker、运行状态、旧写入保护和 Provider 配置锁。没有 `.env` 的 `QA_V10_DECLARATION` 时，全部自动交易与采集关闭；无历史版本回退。
- `market_data/v10_provider_maintenance.py`：新 run 专属锁、候选目录、证明、状态及切换事务。旧 watcher 不参与；失败保持或恢复原路径，未完成事务阻止继续运行。
- `scripts/freeze_intraday_v10.py`：从原始资格与校准证据生成全新、只读声明，要求未来交易日和独立路径。失败移除本次新建声明；不创建账本、不改环境、不启用 worker。
- `scripts/verify_v10_runtime_readiness.py`：只读核对运行编号、声明哈希、三个 worker 和当前到期采集历史。成功仅为 `READY`，始终 `autoShadowAccepted=false`、`releaseAccepted=false`。
- `scripts/run_v10_backend_supervised.sh`：显式选择 V10 入口、独立预检路径；现有 r2 supervisor 不会因此自动切换。

在真实来源资格和完整日校准通过之后，使用冻结工具的 `--help` 获取参数；首个信号日必须是声明当天之后、校准日之后的冻结交易日。工具不会为未完成的证据生成有效声明。

启用时只在后端 `.env` 中选择已经冻结的 `QA_V10_DECLARATION`，再由受控 supervisor 切换到新脚本。首次选择根 Provider 同样经过完整资格核验与事务切换。不要在当前 r2 校准未完成时替换服务，也不要直接复用旧账本路径。当前登录内的子进程重启证明不等于注销或整机重启证明。

浏览器在同一个研究影子盘页面识别 `CALIBRATION_ONLY` 与 `FORWARD_RUNTIME`。Forward 模式单独展示配置、worker健康和未验收状态，复用原盘中账户与证据卡片；不再调用被保护的旧日频写入/引擎接口。状态失败或运行编号不匹配时不展示成功审计证据。

## 启用之前

- 保持现有冻结文件、旧声明及旧实验记录不变；不启用旧日频 research watcher。
- 来源资格必须从原 `measuredAt` 和 realtime/reference/candidate 三份原件重建，绑定 production V4 实现、冻结 root 和精确 301 符号集合。
- 校准须重新核对完整 242 槽、固定质量门、生产实现及全部原始证据；不能复用旧 V3/旧校准结果。
- 实际新 run 的首个信号日必须晚于声明日和校准日，且位于冻结交易日历中。当前安排不能提前视作已验收。
- 已提供 `validate_v10_declaration(path)` 和 `scripts/verify_intraday_v10_declaration.py --declaration <新声明路径>`。函数及 CLI 均不实例化 coordinator、不建 SQLite 或 WAL/SHM、不启动 worker。既有 DB 仅以 `mode=ro&immutable=1` 校验自身 metadata；ownership 只在 WAL 中时失败关闭。
- CLI 成功仅返回 `DECLARATION_QUALIFIED`，失败返回 `NOT_READY`；始终返回 `autoShadowAccepted=false`、`runtimeNotTested=true`。它只检查声明及绑定证据，不代表 runtime readiness，也不创建实际声明。应在已激活 `scripts/qa_env.sh` 环境中运行。

## 新入口与生命周期

新增独立入口，直接复用冻结 `app.py` 的 API 和 settings 安装器；不要直接导入会安装仅校准禁用器的 `local_app.py` 作为 forward 入口。

可以包装 `backend._app_lifespan`，但须在进入 lifespan 前明确绑定配置：旧 research helper 永远 disabled；capture helper/生产采集实现与 shadow helper/coordinator selector 均指向新 V10 声明和已验收数据。任何绑定未知、历史协议误选或 preflight 未通过都不启动 forward worker。

Provider maintenance 另建 task、锁、status 文件和 health 状态，绑定新 run。不要把其状态写进旧 research registry/automation records。新增健康状态必须进入 API 就绪判断，异常须有明确状态，不能只依赖“进程还活着”。

关闭旧 watcher 并不等于关闭全部旧写路径。现有 research-shadow state/realtime 路由会构造旧 coordinator，signals/generate/run 还可触发旧操作。新入口须给旧写入口加禁用门，并使用冻结只读状态展示历史；验收期间核对受保护旧文件清单前后字节不变。

## 独立 provider maintenance

禁止复用 `unattended_provider_automation.advance_official_provider` 和 `run_cycle_and_record`：前者也会创建旧日频 lineage/读取旧 coordinator，并存在历史漂移后切换分支；后者还会写旧 checkpoint/evidence。

新 worker 只复用官方 manifest 查询、下载、`load_release_manifest` 和 `install_release_provider` 等数据获取原语，候选目录、proof、锁和 status 均独立。顺序为：

1. 下载并校验官方 release 到新候选目录，不改当前有效 provider。
2. fresh 读取候选 provider metadata、实际文件哈希和精确冻结 universe。
3. 验证冻结 root 到候选的 calendar/membership/factor 历史前缀，并验证此前已接受后继的 feature tree 未被改写；沿用原历史/因子门，不能只相信缓存 proof 或 ADVANCED 标签。
4. 所有验证通过后才记录新 run proof 并切换有效 provider；失败保持原 provider，输出明确 BLOCKED/FAIL，不回填旧 ledger。

原始 release 若把已核验的成分修正回滚，必须拒绝。若未来需要派生 successor，须有独立官方 membership 原件及 provenance，不得静默覆盖 instruments，也不得拼接旧 feature 前缀来制造 PASS。基准历史字段补齐导致前缀变化时同样失败关闭，不能自动“修复”历史。

V5 结算仍通过 core 的 `discover_latest_provider` 读取全局有效 provider。仅新增 run 内 pointer 不会自动改变结算输入。最小接线可在严格证明后使用受控 runtime provider switch，并关闭旧研究 worker；若改用 scoped selector，须明确实现依赖注入，不能临时 monkeypatch core 全局函数。

## UI/API 语义

`ResearchShadowPanel` 根据 `CALIBRATION_ONLY` / `FORWARD_RUNTIME` 明确区分模式。Forward 接线提供 `configured`、`shadowEnabled`、worker health 及未验收阶段；API endpoint 与前端类型同步。

不能在 shadow 已启用时仍显示“开关异常/自动决策关闭”，也不能因未提供新 endpoint 而把未知状态隐藏。未验收、配置启用和实际 worker 健康是三个不同事实，应分别展示。复用既有面板，不增加重复页面或重复开关，并验证窄屏和 iPad。

## 真实运行验收（仍待完成）

- 真实来源资格和完整新交易日校准通过，随后冻结独立 V10 run。
- 新入口启动/重启、三 worker 绑定、独立 maintenance health/status 和 API/UI 一致。
- provider 无新版本、合法 append、成分回滚、历史因子变化、超时/下载失败均实际失败关闭或正确推进。
- 两阶段决策、执行绑定、次日官方数据结算、重启恢复和 evidence checkpoint 连续通过。
- 不生成追溯信号，不改变旧受保护文件；无秘密进入仓库或日志。
- 全部通过后才执行用户已授权的篮球仓库覆盖及 QuantaAlpha 改名；本方案不执行发布。
