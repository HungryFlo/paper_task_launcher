# Paper Task Launcher

`paper-task` 用于从一个已有 Web 开始，调用 Codex、Claude Code 或 Kimi Code 持续修改 Web，并记录：

- 每轮用户提出的要求。
- 模型的最终回复。
- 初始 Web 和每轮修改后的完整 Web 快照。
- 论文 PDF、模型、backend 和会话信息。

源 Web 只会被复制到新 workspace，不会被直接修改。任务中断后可以恢复原会话。

## 最简使用方法

先下载分配给自己的任务压缩包并完整解压。解压后会得到一个以标注者姓名命名的目录，例如 `刘毅/`。请把这个目录整体放到：

```text
paper_task_launcher/data/<标注者姓名>/
```

不要只复制其中的 PDF 或 Web，也不要修改 `wave1`–`wave4`、`webs` 的目录结构。放置完成后的示例：

```text
paper_task_launcher/
└── data/
    └── 刘毅/
        ├── wave1/
        ├── wave2/
        ├── wave3/
        ├── wave4/
        └── webs/
```

然后只需要指定论文 ID、模型和 backend：

```bash
paper-task --paperID 4 --model claude-opus-4-6 --backend claude
```

程序会自动定位论文和 Web，并把任务保存到 `out/4`。不需要再输入 `--continue`、`--paper`、`--web`、`--workspace` 或 `--boyue-url`。

```bash
# 中断后恢复同一会话
paper-task resume --paperID 4

# 完成后导出修改轨迹
paper-task export --paperID 4
```

`data/` 下的标注者目录和 `out/` 已被 Git 忽略，不会上传到 GitHub。

## 1. Requirements

- Python 3.11 或更高版本。项目使用 Python 3.11 标准库中的 `tomllib`，Python 3.10 及更早版本会报 `ModuleNotFoundError: No module named 'tomllib'`。
- Git。
- 可以访问 Boyue 服务的网络环境和 Boyue token。
- 至少安装一种用于修改 Web 的 harness：
  - `--backend codex`：需要本地可执行 `codex` 命令。
  - `--backend claude`：需要本地可执行 `claude` 命令。
  - `--backend kimi`：需要本地可执行 `kimi` 命令。

在 macOS 或 Linux 上，可按需安装对应的 harness，不必同时安装三种。

### Codex CLI

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

也可以通过 npm 安装：

```bash
npm install -g @openai/codex
```

官方文档：[Codex CLI](https://developers.openai.com/codex/cli/)

### Claude Code

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

官方文档：[Install Claude Code](https://code.claude.com/docs/en/setup#install-claude-code)

### Kimi Code

```bash
curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash
```

官方文档：[Kimi Code CLI Getting Started](https://moonshotai.github.io/kimi-code/en/guides/getting-started)

可以使用以下命令检查本地环境：

```bash
python3 --version
git --version
codex --version
claude --version
kimi --version
```

`python3 --version` 必须输出 `Python 3.11.x` 或更高版本。如果系统中同时安装了多个 Python，请在安装项目时显式使用 Python 3.11：

```bash
python3.11 -m pip install -e .
```

## 2. Install

克隆仓库并以可编辑模式安装 `paper-task`：

```bash
git clone https://github.com/HungryFlo/paper_task_launcher.git
cd paper_task_launcher
python3 -m pip install -e .
```

安装命令会检查 Python 版本；低于 3.11 时会拒绝安装。请先升级 Python，再重新执行安装命令。

安装完成后可以检查命令：

```bash
paper-task --help
```

## 3. 配置 Boyue token

项目目录下需要有以下文件：

```text
token_pool/.env.token
```

文件中每行放一个 Boyue token，格式为 `名称=token`：

```dotenv
token1=**
token2=**
```

支持空行、以 `#` 开头的注释，以及 `export token1=**` 格式。

token 探测会并发执行；第一个验证成功的 token 会立即被使用，其余探测会被终止。token 明文不会写入 workspace、快照或导出数据。

`token_pool/` 目录会保留在 GitHub，但除 `.gitkeep` 外的 token 文件都已被 Git 忽略，不会上传。

## 4. 准备标注目录并开始执行

先下载并完整解压任务文件，再把解压得到的 `<标注者姓名>` 目录整体放到 `paper_task_launcher/data/` 下。最终路径必须是：

```text
paper_task_launcher/data/<标注者姓名>/
```

不要拆分、重命名或移动标注者目录内部的 `wave1`–`wave4` 和 `webs`。完整目录结构应为：

```text
paper_task_launcher/
├── data/
│   └── 刘毅/                     # 标注者姓名
│       ├── wave1/
│       │   └── 4_论文标题.pdf
│       ├── wave2/
│       ├── wave3/
│       ├── wave4/
│       └── webs/
│           └── 4_web/
│               └── index.html
└── out/
```

程序会从 `wave1` 至 `wave4` 中精确查找 `<paperID>_*.pdf`，并查找同一标注者目录下的 `webs/<paperID>_web`。建议每位标注者的本地 `data/` 中只放自己的标注目录；如果同一个 ID 在多个标注者目录中同时出现，程序会拒绝启动并报告歧义。

最简启动命令如下：

```bash
paper-task \
  --paperID 4 \
  --model claude-opus-4-6 \
  --backend claude
```

该命令会自动完成以下解析：

- 论文：`data/<标注者名字>/wave1-wave4/4_*.pdf`
- 初始 Web：`data/<标注者名字>/webs/4_web`
- workspace：`out/4`
- `--continue`：自动启用
- `--boyue-url`：默认使用 `http://35.220.164.252:3888`

启动时和会话结束后，程序都会显示 workspace、恢复命令和导出命令。

使用 Codex 或 Kimi Code 时只需更换 backend 和模型，例如：

```bash
paper-task \
  --paperID 4 \
  --model gpt-5.6-sol \
  --backend codex
```

如需自定义 workspace，仍可显式传入：

```bash
paper-task \
  --paperID 4 \
  --model claude-opus-4-6 \
  --backend claude \
  --workspace /path/to/custom-workspace
```

### 参数说明

| 参数                            | 含义                                                                  |
| ------------------------------- | --------------------------------------------------------------------- |
| `--paperID ID`                | 自动定位论文和初始 Web。也支持别名`--paper-id`。                    |
| `--model NAME`                | 指定修改 Web 使用的模型名称。                                         |
| `--backend codex\|claude\|kimi` | 选择使用 Codex、Claude Code 或 Kimi Code 执行修改。                   |
| `--workspace PATH`            | 可选。默认是项目目录下的`out/<paperID>`；指定路径必须不存在或为空。 |

原来的 `--paper PATH --web PATH --workspace PATH` 路径模式仍然兼容，但日常标注建议只使用 `--paperID`。使用 `--paperID` 时不能再同时传入 `--paper` 或 `--web`。

任务启动后：

1. 程序从 `token_pool/.env.token` 测试并选择可用 token。
2. 初始 Web 被复制到 workspace 并记录为 `baseline`。
3. 每轮交互结束后，自动保存用户输入、模型回复和新的 Web 快照。
4. Codex/Claude/Kimi 的任务级配置和 session 保存在 workspace 的 `.recording/` 中。

### Harness 状态目录

Codex、Claude Code 和 Kimi Code 不会直接在共享 workspace 中运行其状态目录。启动时，`paper-task` 会把对应的任务级状态复制到本机临时目录；harness 退出后，再将完整状态安全回写到 workspace：

| backend     | workspace 中的持久目录        | 运行时环境变量                        |
| ----------- | ----------------------------- | ------------------------------------- |
| Codex       | `.recording/codex-home/`    | `CODEX_HOME`、`CODEX_SQLITE_HOME` |
| Claude Code | `.recording/claude-config/` | `CLAUDE_CONFIG_DIR`                 |
| Kimi Code   | `.recording/kimi-home/`     | `KIMI_CODE_HOME`                    |

默认运行目录位于 `/tmp/paper-task-harness-<uid>/`。如需调整，可以指定一个支持文件锁和可写 `mmap` 的本地目录：

```bash
export PAPER_TASK_RUNTIME_ROOT=/path/to/local/runtime
```

不要把 `PAPER_TASK_RUNTIME_ROOT` 指向 NFS、VirtioFS 或其他不支持 SQLite WAL 共享内存映射的共享目录。workspace 中的目录是持久副本，后续使用 `paper-task resume` 时会自动恢复到本地运行目录。

## 5. 中断后恢复

如果终端关闭、会话中断或需要继续之前的任务，只需指定原 workspace：

```bash
paper-task resume \
  --paperID 4
```

如果启动时使用了自定义 workspace，则使用：

```bash
paper-task resume \
  --workspace /path/to/custom-workspace
```

### 参数说明

| 参数                 | 含义                                                   |
| -------------------- | ------------------------------------------------------ |
| `resume`           | 恢复原来的 Codex、Claude Code 或 Kimi Code 会话。      |
| `--paperID ID`     | 恢复默认保存在`out/<paperID>` 的任务。               |
| `--workspace PATH` | 恢复显式指定的任务 workspace；与`--paperID` 二选一。 |

恢复时会自动读取原任务的 backend、model、Boyue URL 和 session ID，并重新从 `token_pool/.env.token` 测试 token。不需要重新传入模型或 backend。

## 6. 修改完成后导出数据

```bash
paper-task export \
  --paperID 4
```

自定义 workspace 使用：

```bash
paper-task export \
  --workspace /path/to/custom-workspace
```

### 参数说明

| 参数                 | 含义                                                   |
| -------------------- | ------------------------------------------------------ |
| `export`           | 导出对话、论文和所有 Web 版本。                        |
| `--paperID ID`     | 导出默认保存在`out/<paperID>` 的任务。               |
| `--workspace PATH` | 导出显式指定的任务 workspace；与`--paperID` 二选一。 |

数据默认导出到：

```text
<workspace>/out_data/
```

主要内容包括：

```text
out_data/
├── dataset.json
├── recording-manifest.json
├── turns.jsonl
├── paper-source/
└── versions/
    ├── baseline/
    ├── turn-0001/
    ├── turn-0002/
    └── ...
```

- `baseline/`：复制进 workspace 时的初始 Web。
- `turn-XXXX/`：每轮交互结束后的 Web。
- `turns.jsonl`：每轮的用户输入、模型回复、模型名称和对应快照。

`out_data/` 必须不存在或为空目录。导出器不会覆盖已有数据，也不会将 `out_data/` 再次记录到 Web 快照中。
