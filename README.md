# datameter 简体中文版

基于 [vnstat](https://humdi.net/vnstat/) 的终端流量监控面板，适合有流量限额的移动网络、按量计费网络等场景。首次启动时，按向导选择网卡并填写套餐额度和提醒阈值，即可查看用量与趋势。

本仓库 fork 自 [diforenzad-afk/datameter](https://github.com/diforenzad-afk/datameter)，默认界面为简体中文。已汉化主面板、首次设置向导、设置、帮助、用量摘要、提醒和程序自身的提示；同时修复中文双列字符的对齐与截断。命令、快捷键、配置字段和数据格式保持兼容。

[上游英文说明](README.en.md) · [MIT 许可证](LICENSE)

## 预览

```text
  datameter                                      时间：12:30
  vnstat ● 运行中/开机启动                        [d] 停止服务

  周期   2026-11-01 → 2026-12-01   第 13/30 天
  ██ ██ ██ ██ ██ ░░ ░░ ░░ ░░ ░░
  125.0 / 300 GB    41.7%    预计 288.5 GB   ● 预算内

  今日
  ██ ██ ██ ██ ██ ██ ░░ ░░ ░░
  6.25 / 10 GB    62.5%

  平均用量                               用量提醒
  每在线小时   512.0 MiB （共 24 小时）   每日 10 GB  阈值 80,100%
  每日           9.6 GiB （近 7 天）      周期 300 GB  阈值 80,100%
  每周          67.3 GiB                 声音：开   通知：开
  周期预计     288.5 GiB （本周期）       按 s 修改限额
```

以上为中文界面示意，数据仅作演示。上游截图保留在 `assets/screenshot.png`。完整界面还包含最近 7 天和 24 小时的柱状图，是否显示取决于终端高度。

## 功能

- **周期用量**：显示当前计费周期的已用流量、额度、预计总用量及预算进度；支持自定义每月套餐重置日。
- **今日用量**：显示当日流量及每日限额。
- **平均用量**：统计每在线小时、每日、每周平均用量和周期预计用量。
- **用量图表**：展示最近 7 天及过去 24 个自然小时的流量；超过每日限额的日期标红，未记录的小时显示为零。
- **用量提醒**：用量达到限额的指定百分比时，播放短促提示音并发送桌面通知；各阈值在每日和每周期内分别只触发一次。
- **服务控制**：在界面中启动或停止 vnstat 服务。

## 环境要求

- Linux（不支持直接在 Windows 上运行）。
- Python 3.8 或更新版本，无第三方运行时 Python 依赖，使用标准库 curses。
- vnstat 2.x，且 vnstat 后台服务正在运行。
- 支持中文的 UTF-8 终端及中文等宽字体。建议至少使用 **110 列 × 40 行**，以显示完整内容；较小窗口会截断超出部分。

以下工具为可选项，存在时自动使用：

- `pw-play`、`paplay`、`ffplay` 或 `aplay`：播放提示音。
- `notify-send`：发送桌面通知。
- `systemctl`：控制 vnstat 服务。

请先通过发行版包管理器安装 vnstat，并确认要监控的网卡已经由 vnstat 记录。若中文显示乱码，请检查 `locale` 输出，使用系统已安装的 UTF-8 locale，并选择支持中文的终端字体。

## 安装

```bash
git clone https://github.com/justamokou/datameter.git
cd datameter
mkdir -p ~/.local/bin
ln -sf "$PWD/datameter" ~/.local/bin/datameter
```

确保 `~/.local/bin` 已加入 `PATH`，然后运行 `datameter`。

## 首次运行

首次启动会进入设置向导，依次设置：

1. 网卡（默认选择累计流量最多的网卡）。
2. 每月套餐重置日（1–28）。
3. 每个计费周期的套餐额度（GB）。
4. 每日流量预算（GB）。
5. 提醒阈值，使用英文逗号分隔的百分比，例如 `80,100`。
6. 是否播放提示音。
7. 是否发送桌面通知。

按 `Enter` 接受显示的值，按 `Esc` 跳过向导；确认页按 `Enter` 保存到 `config.json`。按 `w` 可随时重新运行向导，按 `s` 可进入设置。

## 命令行用法

```bash
datameter                    # 启动终端面板
datameter --once             # 输出中文用量摘要后退出
datameter --check            # 检查提醒阈值、发送通知后退出
datameter --interface eth0   # 临时指定要监控的网卡
datameter --generate-sound   # 生成或重新生成提示音文件
datameter --help             # 显示中文帮助
```

`--once` 适合状态栏和脚本，输出标签已改为中文。`--check` 适合定时任务，是关闭面板后仍可触发提醒的运行方式，参见 `examples/`。

## 快捷键

| 按键 | 操作 |
|------|------|
| `s` | 打开设置 |
| `w` | 重新运行首次设置向导 |
| `d` | 启动或停止 vnstat 服务，页眉和页脚会提示当前操作 |
| `t` | 试听提示音 |
| `r` | 立即刷新 |
| `?` | 显示帮助 |
| `q` | 退出 |

设置页中：`↑`/`↓`（或 `k`/`j`）选择选项，`←`/`→`（或 `+`/`-`）修改值，`Enter` 编辑网卡或阈值字段；其他字段按 `Enter` 增加数值或切换开关。`t` 试听，`s` 保存并返回，`Esc` 取消当前文本编辑或返回面板。数值及开关修改会立即保存，已提交的文本也会保存，`Esc` 不会撤销这些更改。

## 用量提醒

按 `s` 打开设置：

1. 在「流量套餐」中设置「周期限额 (GB)」「每日限额 (GB)」「周期起始日」。
2. 在「用量提醒」中设置「提醒阈值 (%)」，例如 `80,100`。
3. 在「声音与通知」中设置提示音、桌面通知和音量。

例如，每日限额为 10 GB、周期限额为 300 GB、阈值为 `80,100` 时，当日用量达到 8 GB、10 GB，周期用量达到 240 GB、300 GB 时会分别提醒。

触发记录保存在 `.alert-state.json`，在新的一天或新周期开始时重置，避免重复提醒。面板运行时会检查提醒；面板关闭后，可通过用户级 systemd 定时器运行 `--check`：

```bash
mkdir -p ~/.config/systemd/user
cp examples/datameter-check.service ~/.config/systemd/user/
cp examples/datameter-check.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now datameter-check.timer
```

示例定时器在用户服务管理器启动 2 分钟后首次执行，此后每 10 分钟执行一次。能否在退出登录后继续运行取决于系统的用户服务配置。

## 配置

首次运行会在 `datameter.py` 同目录创建 `config.json`。可以在设置页修改参数；首次设置完成标记 `setup_done` 由向导维护。使用环境变量 `DATAMETER_CONFIG` 可自定义配置文件路径，`DATAMETER_STATE` 可自定义提醒记录路径。指定路径的父目录需要已存在且可写。

| 字段 | 含义 |
|------|------|
| `interface` | vnstat 网卡名称，例如 `wlp4s0` |
| `cycle_start_day` | 每月套餐重置日，1–28 |
| `cycle_limit_gb` | 每周期流量额度 |
| `daily_limit_gb` | 每日流量预算 |
| `alert_thresholds_pct` | 百分比列表，例如 `[80, 100]` |
| `sound_enabled` | 是否播放提示音 |
| `sound_volume` | 音量，0.0–1.0 |
| `notifications` | 是否发送桌面通知 |
| `refresh_seconds` | 自动刷新间隔，单位秒 |
| `setup_done` | 是否已完成或跳过设置向导 |

配置字段保持英文，与上游兼容，不需要迁移已有配置。

## 说明与限制

- 本项目沿用上游计算方式：界面中的 GB 限额实际按 `1024³` 字节换算；自动缩放的用量单位使用 KiB、MiB、GiB。
- 周期用量仅汇总 vnstat 数据库中保留的每日记录。如果数据库未覆盖完整计费周期，周期总量和预测值都基于不完整的数据；每日记录的保留范围取决于 vnstat 配置。
- 「每在线小时」仅对数据库中的小时记录求平均，近似表示 vnstat 服务运行期间的平均小时用量。
- 服务开关执行 `systemctl start|stop vnstat`。若系统没有运行 polkit 认证代理，可检查并按需安装 `contrib/` 中的规则：`contrib/install-polkit-rule.sh`。该规则允许 `wheel` 组用户免密控制 vnstat 服务，安装需要 sudo 权限。
- 提示音在运行时生成为 `tock.wav`，单次约 55 毫秒，每次提醒播放三次。
- 外部程序返回的原始错误和少见的 systemd 状态保留原文，以便排错。

## 开发与验证

```bash
python3 -m unittest discover -s tests -v
python3 preview.py                 # 主面板
python3 preview.py 's'             # 设置
python3 preview.py '?'             # 帮助
python3 preview.py 's down right'  # 操作设置
python3 preview.py '' 110 40       # 指定列数和行数
```

`preview.py` 在伪终端中运行程序，将 ANSI 输出回放为纯文本；若安装了 `pyte`，会优先使用它，否则使用内置回放器。预览需要 Linux 和可用的 vnstat 数据；首次运行时显示设置向导，可先完成或跳过向导。自动化测试使用模拟 vnstat 数据，不读取真实流量，也不控制系统服务。GitHub Actions 在 Linux 上验证单元测试与真实 curses 伪终端交互。

## 上游声明

上游作者声明：原始仓库由 `deepseek-v4.1-flash` 模型通过 `pi` 编程代理生成，未经人工代码审查。本 fork 的中文翻译和适配同样由 AI 辅助完成。请在依赖本工具前自行审阅代码；软件按原样提供，不附带担保。

## 许可证

MIT，保留上游版权声明，详见 [LICENSE](LICENSE)。
