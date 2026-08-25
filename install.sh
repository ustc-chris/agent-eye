#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
用法：./install.sh

环境变量：
  AGENT_EYE_HOME      源码目录；默认是本脚本所在目录。
  AGENT_EYE_BIN_DIR   命令链接目录；默认是 $HOME/.local/bin。

脚本不会复制源码，安装后必须保留源码目录。
EOF
}

if [[ $# -ne 0 ]]; then
    case "$*" in
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "install.sh: 未知参数：$*" >&2
            usage >&2
            exit 2
            ;;
    esac
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
AGENT_EYE_SOURCE="${AGENT_EYE_HOME:-${SCRIPT_DIR}}"
AGENT_EYE_SOURCE="$(cd -- "${AGENT_EYE_SOURCE}" && pwd)"
BIN_DIR="${AGENT_EYE_BIN_DIR:-${HOME}/.local/bin}"
SOURCE="${AGENT_EYE_SOURCE}/eye"
TARGET="${BIN_DIR}/eye"

if [[ ! -x "${SOURCE}" ]]; then
    echo "install.sh: 找不到可执行文件：${SOURCE}" >&2
    exit 1
fi

mkdir -p -- "${BIN_DIR}"

if [[ -e "${TARGET}" || -L "${TARGET}" ]]; then
    EXISTING_TARGET="$(readlink "${TARGET}" 2>/dev/null || true)"
    if [[ "${EXISTING_TARGET}" == "${SOURCE}" ]]; then
        echo "eye 已安装：${TARGET}"
        exit 0
    fi
    echo "install.sh: 拒绝覆盖已有路径：${TARGET}" >&2
    exit 1
fi

ln -s -- "${SOURCE}" "${TARGET}"
echo "已安装 eye -> ${TARGET}"
echo "AGENT_EYE_HOME=${AGENT_EYE_SOURCE}"

case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *)
        echo "请将以下设置加入 Shell 或 cron 环境："
        echo "  export AGENT_EYE_HOME=\"${AGENT_EYE_SOURCE}\""
        echo "  export PATH=\"${BIN_DIR}:\${PATH}\""
        ;;
esac
