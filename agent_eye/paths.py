"""根据当前源码位置确定稳定路径。"""

from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
REPOSITORY_DIR = PACKAGE_DIR.parent
CONFIG_DIR = REPOSITORY_DIR / "agent_eye" / "config"
TEST_DIR = REPOSITORY_DIR / "test"
RUNTIME_DIR = "/tmp/.agent_eye"
