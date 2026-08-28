#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
用法：./install.sh

环境变量：
  AGENT_EYE_HOME      源码目录；默认是本脚本所在目录。
  AGENT_EYE_BIN_DIR   命令链接目录；默认是 $HOME/.local/bin。
  AGENT_EYE_MAN_DIR   man page 链接目录；默认是 $HOME/.local/share/man/man1。

脚本不会复制源码，只会创建 eye 和 eye.1 的符号链接；安装后必须保留源码目录。
如果目标是失效链接，或指向另一个 Agent Eye 源码树的旧链接，安装时会自动更新。
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
MAN_DIR="${AGENT_EYE_MAN_DIR:-${HOME}/.local/share/man/man1}"
SOURCE="${AGENT_EYE_SOURCE}/eye"
TARGET="${BIN_DIR}/eye"
MAN_SOURCE="${AGENT_EYE_SOURCE}/docs/eye.1"
MAN_TARGET="${MAN_DIR}/eye.1"

if [[ ! -x "${SOURCE}" ]]; then
    echo "install.sh: 找不到可执行文件：${SOURCE}" >&2
    exit 1
fi
if [[ ! -f "${MAN_SOURCE}" ]]; then
    echo "install.sh: 找不到 man page：${MAN_SOURCE}" >&2
    exit 1
fi

linked_source_root() {
    local target="$1"
    local kind="$2"
    local linked_path
    linked_path="$(readlink "${target}" 2>/dev/null || true)"
    if [[ -z "${linked_path}" ]]; then
        return 1
    fi
    if [[ "${linked_path}" != /* ]]; then
        linked_path="$(dirname -- "${target}")/${linked_path}"
    fi

    case "${kind}" in
        command)
            [[ "$(basename -- "${linked_path}")" == "eye" ]] || return 1
            dirname -- "${linked_path}"
            ;;
        manpage)
            [[ "$(basename -- "${linked_path}")" == "eye.1" ]] || return 1
            dirname -- "$(dirname -- "${linked_path}")"
            ;;
        *)
            return 1
            ;;
    esac
}

is_agent_eye_source_link() {
    local target="$1"
    local kind="$2"
    local source_root
    source_root="$(linked_source_root "${target}" "${kind}")" || return 1
    [[ -f "${source_root}/eye" ]] || return 1
    [[ -f "${source_root}/install.sh" ]] || return 1
    [[ -f "${source_root}/agent_eye/cli.py" ]] || return 1
    [[ -f "${source_root}/docs/eye.1" ]] || return 1
}

check_target() {
    local target="$1"
    local source="$2"
    local kind="$3"
    if [[ ! -e "${target}" && ! -L "${target}" ]]; then
        return
    fi
    if [[ ! -L "${target}" ]]; then
        echo "install.sh: 拒绝覆盖已有路径：${target}" >&2
        exit 1
    fi

    # A broken link protects no live target. Replacing the installer-owned
    # destination removes only that unusable link, without deleting source data.
    if [[ ! -e "${target}" ]]; then
        return
    fi

    local existing_target
    existing_target="$(readlink "${target}" 2>/dev/null || true)"
    if [[ "${existing_target}" == "${source}" ]]; then
        return
    fi
    if ! is_agent_eye_source_link "${target}" "${kind}"; then
        echo "install.sh: 拒绝覆盖非 Agent Eye 链接：${target}" >&2
        exit 1
    fi
}

check_target "${TARGET}" "${SOURCE}" command
check_target "${MAN_TARGET}" "${MAN_SOURCE}" manpage

mkdir -p -- "${BIN_DIR}" "${MAN_DIR}"

if [[ ! -L "${TARGET}" ]]; then
    ln -s -- "${SOURCE}" "${TARGET}"
    echo "已安装 eye -> ${TARGET}"
elif [[ "$(readlink "${TARGET}")" != "${SOURCE}" ]]; then
    ln -sfn -- "${SOURCE}" "${TARGET}"
    echo "已更新 eye -> ${TARGET}"
else
    echo "eye 已安装：${TARGET}"
fi
if [[ ! -L "${MAN_TARGET}" ]]; then
    ln -s -- "${MAN_SOURCE}" "${MAN_TARGET}"
    echo "已安装 man eye -> ${MAN_TARGET}"
elif [[ "$(readlink "${MAN_TARGET}")" != "${MAN_SOURCE}" ]]; then
    ln -sfn -- "${MAN_SOURCE}" "${MAN_TARGET}"
    echo "已更新 man eye -> ${MAN_TARGET}"
else
    echo "man eye 已安装：${MAN_TARGET}"
fi
echo "AGENT_EYE_HOME=${AGENT_EYE_SOURCE}"

case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *)
        echo "请将以下设置加入 Shell 或 cron 环境："
        echo "  export AGENT_EYE_HOME=\"${AGENT_EYE_SOURCE}\""
        echo "  export PATH=\"${BIN_DIR}:\${PATH}\""
        ;;
esac

if [[ -n "${AGENT_EYE_MAN_DIR:-}" ]]; then
    MAN_ROOT="$(dirname -- "${MAN_DIR}")"
    case ":${MANPATH:-}:" in
        *":${MAN_ROOT}:"*) ;;
        *)
            echo "自定义 man 目录时请确认 MANPATH 包含："
            echo "  export MANPATH=\"${MAN_ROOT}:\${MANPATH:-}\""
            ;;
    esac
fi
