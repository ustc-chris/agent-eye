"""NPU 状态查询实现。"""

from __future__ import annotations

import subprocess
import sys


def run() -> int:
    """使用厂商命令打印 NPU 状态。"""
    try:
        completed = subprocess.run(["npu-smi", "info"], check=False)
    except FileNotFoundError:
        print("eye: npu-smi was not found in PATH", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"eye: failed to execute npu-smi: {exc}", file=sys.stderr)
        return 126
    return completed.returncode
