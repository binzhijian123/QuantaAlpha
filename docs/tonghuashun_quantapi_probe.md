# 同花顺 QuantAPI 隔离验证

本工具只验证同花顺 iFinD QuantAPI 的账号鉴权与 PIT 字段返回结构。它不是 QuantaAlpha 数据 Provider，不写原始数据，不读取价格或收益，也不会生成因子、模型或模拟盘信号。

官方依据：

- [QuantAPI 下载与 HTTP API 手册](https://quantapi.10jqka.com.cn/gwstatic/static/ds_web/quantapi-web/download.html)
- [时点数据应用示例](https://quantapi.10jqka.com.cn/gwstatic/static/ds_web/quantapi-web/example.html)
- [数据权限说明](https://quantapi.10jqka.com.cn/gwstatic/static/ds_web/quantapi-web/help-center/permission.html)

## 1. 获取 Refresh Token

1. 在网页超级命令完成登录和安全验证。
2. 打开“账号信息”，先检查账号类型、有效期和数据权限。
3. 查看当前 Refresh Token；只有当前值无效时才更新。更新 Refresh Token 会使旧 Refresh Token 和 Access Token 全部失效。
4. 只在本机 `.env` 配置 `IFIND_REFRESH_TOKEN`，不要把值发到聊天、日志、截图、请求 JSON 或源码中。

```dotenv
IFIND_REFRESH_TOKEN=<本机当前有效值>
```

## 2. 鉴权预检

```bash
source scripts/qa_env.sh
python scripts/tonghuashun_quantapi_probe.py --preflight
```

预检只调用官方 `get_access_token`，不会调用会消耗数据额度的字段接口。工具不会打印或持久化 Refresh Token/Access Token，也不会调用会使旧 Access Token 失效的 `update_access_token`。

2026-08-04 的真实预检结果是 `BLOCKED`：本机现有 `IFIND_REFRESH_TOKEN` 被官方接口判定为 expired or illegal。预检前后 `data/` 文件清单哈希均为 `82659300c31b8e3f536801fc9b63d791d5fb951a7c20259c9727c66c3572cf61`，没有生成数据产物。

## 3. 字段结构探针

鉴权通过后，在网页超级命令生成 `basic_data_service` 或 `date_sequence` 的 formData，保存为不含凭据的 JSON。先用两只股票和最小日期范围；不要在 JSON 中加入 token、password 或 secret 字段。

```bash
source scripts/qa_env.sh
python scripts/tonghuashun_quantapi_probe.py \
  --field-probe \
  --service date_sequence \
  --request-json /absolute/path/to/super-command-formdata.json \
  --confirm-quota-use
```

`--confirm-quota-use` 是强制开关。输出只包含 `errorcode`、证券代码、字段名、行数、格式和 `dataVol`，不会显示单元格值。

## 4. 数据准入条件

QuantAPI 只有同时满足以下条件，才允许进入新的协议版本；不得改写已冻结的 v2.2 Tushare 注册表：

1. 账号是可覆盖证券上市以来历史的正式权限；免费版/试用版最近五年范围不足以覆盖当前 2016 起始训练期。
2. 两股探针可以按历史时点取得财报变更日期、实际披露日期和目标盈利字段。
3. 同一报告期的历史修订版本可被区分，且每个版本只能在当时可知日期后的首个交易日生效。
4. 无收益全量同步覆盖至少 600 只历史成分股，通过覆盖率、时点、修订和确定性复用审计。
5. 以上通过后才创建 v2.3 候选定义并读取因子值；因子准入通过后才允许读取收益和运行正式验证。

当前结论：HTTP 客户端和脱敏边界已验证，账号/历史权限和字段权限尚未验证，因此 QuantAPI 仍是候选数据源，不是可用 Provider。
