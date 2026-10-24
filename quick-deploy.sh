#!/usr/bin/env bash
# ==============================================================================
# WorkBuddy2API-Hub 一键安装与快速更新脚本
# 支持系统: Linux, macOS, NAS (fnOS / 群晖 / 威联通 / Unraid 等)
# 用法:
#   首次部署: curl -fsSL https://raw.githubusercontent.com/yuanyuanyuan430/workbuddy2api-hub/main/quick-deploy.sh | bash
#   快速更新: 再次运行相同的命令即可完成平滑无感升级（账号配置绝不丢失）
#   部署模式: WB_DEPLOY_MODE=local|lan|public（默认 lan）
#             local/public 只把 8788 绑到 127.0.0.1，公网机请再套 Nginx/Caddy/Tunnel
#             示例: curl -fsSL .../quick-deploy.sh | WB_DEPLOY_MODE=public bash
# ==============================================================================

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "\n${BLUE}======================================================${NC}"
echo -e "${GREEN}   WorkBuddy2API-Hub Docker 一键部署与更新脚本${NC}"
echo -e "${BLUE}======================================================${NC}\n"

# 0. 部署模式
#    本机 / 公网机器默认不要直接暴露 8788；容器内仍监听 0.0.0.0，
#    由 Docker 的 host 端绑定决定谁能连进来。
DEPLOY_MODE="${WB_DEPLOY_MODE:-lan}"
case "${DEPLOY_MODE}" in
    local|lan|public) ;;
    *)
        echo -e "${RED}[错误] WB_DEPLOY_MODE 只能是 local / lan / public，当前为: ${DEPLOY_MODE}${NC}"
        exit 1
        ;;
esac
if [ "${DEPLOY_MODE}" = "lan" ]; then
    PORT_BINDING="8788:8788"
else
    PORT_BINDING="127.0.0.1:8788:8788"
fi
echo -e "${BLUE}[信息] 部署模式: ${DEPLOY_MODE}（端口映射 ${PORT_BINDING}）${NC}"

# 1. 检查 Docker 环境
if ! command -v docker >/dev/null 2>&1; then
    echo -e "${RED}[错误] 未检测到 Docker，请先安装 Docker 后再运行此脚本。${NC}"
    exit 1
fi

# 检查 Docker Compose 支持
if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
else
    echo -e "${RED}[错误] 未检测到 Docker Compose 支持 (docker compose / docker-compose)。${NC}"
    exit 1
fi

# 2. 确定工作目录
APP_DIR="workbuddy2api-hub"
if [ -f "docker-compose.yml" ] && grep -q "wb-proxy" docker-compose.yml 2>/dev/null; then
    TARGET_DIR="$(pwd)"
    echo -e "${BLUE}[信息] 在当前目录检测到项目配置: ${TARGET_DIR}${NC}"
else
    TARGET_DIR="$(pwd)/${APP_DIR}"
    if [ ! -d "${TARGET_DIR}" ]; then
        echo -e "${BLUE}[信息] 创建安装目录: ${TARGET_DIR}${NC}"
        mkdir -p "${TARGET_DIR}"
    fi
    cd "${TARGET_DIR}"
fi

# 3. 首次安装 vs 更新判断
if [ -f "docker-compose.yml" ]; then
    echo -e "${YELLOW}[升级模式] 检测到已有部署，正在拉取最新镜像进行平滑更新...${NC}"
    if [ -n "${WB_DEPLOY_MODE:-}" ]; then
        echo -e "${YELLOW}[提示] 已有部署不会自动改写端口映射；如需限制监听地址，请手动编辑 docker-compose.yml 的 ports。${NC}"
    fi
    $COMPOSE_CMD pull
    $COMPOSE_CMD up -d
    echo -e "\n${GREEN}[成功] WorkBuddy2API-Hub 已成功更新至最新版本！${NC}"
else
    echo -e "${BLUE}[安装模式] 正在进行首次初始化部署...${NC}"
    cat > docker-compose.yml << EOF
services:
  wb-proxy:
    image: ghcr.io/yuanyuanyuan430/workbuddy2api-hub:latest
    container_name: wb-proxy
    restart: unless-stopped
    ports:
      - "${PORT_BINDING}"
    environment:
      - HOST=0.0.0.0
      - PORT=8788
      # - API_KEY=your_secret_key
      - TZ=Asia/Shanghai
    volumes:
      - ./accounts:/app/accounts
      - ./usage:/app/usage
EOF
    mkdir -p accounts usage
    echo -e "${BLUE}[信息] 正在拉取镜像并启动容器...${NC}"
    $COMPOSE_CMD up -d
    echo -e "\n${GREEN}[成功] WorkBuddy2API-Hub 首次部署成功！${NC}"
fi

# 4. 获取本地 IP 并输出引导提示
LOCAL_IP="127.0.0.1"
if command -v hostname >/dev/null 2>&1; then
    LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")
fi

if [ "${DEPLOY_MODE}" = "lan" ]; then
    SHOW_HOST="${LOCAL_IP:-127.0.0.1}"
else
    SHOW_HOST="127.0.0.1"
fi
echo -e "\n${BLUE}------------------------------------------------------${NC}"
echo -e "${GREEN}Web 控制看板:${NC} http://${SHOW_HOST}:8788"
echo -e "${GREEN}OpenAI 接口:${NC}  http://${SHOW_HOST}:8788/v1"
if [ "${DEPLOY_MODE}" = "public" ]; then
    echo -e "${YELLOW}公网部署:${NC} 8788 只绑在本机回环地址，请用 Nginx / Caddy / Cloudflare Tunnel 反代并加 TLS，不要直接把 8788 开到公网。"
fi
echo -e "${BLUE}------------------------------------------------------${NC}"
echo -e "${YELLOW}查看初始 API Key:${NC}  $COMPOSE_CMD logs wb-proxy | grep -i 'api key'"
echo -e "${YELLOW}查看实时运行日志:${NC}  $COMPOSE_CMD logs -f wb-proxy"
echo -e "${YELLOW}后续如何更新:${NC}      再次运行本脚本，或执行: $COMPOSE_CMD pull && $COMPOSE_CMD up -d"
echo -e "${BLUE}======================================================${NC}\n"
