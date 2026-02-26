#!/usr/bin/env bash
set -euo pipefail

# ========= 可按需改动 =========
REPO_URL="https://github.com/ForgottenZ/alist-alo.git"
BRANCH="${BRANCH:-codex/fix-compression-function-performance-issues-ib9k40}"
CLONE_DIR="${CLONE_DIR:-$HOME/alist-alo}"

# Go 版本：可通过环境变量覆盖，例如：GO_VERSION=1.22.0 ./build_alist.sh
GO_VERSION="${GO_VERSION:-1.22.5}"

# Node 主版本默认 20；如需覆盖：NODE_MAJOR_DEFAULT=18 ./build_alist.sh
NODE_MAJOR_DEFAULT="${NODE_MAJOR_DEFAULT:-20}"
# ==============================

DATE_TAG="$(date +%Y%m%d)"
IMAGE_TAG="luobo/alist:v${DATE_TAG}"

need_cmd() { command -v "$1" >/dev/null 2>&1; }

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    bash -lc "$*"
  else
    sudo bash -lc "$*"
  fi
}

install_go() {
  echo "[Go] 安装/更新 Go ${GO_VERSION} ..."
  local arch uname_arch url tmp
  uname_arch="$(uname -m)"
  case "$uname_arch" in
    x86_64|amd64) arch="amd64" ;;
    aarch64|arm64) arch="arm64" ;;
    armv7l|armv6l) arch="armv6l" ;;
    *)
      echo "不支持的 CPU 架构：$uname_arch"
      exit 1
      ;;
  esac

  # 如果已安装且版本一致就跳过
  if need_cmd go; then
    local cur
    cur="$(go version | awk '{print $3}' | sed 's/^go//')"
    if [ "$cur" = "$GO_VERSION" ]; then
      echo "[Go] 已是目标版本：${cur}，跳过。"
      return 0
    fi
    echo "[Go] 当前版本：${cur}，将升级/切换到：${GO_VERSION}"
  fi

  url="https://go.dev/dl/go${GO_VERSION}.linux-${arch}.tar.gz"
  tmp="/tmp/go${GO_VERSION}.tar.gz"

  curl -fsSL "$url" -o "$tmp"

  # 安装到 /usr/local/go
  as_root "rm -rf /usr/local/go && tar -C /usr/local -xzf '$tmp'"

  rm -f "$tmp"

  # 让系统登录后也能找到 go（不依赖当前 shell）
  as_root "install -d /etc/profile.d && printf '%s\n' 'export PATH=/usr/local/go/bin:\$PATH' > /etc/profile.d/golang.sh && chmod 0644 /etc/profile.d/golang.sh"

  # 让当前脚本立即可用
  export PATH="/usr/local/go/bin:$PATH"
  mkdir -p "$HOME/go/bin"
  export GOPATH="${GOPATH:-$HOME/go}"
  export PATH="$PATH:$GOPATH/bin"

  echo "[Go] 安装完成：$(go version)"
}

echo "[1/8] 安装基础依赖（curl/git/ca-certificates 等）..."
if need_cmd apt-get; then
  as_root "apt-get update -y && apt-get install -y curl git ca-certificates gnupg lsb-release build-essential tar xz-utils"
elif need_cmd dnf; then
  as_root "dnf install -y curl git ca-certificates gnupg2 tar xz"
elif need_cmd yum; then
  as_root "yum install -y curl git ca-certificates gnupg2 tar xz"
elif need_cmd apk; then
  as_root "apk add --no-cache curl git ca-certificates bash tar xz"
else
  echo "不支持的发行版：未检测到 apt/dnf/yum/apk。请手动安装 curl/git。"
  exit 1
fi

echo "[2/8] 安装 Docker（若已安装则跳过）..."
if ! need_cmd docker; then
  as_root "curl -fsSL https://get.docker.com | sh"
fi
if need_cmd systemctl; then
  as_root "systemctl enable --now docker || true"
fi

# 尽量让当前脚本也能用 docker：若无权限则自动用 sudo docker
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  DOCKER="sudo docker"
fi

echo "[3/8] 安装 Golang ..."
install_go

echo "[4/8] clone 仓库并切到分支：$BRANCH ..."
if [ -d "${CLONE_DIR}/.git" ]; then
  git -C "$CLONE_DIR" fetch --all --prune
  git -C "$CLONE_DIR" checkout "$BRANCH"
  git -C "$CLONE_DIR" pull --ff-only || true
else
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$CLONE_DIR"
fi

echo "[5/8] 解析项目所需 pnpm / Node 版本（从 alist-web/package.json）..."
PKG_JSON="${CLONE_DIR}/alist-web/package.json"
PNPM_VERSION=""
NODE_MAJOR=""

if [ -f "$PKG_JSON" ]; then
  PNPM_VERSION="$(grep -oE '"packageManager"[[:space:]]*:[[:space:]]*"pnpm@[^"]+"' "$PKG_JSON" \
    | head -n1 | sed -E 's/.*pnpm@([^"]+)".*/\1/')"

  # 用 @types/node 的主版本号作为 Node 主版本的参考（如 ^20.0.0 -> 20）
  NODE_MAJOR="$(grep -oE '"@types/node"[[:space:]]*:[[:space:]]*"\^[0-9]+' "$PKG_JSON" \
    | head -n1 | grep -oE '[0-9]+' | head -n1)"
fi

PNPM_VERSION="${PNPM_VERSION:-9.9.0}"
NODE_MAJOR="${NODE_MAJOR:-$NODE_MAJOR_DEFAULT}"

echo "  - 将使用 Node 主版本：${NODE_MAJOR}"
echo "  - 将激活 pnpm 版本：${PNPM_VERSION}"

echo "[6/8] 安装 Node.js（nvm）并启用/激活 pnpm（corepack）..."
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [ ! -s "$NVM_DIR/nvm.sh" ]; then
  curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
fi
# shellcheck disable=SC1090
source "$NVM_DIR/nvm.sh"

nvm install "$NODE_MAJOR"
nvm use "$NODE_MAJOR"

if need_cmd corepack; then
  corepack enable
  corepack prepare "pnpm@${PNPM_VERSION}" --activate
else
  npm i -g "pnpm@${PNPM_VERSION}"
fi

echo "[7/8] 执行 run_me_to_autobuild.sh -update ..."
AUTO_SCRIPT="$(find "$CLONE_DIR" -maxdepth 4 -name run_me_to_autobuild.sh | head -n1 || true)"
if [ -z "$AUTO_SCRIPT" ]; then
  echo "错误：未在仓库中找到 run_me_to_autobuild.sh（请确认该分支确实包含该脚本）。"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$AUTO_SCRIPT")" && pwd)"
cd "$SCRIPT_DIR"
bash "./$(basename "$AUTO_SCRIPT")" -update

echo "[8/8] Docker build：${IMAGE_TAG}"
# 优先在脚本所在目录构建；若这里没 Dockerfile，则尝试找一个 Dockerfile 并在其目录构建
if [ ! -f "$SCRIPT_DIR/Dockerfile" ]; then
  DF="$(find "$CLONE_DIR" -maxdepth 4 -name Dockerfile | head -n1 || true)"
  if [ -n "$DF" ]; then
    SCRIPT_DIR="$(cd "$(dirname "$DF")" && pwd)"
    cd "$SCRIPT_DIR"
  fi
fi

$DOCKER build -t "$IMAGE_TAG" .

echo "✅ 完成：$IMAGE_TAG"
echo "   构建目录：$SCRIPT_DIR"
echo "   Go：$(go version)"
echo "   Node：$(node -v)"
echo "   pnpm：$(pnpm -v)"
