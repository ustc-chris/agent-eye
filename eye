#!/usr/bin/env python3
"""Agent Eye 仓库内的可执行入口。"""

from __future__ import annotations

import os
import sys


REPOSITORY_ROOT = os.path.dirname(os.path.realpath(__file__))
os.environ["AGENT_EYE_HOME"] = REPOSITORY_ROOT
if REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, REPOSITORY_ROOT)

from agent_eye.cli import main  # noqa: E402，必须在设置源码路径后导入


if __name__ == "__main__":
    raise SystemExit(main())
