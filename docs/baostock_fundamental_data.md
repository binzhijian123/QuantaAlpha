# BaoStock 点时基本面接入手册

## 1. 用途与边界

该接入为现有 `investment_data_20260725_cn` 增加年度基本面与每日 PB，不替换日线行情，也不修改原 Provider、活动策略快照或模拟账户。

BaoStock v1.7 与 Tushare v1.6 是两套独立协议，不能混用名称或结果：

- v1.7 只使用年度第四季度 `roeAvg`、年度第四季度 `YOYNI` 和每日 `pbMRQ`。
- v1.7 不把 `roeAvg` 描述成扣非单季度 ROE，也不把 `YOYNI` 描述成单季度归母净利润增长。
- 财务组合在 `max(profit.pubDate, growth.pubDate)` 后的第一个 Provider 交易日生效。
- 每日 PB 在当日收盘后可见，只能用于下一交易日执行。
- BaoStock 不提供历史修订版本。该流程能阻止普通公告时点泄漏，但不能重建后来被修订掉的旧值，因此仍需在验证报告中保留修订风险标记。

候选在全历史同步和任何历史收益结果之前冻结于：

```text
configs/profit_candidates_v1_7_baostock.json
```

## 2. 已验证状态

本机 BaoStock `0.9.2` 匿名预检已通过，不需要账号、Token 或密码。

固定两股探针 `baostock_fundamental_probe_v1_7_windowed` 已完成：

- 股票：`600000.SH`、`600519.SH`
- 日期：2016-01-01 至 2026-07-24
- 点时面板：5,128 行
- 年度 ROE 与年度净利润同比覆盖率：`97.72%`
- 每日 PB 与账面市值比覆盖率：`100%`
- 公告时点泄漏：0 行
- 报告期泄漏：0 行
- 缺失股票：0

全量批次 `baostock_fundamental_full_v1_7_windowed` 已完成并通过审计：

- 637/637 只历史 CSI300 成分股成功，0 只失败。
- 按各股票实际成分区间裁剪后共发起 9,705 次源请求，比固定全区间的 15,925 次减少 `39.06%`。
- 点时面板共 830,887 行；年度 ROE 覆盖 `97.56%`，年度净利润同比覆盖 `97.64%`，PB 覆盖 `100%`，账面市值比覆盖 `99.93%`。
- 4,196 份年度报告的利润/成长公告日期无冲突；公告时点泄漏和报告期泄漏均为 0 行。
- 全量同步耗时 4,692.76 秒，所有原始响应、manifest、审计报告和失败恢复信息均已保留。

复用上述原始响应的推广批次 `baostock_fundamental_promote_v1_7_windowed` 已创建隔离 Provider：

```text
data/market/a_share/qlib/investment_data_20260725_baostock_v1_7_cn
```

新 Provider 指纹为 `478a2a2f82e4d43edbad1f09161f9b88b1b7b50d03bf8ecc36e1e0684d13cd27`；基础 Provider 指纹仍为 `7813a5dc8e2d081f80d999599a8011013d202c2e7b12e1d3241553c81d3c5d35`，原数据没有被覆盖。

收益验证也已完成：

- 2021 准入运行 `profit_20260804_005110` 中，年度 ROE RankIC 为 `-0.01309`，年度净利润同比为 `-0.00555`，均被淘汰；账面市值比 RankIC 为 `0.01968`，通过准入并冻结为唯一目标。
- 正式运行 `profit_20260804_005256` 的 22 条同质候选收益全部完成，但账面市值比组合最终为 **`REJECTED`**，`paperEligible=false`。
- 该组合年化税费后超额为 `4.00%`、IR 为 `0.437`、最大回撤为 `-21.00%`，2 倍成本下年化超额为 `-1.06%`；90% 增量 bootstrap 区间为 `[-0.73%, 6.39%]`，DSR 为 `0.500`，PBO 为 `0.716`。
- 因此未运行 v1.7 季度走步，未创建策略快照、信号或模拟盘交易。

## 3. 产物规则

每个批次均不可覆盖：

```text
data/market/a_share/raw/baostock_fundamentals/<batch_id>/
data/market/a_share/staging/<batch_id>/
data/market/a_share/manifests/<batch_id>.json
data/market/a_share/reports/<batch_id>.json
```

原始目录按股票保存：

```text
profit_annual.json
growth_annual.json
daily_valuation.json
```

失败重跑必须使用新 `batch_id`，并通过 `--reuse-raw-batch` 复用旧批次中已成功的文件。不得删除失败批次或覆盖原始响应。

## 4. 操作命令

预检，不创建数据产物：

```bash
source scripts/qa_env.sh
python scripts/baostock_fundamental_sync.py --preflight
```

估算全量请求，不访问 BaoStock：

```bash
python scripts/baostock_fundamental_sync.py --estimate
```

新的两股探针示例。已存在的批次名不能复用：

```bash
python scripts/baostock_fundamental_sync.py \
  --codes 600000.SH,600519.SH \
  --start-date 2016-01-01 \
  --batch-id baostock_fundamental_probe_v1_7_r2
```

下一次重新采集时必须使用新的批次名。以下示例只做全量原始同步和审计，不立即创建 Provider：

```bash
python scripts/baostock_fundamental_sync.py \
  --start-date 2016-01-01 \
  --batch-id baostock_fundamental_full_v1_7_r2
```

只有新全量报告为 `PASS` 后，才用另一新批次复用原始响应并创建新的隔离 Provider 路径：

```bash
python scripts/baostock_fundamental_sync.py \
  --start-date 2016-01-01 \
  --batch-id baostock_fundamental_promote_v1_7_r2 \
  --reuse-raw-batch baostock_fundamental_full_v1_7_r2 \
  --promote \
  --output-provider data/market/a_share/qlib/investment_data_20260725_baostock_v1_7_r2_cn
```

## 5. 后续收益门

Provider 创建成功不代表策略有效。v1.7 已按以下固定顺序执行并在第 4 步停止：

1. 审计 637 只股票的年度字段/PB 覆盖、异常值、行业和规模暴露。
2. 在冻结的 2021 选择期做 RankIC 和 `0.70` 相关性去重。
3. 前置门失败即把候选记为 `REJECTED`，不得翻转方向或修改字段后沿用 v1.7。
4. 对通过准入的账面市值比运行固定 `B0/F1/C1`、成本压力和 DSR/PBO；正式门失败后停止，不运行季度走步。
5. 不得根据已查看的 v1.7 结果翻转方向、降低门槛或改选年度净利润同比。新的机制、字段或组合必须先注册为新协议版本，再接触验证期收益。
6. 只有新协议完整通过历史门和季度走步，才能创建 `PAPER_CANDIDATE`；之后还需原样运行 20/60/120 个交易日模拟盘。

该链路不会承诺盈利，也不会授权实盘交易。

## 6. v1.8 E/P 代理扩展

v1.8 只增加一条预注册候选：

```text
configs/profit_candidates_v1_8_baostock_ep.json
```

候选字段 `pit_earnings_to_price` 来自 BaoStock 日频动态市盈率：

```text
1 / peTTM，peTTM 为有限非零值时有效
```

负 `peTTM` 的倒数保留为负值，零值和空值保持缺失。这样亏损公司位于价值排序的低端，和 Liu、Stambaugh、Yuan（2019）构造中国 E/P 价值组合时保留负 E/P 股票作为成长股票的处理一致。该实现仍只是代理：论文使用最近已披露的扣非净利润除以上月末总市值，并按月形成组合；BaoStock `peTTM` 是每日动态市盈率，不能证明其净利润口径完全等同于论文。

`--include-earnings-to-price` 会请求 `peTTM` 并写入新字段。复用不含该字段的 v1.7 原始批次时，年度利润和成长响应继续复用；只有 `daily_valuation.json` 因字段不兼容而重新获取。字段缺失会在 manifest 的 `reuseMisses` 中记录，损坏数据或时间窗口不匹配仍然失败关闭。

```bash
python scripts/baostock_fundamental_sync.py \
  --start-date 2016-01-01 \
  --end-date 2026-07-24 \
  --include-earnings-to-price \
  --reuse-raw-batch baostock_fundamental_full_v1_7_windowed \
  --batch-id <new-v1.8-batch-id> \
  --promote \
  --output-provider <new-isolated-provider>
```

v1.8 保持 v1.7 的模型、两日标签、交易规则和费用不变，目的是验证 E/P 对当前软件策略的增量价值，不声称复现论文组合。E/P 是第 23 条同口径历史候选；项目全部已查看策略的保守次数下限为 27。2022-2026 已被此前实验揭盲，因此本轮任何历史通过结果仍只能作为 `RESEARCH_ONLY`，独立证据必须从候选冻结后的前向日期开始累计。

实际批次 `baostock_fundamental_promote_v1_8_ep_windowed` 已完成并通过数据审计：

- 637/637 只股票成功，0 只失败；复用 1,274 份年度响应，只重新查询 637 份日估值。
- 点时面板仍为 830,887 行，E/P 覆盖率为 `100%`，其中 46,511 行为负值；所有值有限，公告时点泄漏和报告期泄漏均为 0。
- 隔离 Provider 为 `data/market/a_share/qlib/investment_data_20260725_baostock_v1_8_ep_cn`，指纹为 `80bcbde5f2cf4a2e878e12511ea729c3ca7ee91a95d70f98268905dfb6e95a20`。
- 基础 Provider 指纹仍为 `7813a5dc8e2d081f80d999599a8011013d202c2e7b12e1d3241553c81d3c5d35`，v1.7 Provider 仍为 `478a2a2f82e4d43edbad1f09161f9b88b1b7b50d03bf8ecc36e1e0684d13cd27`。
- `data/factorlib/profit_trials_v1_8_baostock_ep_provider.json` 的 23/23 个缓存均绑定新 Provider。

固定准入运行 `profit_20260804_021654` 的数据审计通过，但因子审计为 **`REJECTED`**：E/P 覆盖率 `100%`，2021 平均 RankIC 为 `0.01282`，但与 v1.7 账面市值比的截面排序相关为 `0.72140`，超过预注册的 `0.70` 去冗余门。因此没有运行 2022-2026 正式收益矩阵、季度走步或模拟盘，也不得通过放宽相关性阈值继续 v1.8。

论文依据：

- Liu, Stambaugh, Yuan, *Size and Value in China*, Journal of Financial Economics 134 (2019), DOI `10.1016/j.jfineco.2019.03.008`。
- 论文公开全文：`https://hub.hku.hk/bitstream/10722/273695/1/content.pdf`。

## 7. v1.9 季节性盈利意外代理

v1.9 在完整历史同步和任何 SUE 收益结果出现前，只注册一条候选：

```text
configs/profit_candidates_v1_9_baostock_seasonal_sue.json
```

BaoStock 的 `netProfit` 是年内累计净利润，因此先按连续季度转换为单季值；缺少上一累计季度时不跨缺口相减。随后计算：

```text
UE_q = standalone_net_profit_q - standalone_net_profit_(q-4)
SUE_q = UE_q / sample_std(UE_(q-8) ... UE_(q-1))
```

标准差窗口只使用当期之前的 8 个季度，至少需要 6 个有效观测；零值、非有限分母及无效日期均失败关闭。信号在 BaoStock `pubDate` 之后的第一个 Provider 交易日才可用，并在 `statDate` 六个月后清空。原始响应按股票保存为不可覆盖的 `profit_quarterly.json`，manifest 绑定每个响应的 SHA256、查询年度、基础 Provider 指纹和点时审计。

这不是论文 SUE 的精确复现。论文使用拆股调整后的单季 EPS；当前实现使用公司汇总净利润，因为 BaoStock 没有可靠的历史单季拆股调整 EPS，也不提供被后续更正覆盖的历史版本。因此即使历史门全部通过，v1.9 仍只能是 `RESEARCH_ONLY`，不能直接授权模拟盘或实盘。

在不读取价格、标签或收益的前提下，固定探针 `baostock_sue_probe_v1_9_hash20` 已通过：20/20 只股票、392 个源季度、0 个连续季度缺口、20 只均产生 SUE，共 192 个 SUE 事件；点时面板 4,723 行、覆盖率 `97.08%`、公告时点泄漏和报告期泄漏均为 0。复用探针的原始文件哈希和解压面板逐字节一致。

全市场同步命令支持确定性分片；分片只采集不可变原始响应，不能单独晋升 Provider：

```bash
python scripts/baostock_sue_sync.py \
  --start-date 2016-01-01 \
  --end-date 2026-07-22 \
  --batch-id <batch-id> \
  --shard-count 4 \
  --shard-index <0-3>
```

分片全部成功后，必须合并完整的 637 股原始响应，再用单一批次复用并生成新的隔离 Provider；不得覆盖 v1.8 Provider。固定准入使用 2021 选择期和 21 个交易日标签 `Ref($close,-22) / Ref($close,-1) - 1`，要求正 RankIC、覆盖率不低于 `0.50`，且与 Alpha158(20) 和 v1.7/v1.8 候选的绝对截面排序相关均低于 `0.70`。任一门失败即停止，不读取正式 2022+ 策略收益。

全市场数据批次已经完成：

- 四个最终分片均为 `PASS`，合计 637/637 只股票、0 只失败。连接中断由有限截止时间和重连恢复，已有原始响应通过新批次逐股票复用，旧批次没有覆盖或删除。
- 晋升批次 `baostock_sue_promote_v1_9_windowed` 复用 637/637 份原始响应，网络查询为 0；这些响应代表 25,780 个季度接口请求。
- 完整点时面板为 830,887 行，日信号覆盖率 `92.16%`；637 只中 625 只在源事件中产生 SUE，624 只在实际历史成分窗口内出现有效日信号。
- 23,237 个源季度只有 10 个连续季度缺口；缺口保持缺失。公告时点泄漏和报告期泄漏均为 0，27,770 个过期持有值按六个月规则清空。
- 765,712 个非空日信号全部有限，范围为 `[-47.40, 44.21]`，没有绝对值超过 100。
- 隔离 Provider 为 `data/market/a_share/qlib/investment_data_20260725_baostock_v1_9_sue_cn`，指纹为 `a4ca37aa0bd52ae3e7c66a1f97283655a6a84983bf91db9bfac4324d497503fa`。v1.8 指纹仍为 `80bcbde5f2cf4a2e878e12511ea729c3ca7ee91a95d70f98268905dfb6e95a20`。

24/24 条因子缓存已在新 Provider 上准备完成。早期准入尝试 `profit_20260804_035434` 和 `profit_20260804_035702` 因缓存终点及非交易日起点合同停止，均作为技术阻塞证据保留。验证器随后改为按 Provider 实际交易日历核对缓存边界，并增加了定向回归测试。

有效准入运行 `profit_20260804_040849` 的数据审计为 `PASS`，因子审计和最终决策为 **`REJECTED`**：SUE 在 2021 选择期的覆盖率为 `90.97%`，平均日 RankIC 为 `-0.00909`，未通过预注册的正方向门；未触发功能冗余门。系统按停止条件没有打开 2022+ 正式收益、季度走步或模拟盘。该结果否定的是当前 BaoStock 净利润 SUE 代理及其冻结方向，不是技术阻塞，也不允许事后翻转符号或修改窗口。

方法依据：

- Li, Liu, Liu, Wei, *Replicating and Digesting Anomalies in the Chinese A-share Market*, Management Science，DOI `10.1287/mnsc.2023.4904`。
- Global-q 因子构造说明：`https://global-q.org/uploads/1/2/2/6/122679606/portfoliostd_2024april.pdf`。

## 8. v2.0 盈利公告异常收益 ABR6 代理

中国 A 股异常研究在 2007 年后发现盈利动量家族中的 SUE 与 6/9 个月公告异常收益仍有显著性。v2.0 因此在计算任何 ABR 或未来收益前，只冻结一条 6 个月 ABR 候选：

```text
configs/profit_candidates_v2_0_baostock_abr6.json
```

对每个 BaoStock 公告日期，`d0` 是公告日当天或之后的第一个 Provider 交易日。事件异常收益为 `d=-2..+1` 四个交易日内股票复权收盘收益减去沪深 300 指数收益之和；信号在反应窗结束后的下一交易日，即 `d+2` 才生效，并在报告期六个月后失效。反应窗任一价格、收益或成交量无效时整条事件失败关闭。标签固定为 126 个持有交易日：`Ref($close,-127) / Ref($close,-1) - 1`。

该实现仍是研究代理：论文使用宽基准价值加权市场收益，当前使用沪深 300 指数；BaoStock 也没有被后续修订覆盖的历史公告版本和盘中公告时间。因此历史结果最多是 `RESEARCH_ONLY`，不能直接授权模拟盘。

数据验证结果：

- 固定哈希 20 股探针两次均为 `PASS`：20/20 股票、100/100 有效事件、4,248 个面板行、覆盖率 `97.79%`，两类泄漏均为 0；事件、面板和审计文件逐字节一致。
- 全量批次 `baostock_abr_promote_v2_0_windowed` 为 `PASS`：637/637 股票、831,171 行、18,093 条公告事件，其中 17,402 条 ABR 有效；637 只股票均产生 ABR，691 条无效价格窗保持缺失。
- 日信号覆盖率为 `94.23%`，公告时点与报告期泄漏均为 0，31,629 个过期值被清空。
- 隔离 Provider 为 `data/market/a_share/qlib/investment_data_20260725_baostock_v2_0_abr_cn`，指纹为 `9c30fecf1a745ffc68974da906248e90c98c0b9873feb5cbf87b1874326bb387`；v1.9 Provider 指纹未改变。

有效准入运行 `profit_20260804_043341` 为 `PASS`：选择期覆盖率 `93.64%`、平均日 RankIC `0.12137`，与 Alpha158(20) 及 v1.7-v1.9 候选的最大绝对排序相关低于 `0.70`。

第一次正式运行 `profit_20260804_043420` 后发现 runner 只向 Qlib 传递了 `topk/n_drop`，遗漏冻结的 `hold_thresh=126`，实际使用了默认值 `1`。修复参数传递后的 `profit_20260804_052213` 又暴露第二个执行问题：相同配置和完全相同的 291,809 条预测会因进程哈希顺序产生不同持仓路径。两次运行均保留为实现偏差证据，不再作为有效收益结论。

最终权威运行 `profit_20260804_055814` 强制 `PYTHONHASHSEED=0`，按日以“预测分数降序、股票代码升序”确定并列顺序，并为 27 次实际回测保存有效策略参数和逐日持仓数量。它完成 25/25 条试验和 1,101 个同步测试交易日，目标重放的收益、持仓和指标与正式 `T25` 完全一致，但结论仍为 **`REJECTED`**：

- `C1` 年化税费后超额 `8.48%`、IR `0.977`、最大回撤 `-14.26%`、最近 252 日 `2.27%`、2025 年 `-4.83%`、2 倍成本年化超额 `8.31%`。
- 相对 `B0` 的年化增量为 `2.11` 个百分点，但 90% 分块 bootstrap 区间为 `[-4.78%, 8.63%]`，不能排除增量小于等于零。
- DSR 概率 `0.1034`，低于 `0.95`；PBO `0.3587`，高于 `0.05`。因此不运行走步、不创建快照或模拟盘信号。
- `F1` 独立 ABR 模型在确定性合同下只有年化税费后超额 `1.84%`、IR `0.275`、最大回撤 `-12.27%`、最近 252 日 `-3.84%`。此前看似较强的 F1 是未定义执行路径的产物，不应冻结为前向盈利假设。

论文依据：

- Li, Liu, Liu, Wei, *Replicating and Digesting Anomalies in the Chinese A-share Market*, Management Science，DOI `10.1287/mnsc.2023.4904`。
- 作者接受稿：`https://ira.lib.polyu.edu.hk/bitstream/10397/115240/1/Li_Replicating_Digesting_Anomalies.pdf`。
- Global-q ABR 构造说明：`https://global-q.org/uploads/1/2/2/6/122679606/portfoliostd_2024april.pdf`。

## 9. v2.1 年度投资/资产增长因子

v2.1 在读取候选因子值、RankIC 或未来收益前，预注册唯一候选 `aacce6286aeb85e5`：

```text
-$pit_fy_asset_growth
```

BaoStock `query_growth_data.YOYAsset` 表示年度总资产同比增长；因子取负值，因此资产扩张越保守，分数越高。只接受年末 Q4 报告，且报告必须在次年 6 月 30 日前披露；信号从 6 月 30 日之后首个 Provider 交易日生效，并在报告期末 18 个月后清空。标签和持有期固定为 252 个交易日：`Ref($close,-253) / Ref($close,-1) - 1`。这与论文的年度 I/A 方向一致，但本项目使用 CSI300、截面归一化、LightGBM 和 TopK 执行，不是论文五分位组合的精确复现。

数据与准入结果：

- 原始不可变响应共 637 只股票、4,196 行，`YOYAsset` 有效 4,194 行；日期、年度口径和冲突重复检查均通过。
- 全量批次 `baostock_investment_promote_v2_1_windowed` 复用 637 份已有响应，网络请求为 0；生成 830,887 个面板行，因子覆盖率 `95.37%`，公告时点泄漏和报告期泄漏均为 0，241 个过期值被清空。
- 隔离 Provider 为 `data/market/a_share/qlib/investment_data_20260725_baostock_v2_1_investment_to_assets_cn`，指纹为 `b7af0ceb3aa021736ec0ca7307a3d695801c02d8d7c46e77e067cee6f7ebda76`；v2.0 Provider 未被覆盖。
- 首次审计 `profit_20260804_065735` 在计算候选 RankIC 前发现分段隔离不足及旧缓存绑定，作为技术阻塞证据保留。修正实际交易日边界并在新 Provider 上重建缓存后，准入运行 `profit_20260804_070010` 通过：选择期覆盖率 `99.81%`、平均日 RankIC `0.003206`。方向虽为正，但强度很弱，只允许按预注册协议打开正式矩阵，不构成盈利证据。

最终正式运行 `profit_20260804_070058` 完成 26/26 条收益试验、B0、F1 和 C1，共 28 次实际回测，0 失败，1,101 个同步测试交易日：

| 候选 | 年化税费后超额 | IR | 最大回撤 | 2025 年化超额 | 最近 252 日 | 2x 成本 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `B0` Alpha158(20) | `7.52%` | `0.658` | `-22.41%` | `-11.01%` | `-17.37%` | `7.44%` |
| `F1` 低资产增长独立模型 | `0.72%` | `0.112` | `-10.06%` | `1.17%` | `-1.90%` | `0.65%` |
| `C1` B0 + 低资产增长 | `7.45%` | `0.848` | `-15.0124%` | `1.01%` | `-5.29%` | `7.36%` |

最终结论为 **`REJECTED`**，`paperEligible=false`：

- `C1` 相对 `B0` 的年化超额变化为 `-0.066` 个百分点，IR 增量为 `0.189`，未达到预注册的 `2` 个百分点或 `0.2` IR 实质改善门。
- 90% 的 252 日分块 bootstrap 增量区间为 `[-6.11%, 8.16%]`；DSR 概率为 `0.1217`，PBO 为 `0.5742`，均未过门。
- 最近 252 日为负，2025 年未达到 `2%`，最大回撤按未四舍五入值略超 `15%` 上限。系统未运行走步、未创建快照或模拟盘信号。
- 独立目标重放与正式 `T26` 的逐日收益 SHA256 均为 `adeb74b5f32fb3e00faf5bb8115988187ea953e92d8ea82dd82cca8a605c9a5f`，持仓数量 SHA256 均为 `21e763535de374c5c03b1e75b88744e7039710e479886147bace80e93bf0e83d`；指标和有效配置完全一致。

论文依据：

- Li, Liu, Liu, Wei, *Replicating and Digesting Anomalies in the Chinese A-share Market*, DOI `10.1287/mnsc.2023.4904`。
- 作者接受稿：`https://ira.lib.polyu.edu.hk/bitstream/10397/115240/1/Li_Replicating_Digesting_Anomalies.pdf`。
- Global-q 年度 I/A 构造说明：`https://global-q.org/uploads/1/2/2/6/122679606/portfoliostd_2020ajune.pdf`。
- BaoStock 字段说明：`https://baostock.com/mainContent?file=pythonAPI.md`。
