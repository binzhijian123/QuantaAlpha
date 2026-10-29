# QuantaAlpha 从零启动教程

本教程面向第一次安装的使用者，按顺序完成后，可以在自己的电脑打开网页、配置自己的 API 和数据，再手动启动研究任务。命令适用于 macOS / Linux 的终端。Windows 原生启动流程尚未在本轮验证。

源码仓库：<https://github.com/binzhijian123/QuantaAlpha>。

先完成第 1～4 步看到网页；准备好 API 和数据后，再做第 5～7 步。**网页能打开、API 能保存、研究能运行、自动影子盘验收通过，是不同的完成状态。** 当前自动影子盘仍未完成真实运行验收，普通安装不需要开启它。

## 1. 准备三个工具

| 工具 | 用途 | 安装后检查 |
| --- | --- | --- |
| Git | 下载和更新源码 | `git --version` |
| Miniforge / Conda | 管理 Python 环境 | `conda --version` |
| Node.js（含 npm） | 构建网页 | `node --version`、`npm --version` |

Conda 可使用 [Miniforge 官方安装器](https://github.com/conda-forge/miniforge)。Mac 选择与处理器匹配的版本：Apple 芯片选 arm64，Intel 选 x86_64。安装完成后重新打开终端。

Node.js 从 [官网](https://nodejs.org/en/download)选择仍在维护的 LTS 版本。项目最低要求为 Node.js 18+；最低要求不代表旧版本仍受官方维护。本轮实际构建环境为 Python 3.10.20、Node.js 20.20.2、npm 10.8.2，尚未逐一验证所有新版本组合。

如果 `conda` 提示找不到，先按自己实际安装位置加载它。例如使用默认 Miniforge 路径时：

```bash
source "$HOME/miniforge3/etc/profile.d/conda.sh"
```

## 2. 下载项目、创建 Python 环境

打开终端，执行：

```bash
git clone https://github.com/binzhijian123/QuantaAlpha.git
cd QuantaAlpha
conda create -n quantaalpha python=3.10 -y
conda activate quantaalpha
```

下面的命令都从刚下载的 `QuantaAlpha` 根目录开始。这里应能看到 `pyproject.toml`、`requirements.txt` 和 `frontend-v2/`。

安装 Python 依赖：

```bash
python -m pip install --upgrade pip
python -m pip install -e .
python -m pip install -r frontend-v2/backend/requirements.txt
```

首次安装会下载科学计算、Qlib 和模型工作流依赖，耗时取决于网络与电脑。必须等命令成功结束；不要看到部分包下载成功就跳到下一步。

检查安装的是当前终端所用的 Python：

```bash
python -c "import sys, quantaalpha, fastapi, uvicorn; print(sys.executable); print('Python dependencies OK')"
```

如果下载的是 ZIP，或安装时明确提示 `setuptools-scm` 无法确定版本，请优先改用上面的 `git clone`；也可以仅为这次本地开发安装指定版本：

```bash
SETUPTOOLS_SCM_PRETEND_VERSION=0.1.0 python -m pip install -e .
```

这只影响安装包的版本号，不代表运行验收或正式发行版本。

## 3. 构建网页

仍在项目根目录，执行：

```bash
cd frontend-v2
npm ci
npm run build
cd ..
```

看到构建成功，且 `frontend-v2/dist/` 已生成，再继续。这个目录由构建生成，GitHub 不附带它。

## 4. 启动项目、打开网页

在项目根目录执行：

```bash
conda activate quantaalpha
python frontend-v2/backend/local_app.py
```

保持这个终端运行，在浏览器打开：

**<http://127.0.0.1:8000/>**

这一种启动方式由后端直接提供已构建的网页，**不用另开 3000 端口，也不用运行 `npm run dev`**。应使用 `local_app.py`；`npm run backend` 仍指向历史 `app.py`，不用于本教程。

可以另开一个终端确认后端状态：

```bash
curl --noproxy '*' http://127.0.0.1:8000/api/health
```

返回 `"status":"healthy"` 表示服务已响应。没有 `.env`、没有行情数据、没有 API Key 时，也可以先打开网页；初始列表为空、自动 worker 关闭都是正常状态。

## 5. 在“设置”里填写自己的 API

进入左侧 **设置 → 配置 API**，按服务商控制台的信息填写：

| 网页字段 | 填什么 |
| --- | --- |
| API Key | 你自己的服务商密钥，只在网页输入，不要贴到 GitHub |
| API Base URL | 服务商提供的 OpenAI 兼容接口地址；不要填写控制台网页地址 |
| 聊天模型 ID | 该账户有权调用的完整模型名称 |
| 推理模型 ID | 该账户有权调用的完整模型名称；可以与聊天模型相同 |

保存后进入 **环境与说明**，刷新检查。

- 首次保存会在这台后端电脑创建 `.env`。不需要复制开发者的环境文件，也不必手动改代码。
- 密钥不回传到页面或存入浏览器 localStorage；以后留空密钥再保存，会保留服务器已存的密钥。
- 页面检查配置格式、依赖和路径，不发出外部模型调用。**保存成功不等于鉴权、模型权限和额度已经验证。** 实际模型调用在你启动研究任务后发生，可能产生服务商费用。
- API、数据路径和结果目录属于这台后端；多个人共享一台后端，也会共享这些配置，不是每人独立账户。

## 6. 准备自己的行情数据

只看网页可以先跳过；挖掘和回测需要真实、可读取的 Qlib 数据。源码不包含行情、历史因子库或别人的研究结果。

### 已有 Qlib 数据

在设置页填写 **Provider 根目录**，不要指向 ZIP 或内部 `features/` 子目录。目录结构至少应类似：

```text
my_daily_cn/
├── calendars/
│   └── day.txt
├── instruments/
│   └── ...
└── features/
    └── <symbol>/
        └── *.bin
```

**结果输出目录**填写后端电脑上可写的位置，例如 `data/results`。相对路径以仓库根目录为基准；也可以从终端 `pwd` 获取完整路径。网页路径字段不要填写 `$HOME` 等 Shell 变量。

### 没有 Qlib 数据

项目提供日线数据安装工具。先查看参数：

```bash
python scripts/install_investment_data_release.py --help
```

如决定使用该工具支持的数据源，执行：

```bash
python scripts/install_investment_data_release.py --output-provider data/market/a_share/qlib/my_daily_cn
```

它会下载并校验数据，成功后在设置页把数据路径填写为：

```text
data/market/a_share/qlib/my_daily_cn
```

此命令没有 `--switch`，不需要替换用户全局的 `~/.qlib` 链接。目标目录必须是新的；已有成功安装的数据可以直接使用，不要反复覆盖。网络、数据包是否完整及发布源状态会影响安装，下载失败时不能当作数据已就绪。

如果另外使用官方成分表 XLS 解析工具，可按发布副本的可选依赖说明安装 `xlrd`；普通网页启动本身不需要它。

```bash
python -m pip install 'xlrd>=2.0.1,<3'
```

数据目录结构通过检查，只证明目录可读；研究所需股票、日期、字段和数据资格仍需按自己的任务核对。

## 7. 手动跑第一个小任务

1. 保存 API 和数据路径，在“环境与说明”确认核心依赖和目录检查正常。
2. 在设置页把首次试跑的 **初始探索分支数、进化总轮次、每任务循环数**设为小值，例如各为 1。1 个任务循环仍包含 5 个工作流步骤，可能不止一次模型请求。
3. 进入 **因子挖掘**，填写一个小范围研究方向，例如：“基于已有价格和成交量数据研究短期反转因子，检查数据覆盖并避免未来数据泄露。”
4. 手动启动，查看进度和诊断。终端也应保持运行。
5. 有有效产出后，进入 **因子库**查看结果，再到 **回测**选择对应因子库或缓存。新安装没有因子时，列表为空不表示软件损坏。

`401/403` 优先核对密钥及账户权限；模型不存在时核对完整模型 ID；缺少字段或日期时核对自己的数据。不要靠反复点击启动来排查配置错误。

## 8. 下次启动、停止和更新

下次不用重复安装依赖、构建前端。进入项目目录、激活环境、启动即可：

```bash
cd /实际的项目目录/QuantaAlpha
conda activate quantaalpha
python frontend-v2/backend/local_app.py
```

等自己的研究任务完成后，在启动终端按 **Ctrl+C** 停止这个前台服务。不要用 `killall python`，也不要强行停止其他人在使用的服务。

更新源码前保留自己的配置和研究数据：

```bash
git pull --ff-only
```

如果依赖或前端源码有变化，再按第 2、3 步更新依赖并重建网页。`.env` 和研究结果不随仓库同步；不要用清空目录的方式“重新安装”。

## 常见问题

| 现象 | 检查办法 |
| --- | --- |
| 找不到 `conda` | 重新打开终端；按自己的安装位置加载 `conda.sh` |
| `ModuleNotFoundError` | 先 `conda activate quantaalpha`，再用 `python -m pip` 安装；确认 `sys.executable` 属于这个环境 |
| 网页空白或找不到页面 | 确认执行过 `npm ci`、`npm run build`，且在根目录启动 `local_app.py` |
| 8000 已被占用 | 先确认它是否已经是本项目；不要直接强杀。仅想启动另一份副本时，可用下面的独立端口命令 |
| 保存设置失败 | 检查后端是否在线、安装目录是否可写、Base URL 是否正确，字段中不要放换行、反引号或 `$` |
| API 保存了却调用失败 | 保存不会测试鉴权；查看实际任务错误，核对账户额度和模型权限 |
| 有网页但没有因子、回测列表为空 | 先提供自己的数据并成功生成因子；源码不附带研究结果 |

独立端口示例：

```bash
BACKEND_PORT=8013 python frontend-v2/backend/local_app.py
```

此时打开 <http://127.0.0.1:8013/>。已用 8000 启动成功时不必改端口。

## 开发模式（需要改前端时再用）

日常使用按前面的 8000 构建模式即可。前端开发可使用：

```bash
bash frontend-v2/start.sh
```

该脚本启动或复用后端 8000 与 Vite 3000，环境入口为 `scripts/qa_env.sh`。默认查找 `~/miniforge3`；其他路径或环境名需显式指定，例如：

```bash
QA_CONDA_HOME="$HOME/miniconda3" CONDA_ENV_NAME=my-research bash frontend-v2/start.sh
```

这些环境要预先存在。不要同时照着两种启动方式重复启动服务；网页环境检查也不会替你安装 Conda、创建环境或切换解释器。

## 验证范围与自动影子盘状态

本轮验证了公开源码的 Python 3.10 隔离编辑安装入口（复用已安装依赖）、前端 `npm ci` / `npm run build`，以及无 `.env` 的独立端口启动、配置不回传密钥、网页与说明入口 HTTP 200。没有替使用者执行付费模型请求，也没有声称在每个全新操作系统上从空环境装完全部依赖并完成真实挖掘。

自动影子盘仍需真实来源资格、完整 242 槽校准和 V10 决策、执行、日结、维护、重启验收。普通使用者不要复制开发工作机的声明、账本或恢复开关来开启它。源码已经按用户授权同步，运行验收仍独立推进。

进一步阅读：[公开交接](HANDOFF_PUBLIC.md) · [恢复与验收](SHADOW_RECOVERY.md) · [V10 运行接线](V10_RUNTIME_PLAN.md)。
