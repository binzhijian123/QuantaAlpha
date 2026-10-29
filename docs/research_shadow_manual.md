# QuantaAlpha 研究影子盘与实时数据使用手册

更新日期：2026-08-13

本手册对应前端“回测 > 研究影子盘”。这是一个与正式模拟盘完全隔离的前向研究账户，用于观察已冻结但历史验证为 `REJECTED` 的 DeltaES1 候选。它不能证明或保证盈利，也不会自动晋级为正式模拟盘。

## 1. 当前状态

| 项目 | 当前值 |
| --- | --- |
| 运行 ID | `research_shadow_20260812_deltaes1_causal_v2` |
| 使用域 | `RESEARCH_SHADOW` |
| 历史结论 | `REJECTED` |
| 正式模拟盘资格 | 否 |
| C1 | Alpha158(20) + DeltaES1，快照 `0f1495c1f0a3c149` |
| B0 | Alpha158(20)，快照 `323478f446e48967` |
| 首个冻结信号 | 2026-08-11 收盘，各 50 只 |
| 首个允许撮合日 | 2026-08-12 开盘 |
| 当前官方行情截至 | 2026-08-12 |
| 当前状态 | 等待下一交易日官方日线 |
| 盘中报价刷新 | 每 30 秒，腾讯主源、Sina 批量备用 |
| 盘中研究快照 | 每 60 秒采集 CSI300 + 沪深300指数，当前策略不使用 |
| 正式日线守护 | 后端每 15 分钟检查一次 |
| 执行合同 | 因果开盘 v2，SHA256 `0164e1e672a2445b2cfc76788cfacf37984438a8fcae39f936fa8f6ca6b1a4d2` |
| 执行实现 | 9 个关键文件 + Python/依赖版本，SHA256 `2ba07fe3119708a325e9cfff4d7d9d90212a0dde198da9ecbe64b8f1860c4f25` |
| 审计链 | 5 条，链头 `47cdc4ba` |
| 证据检查点 | 3 条，链头 `cfa291bf` |

C1 和 B0 各使用独立的 100 万元虚拟账户，但共用交易日、费用和执行规则。两边必须成对生成信号、成对撮合；任意一边失败都会阻止或回滚整次操作。

### 1.1 不可变执行合同

首笔真实撮合前，系统已把实际引擎参数写入独立账本元数据，并追加 `EXECUTION_CONTRACT_FROZEN` 审计事件。以后每次启动都会重新从声明构造配置并与已存合同及 SHA256 比较；缺字段、哈希变化或参数漂移都会失败关闭。若账本已经有运行、订单、成交或 NAV，则禁止事后补建缺失合同。

| 参数 | 冻结值 |
| --- | --- |
| 成交基准 | 唯一下一交易日开盘价 |
| 滑点 | 开盘价买入 `+10bp`、卖出 `-10bp`；作为保守有效成交价，不读取当日高低价钳制 |
| 买入综合费率 | `5bp` |
| 卖出综合费率 | `15bp` |
| 单笔最低费用 | `5` 元 |
| 买入交易单位 | 主板/创业板 `100` 股整数手；科创板最低 `200` 股，超过部分按 `1` 股递增；零股只允许整仓卖出 |
| 交收 | `T+1` |
| 冻结涨跌停阈值 | 主板开盘变化 `9.5%`、创业板/科创板 `19.5%` 的保守守卫；触发时禁止该证券全部交易 |
| 上市初期 | 上市交易日龄未知或前五个交易日一律拒绝撮合 |

v2 的开盘成交判定只允许读取 `symbol/open/previousClose/factor/listingSessions`。同一交易日的 `high/low/close/volume` 被明确列入禁止用于开盘决策的字段；它们只能在收盘后用于净值和数据完整性。声明中的费用、手数、板块阈值、上市初期策略和滑点均为显式字段，不使用兼容默认值。

旧运行 `research_shadow_20260811_deltaes1_v1` 会用同一交易日收盘涨跌幅判断开盘订单，并用当日高低价钳制滑点，因此不具备开盘因果性。它已在 C1/B0 均为零运行、零订单、零成交、零持仓、零 NAV 时追加 `RUN_RETIRED_BEFORE_EXECUTION` 事件并退役；原始声明、快照、50 只待执行目标和历史证据均保留，不会被 v2 改写。v2 的候选、模型、Provider、信号日、首个交易日和两臂目标与 v1 完全一致，声明同时如实记录了 v1 排名已在 v2 声明前可见。

这套买卖费率是研究影子盘的保守综合成本假设，不是券商交割单中佣金、印花税和过户费的逐项拆分。不同账户佣金可能不同；若以后按真实券商合同建模，必须在新运行首个信号前完整声明并冻结，不能修改当前运行。

板块规则依据 2026-07-06 起施行的[《上海证券交易所交易规则（2026年修订）》](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)和[《深圳证券交易所交易规则（2026年修订）》](https://investor.szse.cn/lawrules/rule/trade/t20260424_620190.html)。合同使用 `9.5%/19.5%` 而不是精确 `10%/20%` 作为保守触发守卫，以覆盖最小报价单位四舍五入；上市前五个交易日等无涨跌幅情形不尝试估算成交。

### 1.2 不可变执行实现

只冻结参数不足以保证前向结果可比：同一个 `engineModelVersion` 下，代码、Python、SQLite 或数值库变化仍可能改变成交和收益。因此 v2 在首笔真实撮合前又追加了 `EXECUTION_IMPLEMENTATION_FROZEN` 审计事件，并把以下内容合并为实现 SHA256：

- 9 个关键文件：`execution_contract.py`、`market_data.py`、`paper.py`、`research_shadow.py`、`research_shadow_automation.py`、`signal.py`、`investment_data_release.py`、`provider_lineage.py`、`qlib_provider.py`。
- CPython `3.10.20`、SQLite `3.53.2`、LightGBM `4.6.0`、NumPy `1.26.4`、pandas `2.3.3`、pyqlib `0.9.7`、scikit-learn `1.7.2`。

当前实现 SHA256 为 `2ba07fe3119708a325e9cfff4d7d9d90212a0dde198da9ecbe64b8f1860c4f25`。活动 v2 每次启动都重算并严格比对；任一关键文件或运行时版本漂移都会失败关闭，不会继续记录不可比的收益。需要修改实现时，必须退役当前运行并在新运行首个信号前重新声明和冻结。已退役运行仍按其历史清单验证可读性，不要求与当前代码一致。

## 2. 数据更新分层

### 2.1 盘中实时层

页面每 30 秒通过 `a-stock-data` 获取公开行情，并在同一轮询中重新读取 C1/B0 正式账本和正式日线守护状态。系统先批量读取 `a-stock-data/tencent-public`；腾讯请求失败、只返回部分代码，或某代码的价格缺失、非有限、非正数、已标记停滞时，仅对这些代码读取 `a-stock-data/sina-public`。腾讯的其他有效代码不会被替换。两条实时线路都不可用或某只持仓没有有效报价时，该持仓使用最近正式收盘价估值。守护完成新交易日撮合后，持仓、订单、收益和证据状态会自动出现在页面，无需手工刷新。页面显示实际使用的腾讯、Sina 或混合来源，以及：

连续交易和集合竞价时段还会逐代码检查报价时间，门槛为 120 秒。一条新鲜的指数报价不能掩盖某只持仓已超时；超时持仓回退至正式收盘价，页面标记两个臂合并后的延迟代码数。午休、收盘和非交易日不应用该盘中时效门槛，避免把正常静止行情误报为延迟。

- 行情阶段：盘中、已收盘、延迟或不可用。
- 最近报价时间。
- 持仓临时估算净值和报价覆盖率。

这一层始终标记为 `provisional=true`、`persisted=false`、`evidenceEligible=false`。主备切换不改变这三个标志；它不写 Qlib，不改账本，不生成信号，也不计入 20/60/120 日收益证据。

### 2.2 正式日线层

信号、开盘撮合、收盘净值和沪深 300 基准只读取版本化 Qlib Provider。优先使用 `chenditc/investment_data` 的每日完整发布。

每次升级都必须通过追加式历史校验：

1. 新日历必须完整保留上一版本的全部交易日，只能在末尾追加。
2. 上一版本覆盖期内的 CSI300 成分历史必须一致。
3. `open/high/low/close/vwap/volume/factor` 历史值必须逐值一致。
4. 每一级证明都绑定运行 ID、冻结声明 SHA256、Provider 指纹和文件 SHA256。
5. 证明链必须能逐级回溯到 2026-08-11 冻结 Provider。

任何检查失败时，脚本会在切换运行时之前终止；现有 Provider 和账本保持不变。

### 2.3 盘中前向研究数据集

后端现在会在页面关闭时仍持续采集盘中研究快照。股票池是当前正式 Provider 最新日期的 300 只 CSI300 成分股，另加 `000300.SH` 基准，共 301 个代码。交易日期只从 Provider 的 `day_future.txt` 读取，不按工作日猜节假日。每个交易日的首个不可变时槽会锁定当日 Provider；如果正式日线更新器在盘中切换 `.env`，后续时槽仍使用首个时槽绑定的目录，下一交易日才采用新 Provider。

生产采集只在集合竞价/早盘和午盘窗口内请求行情，默认每 60 秒一次。行情按腾讯、Sina、东方财富三层顺序逐级补缺；后一层只请求前一层缺失或无效的代码。开市前若上游还在返回上一交易日，状态为 `WAITING_QUOTES`，不落盘；至少有一条报价切换到当日后才保存当批。部分返回不会被伪装成完整数据，每批都记录请求数、返回数、当日数、可用数、缺失代码和意外代码。

页面和审计中的“覆盖率”只计算报价日期为当日、价格有限且为正、未标记停滞、报价时间不早于可用时间 120 秒以上且不超前 5 秒的代码。原始返回覆盖、当日覆盖和当日可用覆盖分别保存，不能再用上一交易日的返回行把覆盖率显示成 100%。日期或时效不合格的记录会继续触发下一层备用源；三路均失败或全部记录不可用时失败关闭，不落盘，也不把盘后延迟行情冒充实时行情。

每批同时保存 `captureRequestedAt`、`responseReceivedAt` 和 `availableAt`。研究代码只能从 `availableAt` 之后使用该批数据，不能把采集请求开始时间当成行情已知时间。`high/low/amountCny` 是截至报价时点的日内累计字段，不是一分钟 OHLCV；未来编译器不得把相邻快照直接冒充分钟 K 线。

快照路径：

```text
data/market/a_share/intraday/a_stock_data_csi300/
├── status.json
├── slots/YYYY-MM-DD/slot_<sequence>_HHMMSS_<record-sha12>.json
├── snapshots/YYYY-MM-DD/snapshot_HHMMSS_<content-sha12>.json.gz
└── audits/YYYY-MM-DD/calibration_<report-sha12>.json
```

每批是独立的只读 gzip JSON，旁边有归档 SHA256 侧车；JSON 内部同时绑定股票池、Provider 日历/成分文件、采集器、`a-stock-data` 适配器和 Python/依赖版本哈希。快照和侧车写完后，系统最后追加同样只读的时槽提交记录；记录按序号和前一条 SHA256 串成日内链。删除、篡改、重复时槽、同日实现/Provider/股票池漂移都会失败关闭。同一时间槽重启时复用已验证提交，不重复请求；崩溃后留下但没有提交记录引用的快照会作为孤儿报告，不会被静默编入数据集。系统不会覆盖或自动删除这些用户数据。

这一层固定标记为 `usageRealm=INTRADAY_RESEARCH_RAW`、`evidenceEligible=false`、`trainingEligible=false`、`officialDailyEligible=false`。它的作用是从现在开始积累新的盘中原始研究样本；在未来另建预注册因子、完整性规则和新运行之前，它不能被当前 C1/B0 训练、交易或收益证据使用。

质量审计只检查 09:30-11:30、13:00-15:00 共 242 个一分钟时槽：时槽覆盖、当日可用覆盖、核心字段覆盖、基准覆盖、最长缺口、报价时效、文件/提交链、强制快照、孤儿文件和单日绑定一致性。它明确禁止输出特征值、收益、股票排名或盈利结论。15:05 后后台自动冻结唯一日审计报告；`PASS` 只表示数据工程可用，不表示因子有效。

2026-08-12 的 v1 校准已冻结为 `FAIL`。虽然捕获 `223/242` 个预期时槽、有效报价和核心字段覆盖中位数均为 `100%`，但 09:47-10:05 腾讯与 Sina 同时连接失败，形成 19 个连续缺失时槽，超过预声明上限 3。不可变报告 SHA256 为 `8436f6a39f0fa71471572e9bdb3fed156cd55c697266338f373dacf49d4ebbec`。该日不得用于盘中特征、收益或候选选择，也不得通过降低门槛改写结论。

修复只增加东方财富第三备用源和严格日期/时效校验，没有放宽任何质量门。2026-08-13 在当天生产报价出现前冻结了 `configs/intraday_capture_calibration_v2_20260813.json`，声明 SHA256 为 `f458bebdbccfa15cb1dbe44b2d86d986b7cd400f233569985ec0334d179ed552`。后台在 15:05 后自动冻结最终 **`PASS`**：`242/242` 个预期时槽，时槽、基准、可用报价中位数/P10 和核心字段覆盖均为 `100%`，最长缺口为 0；强制记录、时间/范围错误、孤儿快照和完整性错误均为 0。腾讯贡献 72,839 条可用记录，Sina 补缺 3 条。报告语义 SHA256 为 `743820835eb17aff98b5fbfd9e0b5b0154cf976794928dcde2724186cb1db351`；只读文件 `calibration_743820835eb1.json` 及侧车的文件 SHA256 为 `f4ac455d1cc3ea000cc4c61bc2ad7887683f077ea2c76ad962ba9a4ebe0286c8`。报告仍为 `CALIBRATION_ONLY`、`profitEvidenceEligible=false`，且不含特征值或收益。开盘前 301/301 连通性探针因报价日期仍为 2026-08-12，未计入 8 月 13 日校准。

校准通过后，也不能修改当前 DeltaES1/B0。下一步只能在后续交易日开始前另建一个候选、一个基线和独立运行，并重新冻结公式、方向、形成时点、标签、费用与停止条件。校准失败时先修复数据链并建立新版本，不允许降低门槛来让首日通过。

查看状态：

```bash
source scripts/qa_env.sh
python scripts/capture_a_stock_data_intraday.py --status
python scripts/audit_a_stock_data_intraday.py
```

只有连通性诊断时才使用 `--force`，并应指向 `/tmp` 等隔离目录。强制快照仍明确不可入证据，不得写入上述生产目录。

收盘后手工复核或冻结报告：

```bash
python scripts/audit_a_stock_data_intraday.py --finalize
```

收盘前该命令只返回 `IN_PROGRESS`，不会提前写审计文件；收盘后返回 `PASS` 或 `FAIL`，两者都不属于收益证据。

## 3. 打开页面

生产服务未运行时：

```bash
cd /absolute/path/to/user/Documents/QuantaAlpha-main
source scripts/qa_env.sh
python scripts/manage_research_shadow_supervisor.py start
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)，依次进入“回测 > 研究影子盘”。FastAPI 直接提供已构建前端，不需要 Vite 开发服务器。`frontend-v2/start.sh` 和 `http://localhost:3000` 仅用于前端开发。页面不提供手工股票、日期、重置或历史回填入口。

## 4. 自动每日操作

只要后端持续运行，研究影子盘守护会每 15 分钟检查一次 `chenditc/investment_data` 官方发布。每次轮询先只读取 GitHub 发布元数据和小型 `qlib_bin.manifest.json`，以 manifest 的 `target_trade_date` 判断数据实际覆盖到的交易日。目标交易日不晚于当前 Provider 时，不下载约 560 MB 的归档、不重复计算大文件哈希，也不执行任何账本动作；只有目标交易日更新后，才下载归档并执行完整的资产 SHA256、历史连续性和 Provider 追加式谱系校验。发布标签可能落在周末而目标交易日仍是上一个交易日，不能直接比较标签日期。

GitHub 发布元数据和小 manifest 的只读请求最多尝试 3 次，瞬时网络错误后分别等待 1 秒、2 秒重试；普通 4xx 客户端错误会立即失败，不会盲目重试。三次均失败时，本轮状态写为 `ERROR`，账本和 Provider 不变，15 分钟后自动进入下一轮。大归档下载不在该短重试中，避免网络不稳定时连续重复下载约 560 MB 文件；下载失败留下的临时文件会删除，下一轮重新校验后再开始。

页面“盘中行情自动更新”区域会显示守护状态、上次检查时间和下次检查时间；页面自身每 30 秒同步最新账本、临时报价和守护结果。盘中报价优先使用 `a-stock-data/tencent-public`，缺失、无效或交易时段内超过 120 秒的单只证券报价由 `a-stock-data/sina-public` 补齐，仍不可用时回退到正式日线收盘价。该报价链只用于页面临时估值，始终标记为 `provisional=true`、`persisted=false`、`evidenceEligible=false`，不会写入训练数据、信号、订单、成交、正式净值或收益证据。

因此本系统采用两层更新：盘中约 30 秒更新一次临时行情；盘后由版本化官方日线发布更新不可变 Provider。训练、信号生成、模拟撮合、收盘净值和盈利验证只使用后一层。

正式日线状态还会使用当前 Provider 自带的 `calendars/day_future.txt` 判断下一应到交易日，节假日不按普通工作日猜测。页面显示下一应到日期和宽限截止时间；若该交易日结束后的次日 `00:30` 仍未更新，状态变为“正式日线已延迟”并显示累计延迟交易日数。该状态只报警，自动账本继续保持不动，不会拿盘中临时报价补正式日线。

Provider 维护和旧研究影子盘账本现在是两个明确边界。每轮先完成当前活动 Provider 上已经合法、尚未提交的唯一待办，再查询和推进官方数据。若官方最新发布一次包含两个或更多新增交易日，后台仍会验证整段日历前缀、历史成分、完整特征树和谱系后切换 Provider，使盘中数据和后续任务不会永久停在旧日期；但更新后的旧日线影子盘会进入 `MISSED_EXECUTION` / `REVIEW_REQUIRED`，不再执行账本动作，绝不根据已经可见的结果补撮合或补信号。状态中的 `maintenanceMode=UNATTENDED_PROVIDER`、`recoveryMode=MULTI_SESSION_NO_LEDGER_BACKFILL` 和 `ledgerBackfillAllowed=false` 用于审计这条边界。

如果一个经过发布包 SHA256、文件树、日历终点和 CSI300 数量校验的新 Provider 修订了旧历史，系统不会伪造“只追加”证明。旧研究影子盘会永久标记 `providerHistoryInvalidated=true` 并进入 `REVIEW_REQUIRED`，保留切换前最后一个证据检查点；正式数据则切换到新 Provider，状态记录 `ADVANCED_WITH_HISTORY_DRIFT` 和具体漂移类型。后续轮询进入 Provider-only 维护模式，不再加载、结算或新增旧账本证据；相邻新 Provider 仍必须通过完整追加校验。归档损坏、manifest 不一致、日历未前进或其他非历史修订错误仍会失败关闭。

状态首次进入 `REVIEW_REQUIRED` 时会发送一次固定的 `RESEARCH_SHADOW_REVIEW_REQUIRED` 关键告警；后续 15 分钟轮询保持同一状态时不重复发送。告警不包含股票、价格、上游错误正文或密钥。

每次检测到恰好新增一个正式交易日时，后台按固定顺序执行：

1. 安装新的不可变 Provider。
2. 校验日历、CSI300 历史、模型字段和完整相关特征文件树。
3. 切换运行时 Provider。
4. 按唯一下一交易日开盘价成对撮合 C1/B0。
5. 按当日收盘价更新净值和基准。
6. 计算 C1/B0 最大回撤；任一账户达到 15% 停止线时写入审计链并停止账本动作。
7. 未触发停止线时，冻结当日收盘信号并等待下一个交易日。

Provider 升级、页面按钮和自动账本操作共用跨进程文件锁，不会并发写入同一账本。

### 4.1 立即执行一次完整检查

不需要等待下一次 15 分钟轮询时，可运行：

```bash
cd /absolute/path/to/user/Documents/QuantaAlpha-main
source scripts/qa_env.sh
python scripts/research_shadow_daily.py
```

常见结果：

| 状态 | 含义 | 操作 |
| --- | --- | --- |
| `NO_NEW_SESSION` | 官方 manifest 的目标交易日没有晚于当前 Provider；发布标签本身可能更新 | 等待，不执行账本 |
| `ADVANCED_AND_PROCESSED` | 日线升级、配对撮合和新信号冻结完成 | 核对页面和审计链 |
| `RETIRED` | 旧运行已在首笔成交前退役 | 只读保留，使用声明中的替代运行 |
| `REVIEW_REQUIRED` | 已错过执行日、配对异常或触发 15% 回撤停止线 | 停止账本动作，建立新声明或复核账本；正式数据仍可继续检查更新 |
| `ADVANCED_WITH_HISTORY_DRIFT` | 已校验发布修订了旧历史，正式数据已推进，旧实验永久失效 | 新建预声明运行；不得补旧账本 |
| 命令报错 | 下载、归档、结构或非历史修订校验失败 | 保留旧 Provider，先排查，不绕过 |

只安装并校验指定发布、不自动撮合时：

```bash
python scripts/advance_research_shadow_provider.py --release-tag YYYY-MM-DD
```

### 4.2 页面手工执行是备用入口

正常情况下后台会自动完成。若页面恰好处于“可执行下一交易日”，也可以点击“执行唯一下一交易日”；文件锁会保证页面与后台只有一方执行成功。

1. 核对官方行情截至日正好是待执行信号后的第一个交易日。
2. 点击“执行唯一下一交易日”。
3. 系统按该日开盘价、冻结费用、板块申报数量、T+1 和因果涨跌停约束，成对撮合 C1/B0。
4. 系统按该日收盘价更新两边净值和沪深 300 对照。

如果 Provider 已越过唯一允许的交易日，系统会进入 `MISSED_EXECUTION` 并禁止回填；必须建立新的预声明运行，不能修改数据库日期。

### 4.3 冻结当天收盘信号

当当天成对撮合完成后，页面状态变为“可生成最新收盘信号”。点击“冻结最新收盘配对信号”，为下一个交易日保存 C1/B0 目标及哈希。

后台守护通常会在同一循环自动完成这一步；按钮用于中途网络错误后的安全续跑。

已有待执行信号时不能重复生成。信号生成和撮合都不接受用户提交日期，因此不能用旧日数据伪造前向记录。

### 4.4 运行连续性要求

- 后端和电脑必须在官方新日线出现时保持运行，或在下一个交易日结果可见前手工运行完整检查。
- 若活动 Provider 一次落后两个或更多交易日，守护仍可把正式数据安全追到最新发布；当前旧影子盘同时失去前向资格并进入人工复核，绝不会逐日补做已经可见结果的历史信号。
- 电脑休眠、断网或后端停止导致错过唯一执行日时，当前运行进入人工复核；不能通过修改日期或数据库恢复“前向”资格。

### 4.5 后端监督与恢复

当前后端由仓库内的 Screen 监督器托管。Python 后端异常退出后会自动拉起；它仍只启动 FastAPI 中唯一的研究影子盘 watcher，不会创建第二套撮合调度器。短时间连续失败时采用 5、10、20 秒递增的指数退避，最长 300 秒；子进程稳定运行 900 秒后，下一次故障恢复为 5 秒起步，避免配置错误或资源故障造成高频重启。

监督器每 10 秒读取 `/api/health/ready`，连续 3 次失败才重启。readiness 同时验证响应 PID 属于监督器子进程，并检查正式日线 watcher 与盘中采集 worker：任务退出、空闲心跳过期或单次操作超时会返回 HTTP 503；正常 `NO_NEW_SESSION`、午休、收盘等待和瞬时上游错误不会仅因业务状态触发重启。正式日线单次周期上限为 7200 秒，以容纳完整归档下载和校验；盘中周期上限为 300 秒。TERM 后 5 秒仍未退出时升级为 KILL。

校准尚未终态时，FastAPI 每个采集周期都会从不可变时槽链重新生成只读摘要，因此页面上的“有效时槽”会随上午和下午采集推进；15:05 前只更新可变的 `status.json`，不会提前生成或覆盖最终审计文件。监督器不会自动删除或轮转现有日志，因为这些属于用户生成的运行记录；启用有损保留策略前必须先明确保留期限。

监督器告警包含后端生命周期 `BACKEND_UNHEALTHY`、`BACKEND_EXITED`、`BACKEND_RECOVERED`，防休眠状态 `SLEEP_GUARD_FAILED`、`SLEEP_GUARD_RECOVERED`，官方日线更新失败/恢复与逾期/恢复，以及固定的盘中影子盘冻结、结算、漏跑、风险停止和周期故障/恢复事件。每次事件先追加到权限 `0600` 的 `data/results/research-shadow-supervisor-events.jsonl`，再尝试投递；事件本身不包含 webhook 地址、API 密钥、行情值、股票或上游错误正文。同一持续后端故障或业务状态只在边沿通知，恢复后再通知一次。事件日志不会自动删除。

`.env` 已启用 `QA_BACKEND_PREVENT_IDLE_SLEEP=true`。监督器只持有一个 `/usr/bin/caffeinate -i -w <supervisor-pid>` 子进程；该进程意外退出时会自动重建，不会创建第二套行情、信号或撮合调度器。管理器 `status` 会显示 `sleepGuardConfigured`、`sleepGuardActive` 和 `sleepGuardPid`。这只阻止主管存活期间由空闲触发的系统睡眠，显示器仍可休眠；合盖、手动睡眠、关机、断电、电池耗尽和重启恢复不在保证内。交易日优先接通电源，并保持机器开机、用户会话和上游网络可用。

2026-08-13 收盘后已加载并实机验证这条链：受监督后端 PID `13159` 被单独终止后，同一 supervisor 自动拉起 PID `13242`，事件文件依次写入 `BACKEND_EXITED`、`BACKEND_RECOVERED`。文件权限为 `0600`，字段白名单和密钥样式扫描通过；交易审计仍为 5 条、链头 `47cdc4ba...`，证据检查点仍为 3 条、最新 SHA256 `cfa291bf...`。桌面通知命令没有记录投递错误，但是否被 macOS 视觉展示取决于本机通知设置；远程 webhook 仍未配置。

本机 `.env` 已启用 macOS 桌面通知；它是尽力投递，要求当前用户有图形登录会话且系统允许通知。远程告警默认未配置；如有自建的通用 JSON 接收器，可填写 HTTPS 地址，明文远程 HTTP 会被拒绝：

```dotenv
QA_BACKEND_DESKTOP_NOTIFICATIONS=true
QA_BACKEND_ALERT_WEBHOOK_URL=https://your-alert-relay.example/hooks/...
```

Slack、企业微信、钉钉等平台通常要求各自的消息结构，不能假设它们的原生 webhook 能直接接收当前通用事件；应使用适配器或自建 relay。投递超时、权限拒绝或 webhook 失败只会写入不含地址的错误类型，不会阻挡后端终止、退避和重启。

`.env` 包含模型和数据源凭据，当前文件权限固定为 `0600`。唯一生产主管会先设置 `umask 077` 再启动后端，因此自动 Provider 切换创建的临时 `.env` 和替换后的文件都保持 `0600`；主管和防休眠 PID 文件也为 `0600`。不要绕过主管入口运行自动更新器，也不要把 webhook 地址或其他密钥写入命令参数、截图、运行日志或告警事件；只写入本机 `.env`。

```bash
cd /absolute/path/to/user/Documents/QuantaAlpha-main
source scripts/qa_env.sh

# 查看状态
python scripts/manage_research_shadow_supervisor.py status

# 启动、重启、停止
python scripts/manage_research_shadow_supervisor.py start
python scripts/manage_research_shadow_supervisor.py restart
python scripts/manage_research_shadow_supervisor.py stop
```

不要直接用 `screen -X quit` 停止监督器；Screen 退出不保证其子进程同步退出。管理脚本通过 PID 所有权校验先终止监督器和后端，再清理 Screen，不会杀死占用 8000 端口的无关进程。

仓库还提供可选的登录自启动管理器。Screen 与 LaunchAgent 现在共用同一个 readiness 监督脚本：

```bash
python scripts/manage_research_shadow_launch_agent.py install
python scripts/manage_research_shadow_launch_agent.py status
python scripts/manage_research_shadow_launch_agent.py uninstall
```

本机实测 macOS 拒绝 LaunchAgent 访问 `Documents` 下的项目，当前没有安装该服务。安装器遇到此情况会自动 `bootout` 并删除 plist，不会留下重启循环。除非以后明确授予后台文件访问权限，否则继续使用监督式 Screen。它可以处理 Python 崩溃、终端关闭和空闲睡眠，但不能跨电脑重启，也不能阻止合盖、手动睡眠、关机、断电或电池耗尽。

### 4.6 一致性证据检查点

守护在每日操作前后都会检查证据链。每个有实质变化的状态保存四个只读文件：SQLite Backup 一致性快照、快照 SHA256、JSON 摘要和 JSON SHA256。JSON 记录冻结声明、模型快照、活动 Provider 及谱系、全部账本表摘要、C1/B0 账户、信号、订单、成交、持仓、费用、净值、基准和审计头；下一条 JSON 会绑定上一条 JSON 的文件 SHA256。

账本使用 SQLite WAL，因此不能只对 `.sqlite3` 主文件求哈希。检查点在同一操作锁下调用 SQLite Backup API，把主文件和已提交 WAL 事务合并为一致快照，再校验 `integrity_check`、全表逻辑摘要和审计链。普通 15 分钟空轮询会返回 `REUSED`，不会重复保存同一状态。

只有以下变化可以生成下一条检查点：

1. 账本逻辑摘要变化，同时审计事件数增加，且旧审计头仍位于新账本的原序号。
2. 账本不变，但活动 Provider 已通过追加式谱系校验并推进到新交易日。

账本变化但审计头未前进、旧审计链被替换、Provider 无证明切换、JSON/SQLite/侧车被修改、序号断裂或目录出现未引用快照时，守护会失败关闭并停止下一次账本动作。

手工验证现有链：

```bash
source scripts/qa_env.sh
python scripts/verify_research_shadow_evidence.py
```

为当前合法状态创建或复用检查点后再验证：

```bash
python scripts/verify_research_shadow_evidence.py --create
```

v2 第 1 条基线：JSON SHA256 `06810ddac83f448d5ab0d4d2e7014fc2dd9f11832df4d25f0a8c70fde2843b83`，SQLite Backup SHA256 `bc0747ab1014c54f4674306d795836911c2203e845f4f963b43bf4d62ca12fae`，绑定 2 条审计事件和因果执行合同 SHA256 `0164e1e672a2445b2cfc76788cfacf37984438a8fcae39f936fa8f6ca6b1a4d2`。

v2 第 2 条当前检查点：JSON SHA256 `49619bf6571fd71b222460b0c25a65791ee8389120e70c3b9878ff5337683b76`，SQLite Backup SHA256 `d6eff8fa1cad2886670736d2309fd11c1e6d9cbce4e8f1899c61bc5969c42ecc`，绑定 3 条审计事件、执行合同和执行实现 SHA256 `2ba07fe3119708a325e9cfff4d7d9d90212a0dde198da9ecbe64b8f1860c4f25`。页面标题旁显示检查点数量和最新 JSON 链头前 8 位，证据区同时显示执行合同和执行实现指纹。

旧 v1 的第 3 条终态检查点继续单独保存：JSON SHA256 `da21b59d3787e3f869dc6720aee130c2e532a828bea361f04ccf5f17e210c157`，SQLite Backup SHA256 `0e681fb4d5becf4b8bafca83400fe2e7b4691431e69efc485b7ab498c6326a25`，绑定 3 条审计事件和退役链头 `a4b71a89...`。该检查点证明 v1 在退役时仍是零成交，不属于盈利证据。

这是本地篡改可检测链，不是第三方公证。拥有整个目录写权限的人理论上可以重写整条链；若未来要向外部提供不可抵赖证明，应把每日最新链头签名或同步到独立的只追加存储。

### 4.7 自主运行验收矩阵

“不需要 Codex”必须拆成可验证的运行条件，不能只看页面能否打开：

- 软件自主执行：行情采集、官方日线检查、Provider 追加校验、既有冻结策略信号、C1/B0 成对模拟撮合、费用与 T+1 记账、净值、审计检查点、worker 健康检查和异常进程重启。关闭页面或 Codex 对话不会停止这些任务。
- 人工治理但不参与每日运行：新因子假设、模型重训、协议或费用变更、策略晋级/退役，以及 macOS 隐私权限审批。系统不会因为一次好收益自动调参、替换模型或晋级策略。
- 当前目标是可独立运行的研究影子盘和模拟交易基础设施，不是无人监管的实盘券商交易。Codex 是开发和验收工具，不属于生产依赖或交易决策链。

当前盘中权威运行是 `intraday_shadow_20260817_t1_reversal_v6`。V5 的首个真实会话已经只读结算并保留；V6 保持策略、成本、时序和因果协议不变，增加经过资格审计的独立 TCP 行情降级与逐条实时契约。当前门禁包括 301 个唯一代码、核心字段覆盖至少 95%、报价时效、基准可用，以及全部入选目标在决策和成交两个时点逐只可用。旧版本数据继续只读保存。该切换提高的是自动运行的数据可靠性，不构成盈利证据；盘中细节以 `docs/intraday_shadow_manual.md` 为准。

| 条件 | 当前结论 | 证据或边界 |
| --- | --- | --- |
| 关闭前端页面 | `PASS` | watcher 和采集 worker 在 FastAPI 生命周期中运行，不依赖浏览器轮询 |
| 关闭 Codex 对话 | `PASS` | 生产进程由 Screen 监督器持有，PID、worker 心跳和自然轮询均独立推进 |
| 锁屏 | `PASS` | 2026-08-13 锁屏期间下午有效时槽从 129 自动推进到 136，301/301 可用且错误为 0 |
| Python 子进程崩溃 | `PASS` | 2026-08-14 实机终止后端 PID `58252`，同一 supervisor 自动拉起 `58918`；恢复后自动前检重新达到 `READY 14/14`，V3 数据源契约和冻结账本证据不变 |
| 终端关闭 | `PASS` | Screen 会话不依赖启动它的终端窗口 |
| 退出登录或电脑重启后重新登录 | `STAGED` | 已生成并完整验证登录项应用 build 5，但当前版本启动回执不存在且未注册；必须先通过 macOS `Documents` 授权并完成真实登录恢复测试 |
| AC 或电池供电、合盖打开时系统空闲 | `PASS_WITH_CONDITION` | supervisor 拥有 `caffeinate -i -w`，空闲睡眠保护已实机自动恢复；电池会持续消耗，仍应接通电源 |
| 合盖、手动睡眠、关机、断电或电池耗尽 | `NOT_GUARANTEED` | 这些状态不会被 idle-sleep guard 阻止，睡眠或断电期间无法持续采集 |
| SIGKILL 或断电时的配对账本原子恢复 | `NOT_PROVEN` | SQLite 单事务和检查点可检测已提交状态，但当前冻结运行不能通过改执行代码补做破坏性故障注入 |
| 网络或上游中断 | `FAIL_CLOSED` | 不伪造行情、不回填已可见结果；下一轮自动重试，但连续缺口可能使当日数据校准失败 |
| 策略稳定盈利 | `NOT_PROVEN` | 只有 1 个真实配对观察日，且历史结论仍为 `REJECTED` |

登录恢复采用单一现有入口，不创建第二套 watcher：

```bash
# 仅构建并检查登录项应用，不注册
python scripts/manage_research_shadow_login_item.py build

# 首次安装会请求 macOS Documents 权限；授权成功后才注册
python scripts/manage_research_shadow_login_item.py install

python scripts/manage_research_shadow_login_item.py status
python scripts/manage_research_shadow_login_item.py uninstall
```

应用位于 `~/Applications/QuantaAlpha Research Shadow Startup.app`。`status` 的 `appValid=true` 只有在签名、plist 身份、build 版本、源码哈希、反编译 AppleScript、Python 路径、supervisor 路径、主脚本和权限声明全部匹配时才成立；`build` 会原子替换发生漂移的同 bundle 应用，但拒绝覆盖无关应用。它只调用幂等的 `manage_research_shadow_supervisor.py start`，不会读取或保存凭据；实际 `.env` 仍只由仓库启动链在本机读取。

macOS 把 `Documents` 视为受保护目录，需要用户明确授权。授权标记现在是权限 `0600` 的结构化启动回执：应用必须先成功启动 supervisor，再由仓库脚本复核应用完整性、当前 boot session、supervisor PID、后端实例和三个 worker，才能原子写入与当前启动源码合同绑定的令牌。空文件、旧版本令牌、旧开机回执或缺失标记都不会被解释为本次开机成功；安装器只有在当前应用实际运行并写出有效回执后才注册登录项。

`status` 中的 `authorizationConfirmed=true` 表示回执匹配当前应用合同；`currentBootLaunchConfirmed=true` 进一步表示回执属于当前 macOS boot session。真实重启验收必须两者都为 true，并同时复核 supervisor、后端和盘中自动前检。首次权限窗口或管理器阻塞超过 60 秒时，启动器会 TERM/KILL 自己创建的管理器并失败退出，不会无限遗留授权探测进程。

### 4.8 下一交易日隔离演练

在等待真实下一交易日之前，可以用当前冻结的 C1/B0 模型和待执行目标预演一次完整生产状态机：

```bash
cd /absolute/path/to/user/Documents/QuantaAlpha-main
source scripts/qa_env.sh
python scripts/rehearse_research_shadow_next_session.py \
  --report /tmp/quantaalpha_research_shadow_rehearsal.json
```

演练会执行以下硬门禁：

1. 从正式 SQLite 使用 Backup API 创建临时账本，不复制可能遗漏 WAL 的主文件视图。
2. 使用 APFS 写时复制创建临时 Provider，并确认它不是指向正式文件的硬链接。
3. 逐项重放当前 C1/B0 冻结目标；任一目标或权重哈希不一致即停止。
4. 模型快照按字节原样复制，快照 ID 和 LightGBM 模型合同必须保持不变。
5. 只追加声明中的唯一下一交易日，校验历史日历、成分和核心特征前缀均未变化。
6. 使用真实 `ResearchShadowCoordinator` 完成开盘撮合、NAV、下一收盘信号和两条临时证据快照。
7. 退出前重新计算正式 `.env`、声明、清单、模型、Provider 特征树、WAL 感知账本逻辑摘要和正式证据链头；任一变化即失败。

临时 Provider、账本、模型副本和证据链默认在命令结束时删除。报告中的所有状态都会强制写成 `evidenceEligible=false`；底层引擎的原始判断只保留在 `engineStateEvidenceEligible` 诊断字段中。合成收益、回撤、订单、成交和 NAV **永远不能**计入 20/60/120 日证据，也不能用于自动切换 Provider 或晋级正式模拟盘。

旧 v1 在 2026-08-11 的实机演练结果：

- 报告 SHA256：`c06ea03647ced001d5e4ed6005336a9b3166553bb234e326d6a4a5e636dcb71c`，状态 `PASS`，耗时约 43 秒。
- C1 与 B0 各 50 个冻结目标逐项重放一致；目标 SHA256 分别为 `00dcb056a670434af218cfe9148d6d0ade98f6162f2f49ad9a4615bb381389dd` 和 `4e90bec2709d27271195c92af71baf86a34945156c8e8e1bab4024cbe2553504`。
- 临时 Provider 只追加 2026-08-12，覆盖 300 个成分、2,107 个新增核心特征值文件尾；谱系检查覆盖 6,650 个历史特征文件和 30,573,017 个非空历史值。
- C1 产生 50 个订单、46 个成交和 46 个持仓；B0 产生 50 个订单、48 个成交和 48 个持仓；两臂各生成 1 条日期对齐的 NAV 和新的 50 目标收盘信号。未成交来自冻结整手/资金约束，不是行情缺失。
- 临时审计链从 1 条推进到 3 条，临时证据检查点从 1 条推进到 2 条，最终临时链头为 `2d9db27d76bd514a8a9470a7f2edee95aeb672b4c4b584897b253138fb8d4a3b`。
- 正式环境前后完整签名均为 `f858739d130a95ebfa944d8d3093634cc8fc1ecf7fed90c9fe6b94a802272154`；正式账本逻辑 SHA256 仍为 `bd0eeae08cd9900f0e08e6fffe1cb6dda5331055be066cf56ccae591c1d7162f`，正式证据链头仍为 `9af90428f17c8b9677c97ff18003b015474da640fab06554177a5320ca70581c`。

这项结果证明当前工程链路能够处理唯一下一交易日，不证明 DeltaES1 能盈利。真实有效性仍从真实 Provider 的第 1 个观察日开始累计。

旧 v1 在 2026-08-12 执行合同固化后又完成一次隔离复演。报告 `/tmp/quantaalpha_research_shadow_execution_contract_rehearsal.json` 为 `PASS`，SHA256 `f1a13435e7544911ff4d6e127237a843f0beda8c6d237c595a8986bbf002d9bd`。该结果只保留为退役前的工程历史，不计入盈利证据。

当前 v2 的隔离演练报告为 `/tmp/quantaalpha_research_shadow_causal_v2_rehearsal.json`，状态 `PASS`，SHA256 `5650b0e915cb26cc3997fa0ab1b9c6a7c67996cd72bc0d239d68f7b77f49b8fd`，耗时约 42 秒。九项验收全部通过：冻结目标逐项一致、模型合同不变、Provider 只追加一个合成交易日、历史不变、配对撮合和下一信号完成、NAV 日期对齐、两条临时检查点有效、生产状态不变。C1/B0 在合成盘分别成交 45/47 只；差异来自 v2 的因果板块和申报数量规则。生产前后完整签名均为 `132691f76f8f9df5ca0ee27a1966fd8dcfaab787264756ec210d488b04f8ac62`。这些合成订单、收益和 NAV 仍强制标记为不计入盈利证据。

## 5. 非官方 Provider 的接入

若已有一个通过独立审计、只追加新交易日的 Provider，可先生成研究影子盘继承证明：

```bash
python scripts/verify_research_shadow_provider_lineage.py \
  --parent-provider /absolute/path/to/current_provider \
  --successor-provider /absolute/path/to/new_provider
```

证明通过后再使用 `switch_runtime_provider` 对应的安装流程切换。不要先修改 `.env`，否则活动数据可能处于“新 Provider 已启用但证明缺失”的失败关闭状态。

腾讯日线扩展工具 `scripts/a_stock_data_investment_extend.py` 只可作为收盘后的审计备选。2026-08-04、05、06 的前复权连续性授权只适用于那三天，不能自动推广到以后日期；每个新日期仍要保存不可变双快照并重新通过数据审计。

## 6. 证据门槛

| 阶段 | 最低要求 | 作用 |
| --- | --- | --- |
| 20 个交易日 | C1/B0 日期连续、账本和数据链完整 | 工程门，不判断盈利 |
| 60 个交易日 | C1 税费后超额大于 0、C1 相对 B0 大于 0、未触发 15% 回撤停止线 | 早期数值门 |
| 120 个交易日 | 保持 60 日条件并完成更长前向观察 | 较强证据，仍不自动晋级 |

历史 `REJECTED` 结论不会因影子盘短期盈利自动改变。达到阶段门后仍需检查交易成本、集中度、成交可实现性、收益来源和市场状态。

### 6.1 回撤风控

- 任一 C1/B0 账户最大回撤达到 `10%` 时，页面显示预警，但不会自动改变冻结策略。
- 任一账户最大回撤达到 `15%` 时，整个配对盘进入 `RISK_STOPPED`，后端同时拒绝待撮合信号和新收盘信号。
- 触发停止线的交易日、账户和最大回撤会写入 `DRAWDOWN_STOP_TRIGGERED` 审计事件。
- 风险停止不会自动恢复，也不能通过页面按钮绕过。正式 Provider 可以继续按追加式校验更新，但当前账本不再产生新交易证据；继续研究必须复核原因并建立新的预声明运行。

## 7. 故障检查

后端进程存活状态：

```bash
curl -sS http://127.0.0.1:8000/api/health
```

后台任务可工作状态（监督器实际使用；HTTP 503 表示需要恢复）：

```bash
curl -sS http://127.0.0.1:8000/api/health/ready | jq '{status,ready,pid,instanceId,workers}'
```

研究影子盘状态：

```bash
curl -sS http://127.0.0.1:8000/api/v1/research-shadow/state | jq '.data | {status,nextAction,marketData,executionContract,audit}'
```

自动守护与证据状态：

```bash
curl -sS http://127.0.0.1:8000/api/v1/research-shadow/automation | jq '.data | {status,lastCheckedAt,nextCheckAt,consecutiveErrors,lastError,evidenceCheckpoint}'
```

盘中行情状态：

```bash
curl -sS http://127.0.0.1:8000/api/v1/research-shadow/realtime | jq '.data.arms.C1 | {status,source,quoteAsOf,provisional,persisted,evidenceEligible}'
```

关键文件：

| 内容 | 路径 |
| --- | --- |
| 冻结声明 | `configs/research_shadow_20260812_deltaes1_causal_v2.declaration.json` |
| 冻结清单 | `data/trading/research_shadow/research_shadow_20260812_deltaes1_causal_v2/manifest.json` |
| 独立账本 | `data/trading/research_shadow/research_shadow_20260812_deltaes1_causal_v2/research_shadow.sqlite3` |
| Provider 继承证明 | `data/trading/research_shadow/research_shadow_20260812_deltaes1_causal_v2/provider_lineage/` |
| 自动守护状态 | `data/trading/research_shadow/research_shadow_20260812_deltaes1_causal_v2/automation_status.json` |
| 一致性证据链 | `data/trading/research_shadow/research_shadow_20260812_deltaes1_causal_v2/evidence_checkpoints/` |
| 旧 v1 退役终态 | `data/trading/research_shadow/research_shadow_20260811_deltaes1_v1/` |
| 当前正式 Provider | `data/market/a_share/qlib/investment_data_20260812_cn` |

不要手工编辑冻结声明、模型快照、继承证明或 SQLite 账本。任一冻结哈希不匹配时，系统会失败关闭。

新建的盘中 T+1 机制影子盘是独立运行和独立账本，详细协议、状态和运维方法见 [盘中 T+1 机制影子盘使用手册](intraday_shadow_manual.md)。它不会修改或替代本手册中的日频 DeltaES1/B0 运行。
