# Agent Eye

Agent Eye 是一个零第三方 Python 依赖的轻量命令行工具。它通过统一的 `eye` 入口
查询 NPU 状态、在宿主机或 Docker 容器中运行命令，并按 tag 避免重复启动任务。

目标机器需要 Python 3。容器模式还需要 Docker CLI，以及容器内的 Bash、`pgrep`、
`tee` 和 `awk`；安全 kill 还需要可读取的 `/proc`。

## 直接使用和安装

在源码目录中可以直接运行：

```bash
./eye --help
./eye --version
```

`AGENT_EYE_HOME` 表示源码目录。未设置时，默认就是 `install.sh` 所在目录：

```bash
./install.sh
```

安装脚本不会复制源码，只会创建：

```text
$HOME/.local/bin/eye -> $AGENT_EYE_HOME/eye
```

可以显式指定源码和命令链接目录：

```bash
AGENT_EYE_HOME=/opt/agent_eye \
AGENT_EYE_BIN_DIR=/opt/bin \
./install.sh
```

SSH 或 cron 中建议显式设置：

```bash
export AGENT_EYE_HOME=/opt/agent_eye
export PATH="$HOME/.local/bin:$PATH"
```

## 命令概览

```text
eye [--allow WINDOW] npu_status
eye [--allow WINDOW] run    (--container NAME | --no-container) --exec COMMAND [OPTIONS]
eye [--allow WINDOW] ensure (--container NAME | --no-container) --tag TAG --exec COMMAND [OPTIONS]
eye [--allow WINDOW] kill [--container NAME] --tag TAG
eye [--allow WINDOW] from_file FILE
eye test
eye version
eye help [COMMAND]
```

同时支持 `eye --version`、`eye --help` 和各子命令的 `--help`。

## 允许时间

`--allow` 是 `npu_status`、`run`、`ensure`、`kill` 和 `from_file` 的最高优先级执行闸门。
检查发生在 NPU 查询、任务文件读取、pgrep、Docker 查询和命令执行之前：

```bash
eye --allow "mon:1500-2100,2200-2359;tue:0000-2359" npu_status
```

也可以把选项写在受限子命令之后：

```bash
eye run \
  --no-container \
  --exec "python3 task.py" \
  --allow "2100-2359,0000-0600"
```

语法分隔符：

```text
-   一个时间范围
,   同一天的多个时间范围
;   不同日期规则
```

支持的日期选择器是 `mon`、`tue`、`wed`、`thu`、`fri`、`sat`、`sun`、
`workday` 和 `weekends`。日期不区分大小写：

```bash
--allow "workday:0900-1200,1300-1800;weekends:1000-1600"
```

`workday` 固定表示周一到周五，`weekends` 固定表示周六和周日；它们不读取法定
节假日或调休信息。重叠规则取并集，例如：

```bash
--allow "workday:0900-1800;mon:2000-2200"
```

不写日期时，时间范围每天生效：

```bash
--allow "2100-2359,0000-0600"
```

时间严格使用四位 `HHMM`，范围按分钟包含两端。`2200-0600` 这样的跨午夜范围
会被拒绝，必须拆成 `2200-2359,0000-0600`；带日期时也要拆到对应的两天。

判断使用目标机器当前本地时区，并且只限制任务的开始时间，不会终止已经开始运行
的阻塞或 detach 任务。禁止时间内返回 0，并输出：

```text
SKIPPED reason=outside_allow now=mon:1430 timezone=CST allow="mon:1500-2100"
```

表达式错误返回 2。`test`、`help` 和 `version` 不受允许时间限制，即使全局传入
`--allow` 也不会检查它。

## NPU 状态

```bash
eye npu_status
```

该命令调用 `agent_eye/config/npu_status.py`，当前执行：

```bash
npu-smi info
```

## run

在宿主机运行：

```bash
eye run --no-container --exec "echo hello world"
```

在容器中运行：

```bash
eye run --container worker-0 --exec "python3 /workspace/task.py"
```

默认阻塞等待，实时输出命令内容，并返回命令的真实退出状态。只有显式增加
`--detach` 才会后台提交：

```bash
eye run \
  --no-container \
  --detach \
  --exec "sleep 10; echo finished" \
  --log /tmp/task.log
```

`run` 可以使用可选 tag，但不会因为同名 tag 已存在而跳过：

```bash
eye run --no-container --tag task-01 --exec "sleep 10"
```

## ensure

```bash
eye ensure \
  --container worker-0 \
  --tag training-01 \
  --exec "cd /workspace && python3 train.py"
```

`ensure` 首先检查 tag：

- tag 已存在：输出 `SKIPPED`，返回 0。
- tag 不存在：执行命令。默认阻塞，添加 `--detach` 后后台提交。
- 检查失败或命令失败：输出 `FAILED`，返回非零状态。

同一个 container/tag 或 local/tag 的“检查并启动”由宿主机非阻塞文件锁保护。多个
cron 在同一瞬间触发时，只有一个会继续检查和启动，其余输出
`SKIPPED reason=ensure_in_progress`；阻塞任务运行期间也会持有该锁。

容器模式通过容器内的 `pgrep -f` 查询 tag。no-container 模式使用
`/tmp/.agent_eye/pids/` 中权限为 `0600` 的 PID 状态文件，并检查进程是否存活；
Linux 上还会通过 `/proc/<pid>/cmdline` 防止 PID 重用造成误判。

`--exec` 中的长期程序应保持前台运行，不要自行添加 `nohup` 或 `&`。后台化交给
`--detach`。

## kill

终止宿主机上的任务时只需要 tag：

```bash
eye kill --tag training-01
```

终止容器内任务时增加容器名称：

```bash
eye kill --container worker-0 --tag training-01
```

`kill` 默认发送 `SIGTERM`，并遵循以下 fail-closed 安全流程：

1. 使用与 `ensure` 相同的方式查询 tag。
2. 没有匹配时输出 `SKIPPED reason=tag_not_found`，返回 0。
3. 只有恰好匹配一个 tag 包装进程时才继续；多个匹配返回 4，不发送信号。
4. 只通过 `/proc` 递归读取包装进程及其所有子进程并执行安全审查，不调用 `ps`。
5. 审查失败、UID 不匹配或命中保护名单时返回 4，不发送信号。
6. 审查通过后从子进程到包装进程依次发送 `SIGTERM`。

保护名单当前包括：

```text
init, systemd, launchd, kernel_task, kthreadd,
sshd, cron, crond, dbus-daemon, NetworkManager,
dockerd, containerd, containerd-shim, kubelet,
polkitd, rsyslogd, syslogd,
systemd-journald, systemd-logind, systemd-udevd,
systemd-networkd, systemd-resolved, systemd-timesyncd
```

PID 0 和 PID 1 也始终受到保护。安全审查还会再次确认根进程命令行包含完整 tag，
防止 PID 重用或状态文件异常。保护名单会检查整个任务子进程树，而不只检查带 tag
的外层 Bash。container 和本地模式使用相同的安全规则，但容器 PID 只能在对应
容器中查询和发送信号。`kill` 要求目标环境提供可读的 `/proc`；如果 `/proc`
不存在、权限不足，或者任一已发现进程无法完成审查，命令会返回失败且不发送信号。
因此，本地 `kill` 不支持默认没有 `/proc` 的 macOS，其他命令不受影响。

`kill` 同样受 `--allow` 限制，禁止时间内不会查询 PID 或发送信号。

## 日志

`run` 和 `ensure` 都支持：

```text
--log FILE
```

行为取决于是否 detach：

| 模式 | 日志行为 |
|---|---|
| 阻塞且无 `--log` | 仅实时输出到调用方 |
| 阻塞且有 `--log` | 使用 `tee -a`，实时输出并追加到日志 |
| detach 且有 `--log` | 后台任务直接追加 stdout 和 stderr |
| detach 且无 `--log` | 自动写入 `/tmp/.agent_eye/<hash>.log` |

容器模式中的日志路径属于容器；no-container 模式中的路径属于宿主机。工具会创建
日志的父目录，日志采用追加方式，不会清空已有内容。

每次执行还会输出便于 agent 判断的状态行，例如：

```text
SUCCEEDED mode=local exit=0
STARTED mode=local tag=train pid=1234 log=/tmp/train.log
SKIPPED mode=container:worker-0 tag=train pid=56
FAILED mode=local exit=1
```

阻塞任务的 `SUCCEEDED` 或 `FAILED` 表示最终结果；detach 任务的 `STARTED` 只表示
成功提交，最终输出需要从日志读取。

## 从文件运行

一个 JSON 文件只描述一个任务：

```json
{
  "command": "ensure",
  "no_container": true,
  "tag": "training-01",
  "exec": "python3 /workspace/train.py",
  "detach": true,
  "log": "/tmp/training-01.log"
}
```

执行：

```bash
eye from_file /absolute/path/to/task.json
```

`command` 支持 `npu_status`、`run`、`ensure` 和 `kill`。run/ensure 任务必须通过 `container` 或
`no_container: true` 选择且只选择一种执行目标。`detach` 和 `log` 均为可选字段。

本地 kill 文件省略 `container`：

```json
{
  "command": "kill",
  "tag": "training-01"
}
```

容器 kill 文件增加 `container`：

```json
{
  "command": "kill",
  "container": "worker-0",
  "tag": "training-01"
}
```

## 测试和示例

示例和测试统一放在 `test/` 目录。`eye test` 会先按文件名顺序运行全部 `*.json`，
再运行全部 `test_*.py` 功能、安全和集成测试：

```bash
eye test
```

JSON 任务会：

1. 查询 NPU 状态；没有 `npu-smi` 时显示为跳过。
2. no-container 运行 `echo hello world`。
3. 使用隔离 tag detach 启动 `sleep 5`。
4. 再次 ensure 相同 tag，预期显示为跳过。

完整测试覆盖 `npu_status`、`run`、`ensure`、`kill`、`from_file`、`test`、
`version`、`help`、`--allow`、tag、阻塞与 detach、日志、安装脚本、本地执行和
容器执行。容器测试会验证 run、ensure、重复 tag 查询和安全 kill；Linux 主机还会
实际启动并终止一个隔离的本地测试任务。这些 JSON 文件同时也是 `from_file` 示例。

交互终端中，测试进度始终在同一行刷新，并以绿色、黄色和红色区分最终状态。完成后
只展开失败项和跳过项，成功项仅计入汇总，不逐条显示。例如：

```text
Agent Eye Test  ✓ 38 passed  ○ 3 skipped  ✕ 0 failed

SKIPPED
  ○ task · 00_npu_status.json
    npu-smi is unavailable
```

输出重定向到日志或 cron 时不会产生颜色和单行刷新控制符。设置标准环境变量
`NO_COLOR=1` 可以仅关闭颜色。缺少 NPU、Docker、本地镜像或 `/proc` 等外部能力时，
对应集成项会显示为跳过；其他测试仍会完整运行。

标准库单元测试也在相同目录：

```bash
python3 -m unittest discover -s test -p 'test_*.py' -v
```

Docker 集成测试按以下规则处理 `eye_test_worker`：

- 没有 Docker 命令或 daemon 不可用时跳过。
- 已有运行中的 `eye_test_worker` 时直接复用，绝不删除。
- 不存在时，使用本地 `ubuntu:latest` 创建；仅删除本次创建的容器。
- 本地没有测试镜像时跳过，不自动下载。可用 `AGENT_EYE_TEST_IMAGE` 指定其他本地镜像。

测试镜像或已有容器必须包含 Bash、`pgrep`、`tee` 和 `awk`。
集成测试会在容器可用时验证 run、ensure、重复 tag 跳过及安全 kill；安全性单元
测试还覆盖 tag 不唯一、关键进程、查询失败和 local/container 分流。

## crontab 示例

```cron
AGENT_EYE_HOME=/opt/agent_eye
*/5 * * * * /opt/agent_eye/eye from_file /opt/tasks/train.json >>/tmp/eye-cron.log 2>&1
```

默认阻塞并不妨碍 cron 启动新的调度实例；后续 `ensure` 会在 tag 存在时自动跳过。

## 安全边界

`--exec` 和任务 JSON 可以在宿主机或容器中执行完整 Shell 命令，因此只能使用可信
输入。no-container 命令拥有启动 `eye` 的用户权限。命令默认继承当前环境变量；
cron 的 PATH 通常较少，需要在 crontab 或命令中显式配置。
