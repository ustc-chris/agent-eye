# Agent Eye

Agent Eye 是一个零第三方 Python 依赖的轻量命令行工具。它通过统一的 `eye` 入口
查询 NPU 状态、在宿主机或 Docker 容器中运行命令，并按 tag 避免重复启动任务。

目标机器需要 Python 3。容器模式还需要 Docker CLI，以及容器内的 Bash、`pgrep`、
`tee` 和 `awk`；安全 kill 还需要可读取的 `/proc`。

## 给新 Agent 的快速入口

先运行下面任一命令读取完整说明。源码目录内一定可以使用 `eye doc`；执行过
`./install.sh` 后，也可以使用系统风格的 `man eye`：

```bash
eye doc
man eye
```

根据任务目的选择命令：

| 目的 | 应使用的命令 |
|---|---|
| 查看 NPU | `eye npu_status` |
| 无条件执行一次 | `eye run` |
| 同一任务不存在时才启动 | `eye ensure` |
| 安全终止唯一 tag 对应的任务 | `eye kill` |
| 执行 JSON 中描述的任务 | `eye from_file` |

推荐 agent 对长任务使用 `ensure + --tag + --detach + --log`。这能避免重复启动，
立即返回，并留下可持续检查的日志：

```bash
eye ensure \
  --tag my-unique-task \
  --detach \
  --log /tmp/my-unique-task.log \
  --exec "python3 /absolute/path/to/task.py"
```

不写容器选项时默认在宿主机执行；增加 `--container CONTAINER_NAME` 即可在容器内
执行，此时 `--exec` 中的路径和 `--log` 路径都是容器内路径。不要在 `--exec` 中
添加 `nohup` 或 `&`，后台化统一交给 `--detach`。

读取命令最终输出的状态行即可决定下一步（阻塞任务自身的 stdout 可能出现在状态行之前）：

| 状态 | 含义 | Agent 下一步 |
|---|---|---|
| `SUCCEEDED` | 阻塞任务已成功完成 | 使用结果继续工作 |
| `FAILED` | 启动、检查或任务执行失败 | 读取错误和日志，停止假设任务成功 |
| `STARTED` | detach 任务已成功提交 | 记录 log，稍后检查任务最终输出 |
| `SKIPPED mode=... tag=... pid=...` | 同 tag 任务已运行 | 不要重复启动，检查现有任务日志 |
| `SKIPPED reason=outside_allow` | 当前不在允许时段 | 保留任务，等待允许时段再调用 |
| `KILLED` | 目标任务已通过安全审查并终止 | 按需确认日志或重新启动 |

最重要的边界：`STARTED` 不代表任务最终成功；`--exec` 会执行完整 Shell 命令，只能
传入可信内容；tag 应为每个逻辑任务稳定且唯一的字面量；不确定是否已有任务时优先
用 `ensure`，不要先 `kill` 再 `run`。

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

安装脚本不会复制源码，只会创建命令和 man page 的符号链接：

```text
$HOME/.local/bin/eye -> $AGENT_EYE_HOME/eye
$HOME/.local/share/man/man1/eye.1 -> $AGENT_EYE_HOME/docs/eye.1
```

可以显式指定源码、命令链接目录和 man page 链接目录：

```bash
AGENT_EYE_HOME=/opt/agent_eye \
AGENT_EYE_BIN_DIR=/opt/bin \
AGENT_EYE_MAN_DIR=/opt/share/man/man1 \
./install.sh
```

自定义 `AGENT_EYE_MAN_DIR` 时，它的上级 man 根目录必须位于 `MANPATH`，例如上例
还需设置 `export MANPATH="/opt/share/man:${MANPATH:-}"`。默认目录会被常见 Linux 和
macOS 的 `man` 自动发现；如本机未发现，可设置
`export MANPATH="$HOME/.local/share/man:${MANPATH:-}"`。

SSH 或 cron 中建议显式设置：

```bash
export AGENT_EYE_HOME=/opt/agent_eye
export PATH="$HOME/.local/bin:$PATH"
```

## 命令概览

```text
eye [--allow WINDOW] npu_status
eye [--allow WINDOW] run    [--container NAME] --exec COMMAND [OPTIONS]
eye [--allow WINDOW] ensure [--container NAME] --tag TAG --exec COMMAND [OPTIONS]
eye [--allow WINDOW] kill [--container NAME] --tag TAG
eye [--allow WINDOW] from_file FILE
eye test
eye version
eye doc
eye help [COMMAND]
```

同时支持 `eye --version`、`eye --help` 和各子命令的 `--help`。
help 和 version 在交互终端中使用配色；重定向输出、cron、非 TTY 环境或设置
`NO_COLOR=1` 时自动输出无 ANSI 控制符的纯文本。

## 允许时间

`--allow` 是 `npu_status`、`run`、`ensure`、`kill` 和 `from_file` 的最高优先级执行闸门。
检查发生在 NPU 查询、任务文件读取、pgrep、Docker 查询和命令执行之前：

```bash
eye --allow "mon:1500-2100,2200-2359;tue:0000-2359" npu_status
```

也可以把选项写在受限子命令之后：

```bash
eye run \
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

表达式错误返回 2。`test`、`doc`、`help` 和 `version` 不受允许时间限制，即使全局传入
`--allow` 也不会检查它。

## NPU 状态

```bash
eye npu_status
```

该命令执行 `npu-smi info`，解析设备表和进程表，并为每张 NPU 输出一行固定四列的
CSV 风格状态：

```text
NPU_ID,STATUS,PROCESS_TYPE,OWNER_ID
0,FREE,null,null
1,PROCESSING,VLLM,z50064016
2,PROCESSING,VLLM,null
```

实际输出不包含标题行。`FREE` 表示该卡没有进程，后两列固定为 `null`；
`PROCESSING` 表示至少有一个进程。进程名包含 `VLLM`（大小写不敏感）时，类型统一
输出为 `VLLM`；无法识别时输出 `UNKNOWN`，同卡存在多种类型时输出 `MIXED`。

对于每个 NPU 进程，工具从 `npu-smi` 给出的宿主机 PID 开始，使用 `ps` 获取完整
`ppid` 和 `command`，沿父进程链向上查询到 `ppid=0`。每一级命令行都会匹配
`/([A-Za-z]\d{8})/`：例如 `/z50064016/` 记录为 `z50064016`。进程已退出、查询失败、
父链循环、超过安全深度或全链未匹配时，OWNER_ID 输出 `null`。同卡多个进程解析出
不同 OWNER_ID 时也输出 `null`，避免错误归属。

## 多机 NPU 终端大屏

仓库根目录的 `npu_dashboard.py` 可以通过 SSH 并行调用多台服务器上的
`eye npu_status`，并持续刷新终端大屏。先编辑脚本顶部配置区：

```python
QUERY_REFRESH_SECONDS = 20.0
TERMINAL_REFRESH_SECONDS = 0.5
DISPLAY_COLUMNS = 1
MACHINES = [
    {"name": "server-01", "ip": "root@192.168.1.10", "alias": "推理节点 A"},
    {"name": "server-02", "ip": "root@192.168.1.11", "alias": "推理节点 B"},
]
```

然后运行：

```bash
python3 npu_dashboard.py
```

`ip` 支持 IP、主机名或 `user@host`。运行前需要保证本机可以通过 SSH 密钥无交互
登录目标服务器，目标服务器的 PATH 中可以找到 `eye`，并且 known_hosts 已配置。
查询按机器并行执行；单台机器超时或失败只会在对应卡片内显示错误。大屏每 0.5 秒
更新倒计时，每 20 秒重新查询，并在终端宽度变化时自动调整实际列数。

每台机器的 header 显示 `name (alias)     free: 可用卡数/总卡数`。表格边框和 header
按可用卡数量着色：0 张为红色、1～3 张为黄色、4 张及以上为绿色；NPU 数据内容单独
着色，空闲卡为蓝色、占用卡为红色。所有横向边框均使用 `#`。

`DISPLAY_COLUMNS` 是期望的最大列数，默认 `1`；终端放不下时会自动减少。按
`Ctrl-C` 退出，大屏会恢复终端光标。设置 `NO_COLOR=1` 可以关闭颜色。

## run

在宿主机运行：

```bash
eye run --exec "echo hello world"
```

在容器中运行：

```bash
eye run --container worker-0 --exec "python3 /workspace/task.py"
```

默认阻塞等待，实时输出命令内容，并返回命令的真实退出状态。只有显式增加
`--detach` 才会后台提交：

```bash
eye run \
  --detach \
  --exec "sleep 10; echo finished" \
  --log /tmp/task.log
```

`run` 可以使用可选 tag，但不会因为同名 tag 已存在而跳过：

```bash
eye run --tag task-01 --exec "sleep 10"
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

容器模式通过容器内的 `pgrep -f` 查询 tag。本地模式使用
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

容器模式中的日志路径属于容器；本地模式中的路径属于宿主机。工具会创建
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

`command` 支持 `npu_status`、`run`、`ensure` 和 `kill`。run/ensure 任务省略 `container`
时默认在宿主机运行，提供 `container` 时进入对应容器。`detach` 和 `log` 均为
可选字段。

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
2. 在宿主机运行 `echo hello world`。
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
输入。本地命令拥有启动 `eye` 的用户权限。命令默认继承当前环境变量；
cron 的 PATH 通常较少，需要在 crontab 或命令中显式配置。
