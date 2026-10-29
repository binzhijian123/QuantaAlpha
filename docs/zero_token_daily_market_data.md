# 零 Token 日线扩展使用说明

## 用途

该流程使用腾讯公开日线接口，为现有 `investment_data` Qlib Provider 构建一个新的、不可覆盖的 CSI300 前向数据版本。

- 不需要同花顺账号或 QuantAPI Token。
- 不修改基础 Provider。
- 不自动切换 `.env`。
- 不把扩展结果声明为完整全 A 数据。
- 任一接口、复权、单位、快照或完整性检查失败时，不生成最终 Provider。

## 当前状态

腾讯扩展的基础 Provider 保持不变：

```text
data/market/a_share/qlib/investment_data_20260804_cn
```

基础指纹：

```text
e24ad8753bdd27610dc00061760cec593a560a3fb9b4986ba748c9250cfbdb10
```

用户已明确批准把 8 月 4 日作为基准锚点，并把 8 月 5、6 日稳定的腾讯前复权数据计入复权区间证据。系统仍要求至少 2 个基础 Provider 直接样本，并验证完整的 `8 月 4 日锚点 + 8 月 5、6 日前向样本` 的 `qfq/raw` 比率连续性；该授权不会增加共同历史样本数，也不会替代或放宽原有价格、复权因子、成交量和成交额误差阈值。

首次在 8 月 7 日实时重取腾讯数据时，`600886.SH` 的历史前复权价格因后续公司行为发生回写，审计按预期拒绝。最终发布使用 8 月 6 日当时保存的两份不可变快照重放，避免引入事后信息。审计和发布均已通过，新 Provider 为：

```text
data/market/a_share/qlib/investment_data_20260806_a_stock_data_csi300_cn
```

新 Provider 指纹：

```text
8dea8572b80e87f616106ae06d6bebdfbd2b9fc1b4d0f59aef17749e40e0c1ec
```

该腾讯 Provider 曾在 2026-08-07 通过审计后切换为运行源。它现在作为不可变审计证据和回退源保留；后续官方完整发布不会修改或删除它。

截至 2026-08-11，当前运行源已升级为上游恢复发布的官方 2026-08-10 完整快照：

```text
data/market/a_share/qlib/investment_data_20260810_cn
```

- 发布归档：`560,601,055` 字节。
- 归档 SHA256：`b502e23852ea5893aec5f434e7467aac87d001e907aa80640988a824d729988d`。
- 归档与安装目录逐文件一致：61,318 个文件、719,414,214 字节，安装树 SHA256 为 `e6d7646d64376a89a9b226e28bd1caf8ee882e5472877eff78d555a63ab70a41`。
- Provider 指纹：`b78e2e624105cdafa0dfc5901adcb0dc77808964197261c18f46ca54ae53b08d`。
- 日历截至 `2026-08-10`，该日 CSI300 点时成分为 300 只；完整市场有 5,543 个活动证券。

因此，8 月 4、5、6 日腾讯数据的作用是证明当时扩展的复权和单位合同，而不是替代官方 8 月 10 日完整市场数据。模拟盘现金、持仓和待执行信号均未改变；旧策略仍为 `RESEARCH_ONLY`，不会因为数据源切换而自动生成信号或交易。

## 字段合同

```text
OHLC    = 腾讯前复权 OHLC * 基础 close 锚点 / 腾讯前复权 close 锚点
factor  = 标准化 close / 腾讯 raw close
volume  = 腾讯 raw 股数 / (100 * factor)
amount  = 腾讯 raw 成交额元 / 1000
vwap    = amount * 10 / volume
change  = 腾讯 raw close / 上一个有效 raw close - 1
adjclose = 标准化 close * 基础 adjclose/close 稳定比例
```

普通股票和科创板的腾讯成交量单位不同，适配器会先统一为“股”；`change` 必须使用 raw close，不能使用复权 close。

## 审计命令

默认只审计、不生成 Provider。对历史日期必须重放当时保存的不可变双快照，不能重新请求今天已经回写的前复权历史：

```bash
/absolute/path/to/user/miniforge3/envs/quantaalpha/bin/python \
  scripts/a_stock_data_investment_extend.py \
  --end-date 2026-08-06 \
  --source-snapshot-dir data/market/a_share/raw/a_stock_data_investment_daily/a_stock_data_investment_20260806_csi300_v2 \
  --forward-adjustment-evidence-dates 2026-08-05 2026-08-06 \
  --batch-id <新的唯一批次号>
```

只有准备生成新 Provider 时才增加：

```bash
--promote \
--output-provider data/market/a_share/qlib/<新的不存在目录>
```

输出目录或批次号已经存在时，命令会拒绝覆盖。

本次通过的发布报告：

```text
data/market/a_share/reports/a_stock_data_investment_20260806_csi300_v6_forward_evidence_replay_publish.json
```

## 发布后检查

只有报告为 `PASS` 才继续：

1. 基础 Provider 指纹必须保持不变。
2. 新 Provider 日历应只追加目标交易日。
3. 301 个证券、10 个字段的历史二进制前缀必须逐字节不变。
4. `csi300` 在每个目标日必须仍有 300 个证券。
5. `SH000300` 必须有完整目标日行情。
6. `market=all` 跨过基础截止日必须被范围保护拒绝。
7. Qlib、后端健康检查和完整测试通过后，才可以考虑切换数据源。
8. 旧模型快照不能直接用于新 Provider；训练与信号 Provider 必须一致，并重新通过收益和模拟盘资格检查。

## 官方发布安装

优先使用官方 `investment_data` 完整发布。仓库安装器会先校验发布清单、归档大小和 SHA256，再安全解压到一个新目录；现有 Provider 不会被覆盖。只有显式传入 `--switch` 才更新 `.env` 和 Qlib 默认链接：

```bash
cd /absolute/path/to/user/Documents/QuantaAlpha-main
source scripts/qa_env.sh
python scripts/install_investment_data_release.py --switch
```

指定历史发布时增加 `--release-tag YYYY-MM-DD`。已有离线归档可使用 `--source-dir <发布目录>`。切换后需要重启前后端，使运行进程重新读取配置。

只有在官方发布暂时不可用、且确实需要新交易日时，才使用本页腾讯扩展流程。每组未来日期都必须重新保存同日双快照并单独审计，不能把本次授权自动延伸到后续日期。

## 证据边界

- 本次授权只适用于 2026-08-05 和 2026-08-06，锚点固定为 2026-08-04；未来日期需要重新采集双快照并单独授权。
- 扩展范围仅为 2026-08-04 时点的 300 只 CSI300 成分股及 `SH000300`，`market=all` 在 8 月 4 日以后会被拒绝。
- 不能通过降低误差阈值、删除异常股票、把接口缺失当停牌或修改基础 Provider 来绕过审计。
- 腾讯前复权历史会因后续公司行为回写。历史扩展必须使用目标日留存的不可变快照，不得用后续实时查询替代。

## 风险

腾讯公开接口没有服务等级保证，也没有替代商业数据授权。该流程适合可审计的研究和自建模拟盘前向验证；用于长期生产前仍需评估接口稳定性和数据使用条款。
