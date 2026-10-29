#!/bin/bash


echo "🚀 启动 QuantaAlpha AI V2..."
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# =============================================================================
# 激活 conda 环境（固定使用项目公共入口，避免非交互 shell 找不到 conda）
# =============================================================================
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/scripts/qa_env.sh" || exit 1

# =============================================================================
# 检查 Node.js
# =============================================================================
if ! command -v node &> /dev/null; then
    echo "❌ 错误: 未找到 Node.js"
    echo "请先安装 Node.js: https://nodejs.org/"
    exit 1
fi
echo "✅ Node.js: $(node --version)"

if ! python -c "import quantaalpha" 2>/dev/null; then
    echo "❌ 错误: quantaalpha 包未安装"
    echo "请先运行: source ${PROJECT_ROOT}/scripts/qa_env.sh && cd ${PROJECT_ROOT} && pip install -e ."
    exit 1
fi
echo "✅ Python: $(python --version) (conda env: ${QA_CONDA_ENV})"

# =============================================================================
# 加载 .env 配置
# =============================================================================
if [ -f "${PROJECT_ROOT}/.env" ]; then
    echo "✅ 后端将按 dotenv 格式读取 .env 配置（启动脚本不执行配置内容）"
else
    echo "⚠️  未找到 .env 文件，后端将使用默认配置"
fi

# =============================================================================
# 安装前端依赖
# =============================================================================
cd "${SCRIPT_DIR}"
if [ ! -d "node_modules" ]; then
    echo ""
    echo "📦 安装前端依赖..."
    npm install
    if [ $? -ne 0 ]; then
        echo "❌ 前端依赖安装失败"
        exit 1
    fi
    echo "✅ 前端依赖安装完成"
fi

# =============================================================================
# 安装后端依赖（在 conda 环境中）
# =============================================================================
echo "📦 检查/安装后端 Python 依赖..."
pip install -q -r "${SCRIPT_DIR}/backend/requirements.txt" 2>/dev/null || true
echo "✅ 后端依赖就绪"

# =============================================================================
# 获取本机 IP（用于多用户访问提示）
# =============================================================================
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "$HOST_IP" ]; then
    HOST_IP="localhost"
fi

# =============================================================================
# 检测并启动后端（复用已有服务 or 重新启动）
# =============================================================================
BACKEND_PID=""
BACKEND_REUSED=false

echo ""
echo "🔍 检测后端服务 (端口 8000)..."
if curl -s --connect-timeout 2 http://localhost:8000/api/health > /dev/null 2>&1; then
    if ! curl --fail --silent --connect-timeout 2 http://localhost:8000/api/v1/system/environment > /dev/null; then
        echo "❌ 当前后端不是新的可配置入口。请通过原管理方式停止旧服务，再运行本脚本。"
        exit 1
    fi
    echo "✅ 后端服务已在运行中 (端口 8000)，复用现有服务"
    BACKEND_REUSED=true
    BACKEND_PID=$(lsof -ti:8000 2>/dev/null | head -1)
else
    # 清理可能占用端口但未正常服务的残留进程
    OLD_PID=$(lsof -ti:8000 2>/dev/null)
    if [ -n "$OLD_PID" ]; then
        echo "❌ 端口 8000 已被其他进程占用，请通过对应服务的管理入口处理后重试。"
        exit 1
    fi

    echo "🔧 启动后端服务 (端口 8000)..."
    cd "${SCRIPT_DIR}"
    python backend/local_app.py &
    BACKEND_PID=$!

    # 等待后端启动
    sleep 3

    if curl -s http://localhost:8000/api/health > /dev/null 2>&1; then
        echo "✅ 后端服务启动成功 (PID: $BACKEND_PID)"
    else
        echo "❌ 后端启动失败，请检查日志"
        kill $BACKEND_PID 2>/dev/null
        exit 1
    fi
fi

# =============================================================================
# 检测并启动前端（复用已有服务 or 重新启动）
# =============================================================================
FRONTEND_PID=""
FRONTEND_REUSED=false

echo ""
echo "🔍 检测前端服务 (端口 3000)..."
if curl -s --connect-timeout 2 http://localhost:3000 > /dev/null 2>&1; then
    echo "✅ 前端服务已在运行中 (端口 3000)，复用现有服务"
    FRONTEND_REUSED=true
    FRONTEND_PID=$(lsof -ti:3000 2>/dev/null | head -1)
else
    # 清理可能占用端口但未正常服务的残留进程
    OLD_PID=$(lsof -ti:3000 2>/dev/null)
    if [ -n "$OLD_PID" ]; then
        echo "❌ 端口 3000 已被其他进程占用，请通过对应服务的管理入口处理后重试。"
        exit 1
    fi

    echo "🎨 启动前端服务 (端口 3000)..."
    cd "${SCRIPT_DIR}"
    npm run dev &
    FRONTEND_PID=$!
    sleep 3
fi

echo ""
echo "============================================"
echo "✅ 所有服务启动完成!"
echo ""
echo "📍 访问地址:"
echo "   本机:     http://localhost:3000"
if [ "$HOST_IP" != "localhost" ]; then
echo "   局域网:   http://${HOST_IP}:3000"
fi
echo "   后端 API: http://localhost:8000"
echo "   API 文档: http://localhost:8000/docs"
echo ""
if [ "$BACKEND_REUSED" = true ] || [ "$FRONTEND_REUSED" = true ]; then
echo "ℹ️  部分服务为复用已有进程（多用户共享模式）"
echo "   Ctrl+C 仅停止本脚本启动的服务，不影响其他用户"
fi
echo ""
echo "按 Ctrl+C 停止服务"
echo "============================================"
echo ""

# 捕获退出信号 — 只杀自己启动的进程，不杀复用的
cleanup() {
    echo ""
    echo "🛑 停止服务..."
    if [ "$BACKEND_REUSED" = false ] && [ -n "$BACKEND_PID" ]; then
        kill $BACKEND_PID 2>/dev/null
        echo "  已停止后端 (PID: $BACKEND_PID)"
    else
        echo "  后端为共享服务，保持运行"
    fi
    if [ "$FRONTEND_REUSED" = false ] && [ -n "$FRONTEND_PID" ]; then
        kill $FRONTEND_PID 2>/dev/null
        echo "  已停止前端 (PID: $FRONTEND_PID)"
    else
        echo "  前端为共享服务，保持运行"
    fi
    echo "✅ 完成"
    exit 0
}
trap cleanup SIGINT SIGTERM

# 等待子进程
wait
