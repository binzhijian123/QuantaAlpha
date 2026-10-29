# Tushare 点时基本面接入手册

## 目标与边界

该接入不是新的日线行情源，也不会替换现有 `investment_data` Provider。它从 Tushare Pro 获取财务指标、实际披露日期和每日估值，把无未来数据的补充字段写入一个新的、不可覆盖的 Qlib Provider。

当前实现状态（2026-08-04）：

- 客户端、原始响应归档、重试/恢复、点时转换、审计、隔离 Provider、训练读取和模拟盘读取均已实现并通过本地固定数据测试；v2.2 另已实现严格季度同比 ROE 变化字段。
- 2026-08-04 实际执行预检仍返回 `BLOCKED`：本机未配置 `TUSHARE_TOKEN`。预检前后数据目录清单哈希一致，没有创建原始响应、manifest 或报告；真实账号权限、全量字段覆盖率、接口限流和修订版本合同仍未验证。
- 任何新因子在正式历史门和走步门通过前仍为 `RESEARCH_ONLY`，不能生成模型模拟盘信号。

官方字段依据：

- [`fina_indicator`](https://tushare.pro/document/2?doc_id=79)：财务指标及 `ann_date`。普通逐股票接口至少需要 2000 积分；全市场季度 VIP 接口需要 5000 积分。
- [`disclosure_date`](https://tushare.pro/document/2?doc_id=162)：`actual_date`、`ann_date`、预计披露和修改记录，至少需要 2000 积分。
- [`daily_basic`](https://tushare.pro/document/2?doc_id=32)：PB、PE TTM、市值等每日指标，至少需要 2000 积分；官方说明 5000 积分无总量限制。

## 安全配置

不要在聊天、日志或配置 JSON 中发送 Token。只在项目本机 `.env` 增加：

```dotenv
TUSHARE_TOKEN=<your-local-token>
```

`.env` 已被 `.gitignore` 排除。同步 manifest 只记录 `tokenPersisted=false`，不会保存 Token 或 Token 哈希。

## 点时规则

财务数据必须同时满足以下规则：

1. 可用日期取 `max(fina_indicator.ann_date, disclosure_date.actual_date)`。
2. 因公告时刻不确定，财务值从该日期之后的第一个 Provider 交易日开始生效。
3. 同一报告期发生重述时，只有重述生效后才替换该报告期旧版本。
4. 较老报告期的晚到重述不能覆盖已经可用的较新季度。
5. `daily_basic` 只在其 `trade_date` 收盘后用于下一交易日执行。
6. 任意公告提前使用、报告期晚于特征日期、股票整段缺失都会令审计失败，并禁止生成扩展 Provider。

默认输出字段：

```text
$pit_q_dt_roe
$pit_q_ocf_to_sales
$pit_q_netprofit_yoy
$pit_debt_to_assets
$pit_pb
$pit_pe_ttm
$pit_total_mv
$pit_circ_mv
$pit_book_to_price
$pit_log_total_mv
$pit_report_age_days
$pit_disclosure_lag_days
```

`$pit_book_to_price` 只对正 PB 计算 `1 / PB`；非正 PB 保持缺失。

### v2.2 严格季度 dROE1 字段

v2.2 只请求 `fina_indicator.q_roe` 与 `disclosure_date`，不调用 `daily_basic`，并派生：

```text
$pit_droe_1y = q_roe(最新可用季度) - q_roe(同一季度一年前)
```

必须是精确的 `t-4` 季度。当前季度或同比季度的后续修订只从修订公告后的下一 Provider 交易日起生效，并重新计算该值；最新报告期结束六个月后字段变为缺失，不做临近季度替代、插值或永久前向填充。

该字段是论文 dRoe1 的数据源代理，不是会计行项目级精确复现：论文按扣除非经常项目前利润与滞后账面权益构造季度 ROE，本实现直接采用 Tushare 定义的单季度 `q_roe`。如果真实接口只返回今天看到的最终修订快照，无法还原历史公告版本，则该候选最多为 `RESEARCH_ONLY`。

## 操作顺序

先进入项目环境：

```bash
cd /absolute/path/to/user/Documents/QuantaAlpha-main
source scripts/qa_env.sh
```

### 1. 权限预检

```bash
python scripts/tushare_fundamental_sync.py --preflight
```

预期为 `status=PASS`。当前无 Token 时会以退出码 `2` 返回 `BLOCKED`，且不创建数据目录。

### 2. 两只股票小样本

```bash
python scripts/tushare_fundamental_sync.py \
  --codes 000001.SZ,600000.SH \
  --start-date 2024-01-01 \
  --batch-id tushare_fundamental_probe_v1
```

检查：

```text
data/market/a_share/manifests/tushare_fundamental_probe_v1.json
data/market/a_share/reports/tushare_fundamental_probe_v1.json
data/market/a_share/staging/tushare_fundamental_probe_v1/as_of_audit.json
```

小样本只验证权限、字段和时点转换，不得用于收益结论，也不能 `--promote`。

v2.2 dROE1 权限与字段探针使用冻结命令：

```bash
python scripts/tushare_fundamental_sync.py \
  --base-provider data/market/a_share/qlib/investment_data_20260725_baostock_v2_1_investment_to_assets_cn \
  --codes 000001.SZ,600000.SH \
  --start-date 2014-01-01 \
  --financial-metrics q_roe \
  --daily-basic-metrics "" \
  --batch-id tushare_droe_probe_v2_2
```

### 3. 全市场构建隔离 Provider

当前 2014-01-01 至 2026-07-24 的点时 CSI300 名单包含 700 只曾任成分股，默认逐股票调用三个接口，预计 2100 次请求。使用新批次 ID 和不存在的输出目录：

```bash
python scripts/tushare_fundamental_sync.py \
  --start-date 2014-01-01 \
  --batch-id tushare_fundamental_full_v1 \
  --promote \
  --output-provider data/market/a_share/qlib/investment_data_20260725_fundamental_v1_cn
```

基础 Provider 保持只读。输出 Provider 通过硬链接复用基础字段，新增独立基本面二进制和 `quantaalpha_extension.manifest.json`；扩展 manifest 会进入 Provider 指纹，旧行情缓存不能冒充基本面缓存。

v2.2 探针通过并确认修订版本合同后，才允许执行以下全量构建：

```bash
python scripts/tushare_fundamental_sync.py \
  --base-provider data/market/a_share/qlib/investment_data_20260725_baostock_v2_1_investment_to_assets_cn \
  --start-date 2014-01-01 \
  --financial-metrics q_roe \
  --daily-basic-metrics "" \
  --batch-id tushare_droe_full_v2_2 \
  --promote \
  --output-provider data/market/a_share/qlib/investment_data_20260725_tushare_v2_2_droe1_cn
```

在当前 `BLOCKED_DATA_ACCESS` 状态不得执行收益准入或正式回测。冻结候选、方向、21 日持有期和 27 次同口径试验登记在 `configs/profit_candidates_v2_2_tushare_droe1.json`；源候选库为 `data/factorlib/profit_trials_v2_2_tushare_droe1_source.json`。

### 4. 失败批次恢复

若全量请求部分失败，保留原批次并启动新批次复用已成功原始响应：

```bash
python scripts/tushare_fundamental_sync.py \
  --start-date 2014-01-01 \
  --batch-id tushare_fundamental_full_v2 \
  --reuse-raw-batch tushare_fundamental_full_v1 \
  --promote \
  --output-provider data/market/a_share/qlib/investment_data_20260725_fundamental_v2_cn
```

旧批次不会被覆盖；新 manifest 会记录复用响应数量和来源目录。

## 首批冻结候选

候选已在下载真实数据前固定于 `configs/profit_candidates_v1_6_fundamentals.json`：

| 候选 | 方向 | 经济机制 |
|---|---:|---|
| 扣非单季度 ROE | 高 | 盈利质量 |
| 单季度经营现金流/销售额 | 高 | 现金转化质量 |
| 单季度归母净利润同比 | 高 | 盈利增长 |
| 负资产负债率 | 高 | 低杠杆韧性 |
| 正 PB 的账面市值比 | 高 | 价值 |

真实数据接入后，先审计字段覆盖、极值、行业/规模暴露和 2021 选择期 RankIC；任何失败候选都必须保留。只有新候选通过固定历史门、成本压力和季度走步后，才能创建新的 `PAPER_CANDIDATE`，再开始 20/60/120 个交易日的冻结模拟盘。
