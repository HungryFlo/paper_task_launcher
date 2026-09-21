# Paper Task Launcher

`paper-task` 用于从一个已有 Web 开始，调用 Codex、Claude Code 或 Kimi Code 持续修改 Web，并记录：

- 每轮用户提出的要求。
- 模型的最终回复。
- 初始 Web 和每轮修改后的完整 Web 快照。
- 论文 PDF、模型、backend 和会话信息。

源 Web 只会被复制到新 workspace，不会被直接修改。任务中断后可以恢复原会话。

## 1. Requirements

- Python 3.10 或更高版本。
- Git。
- 可以访问 Boyue 服务的网络环境和 Boyue token。
- 至少安装一种用于修改 Web 的 harness：
  - `--backend codex`：需要本地可执行 `codex` 命令。
  - `--backend claude`：需要本地可执行 `claude` 命令。
  - `--backend kimi`：需要本地可执行 `kimi` 命令。

可以使用以下命令检查本地环境：

```bash
python3 --version
git --version
codex --version
claude --version
kimi --version
```

只需要检查你实际要使用的 harness，不必同时安装三种工具。

## 2. Install

克隆仓库并以可编辑模式安装 `paper-task`：

```bash
git clone https://github.com/HungryFlo/paper_task_launcher.git
cd paper_task_launcher
python3 -m pip install -e .
```

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

`token_pool/` 已被 Git 忽略，不会上传到 GitHub。

## 4. 开始执行

```bash
paper-task \
  --continue \
  --paper ./data/example.pdf \
  --web ../inputweb/example-web \
  --model gpt-5.6-sol \
  --backend codex \
  --workspace ../workspace/example-task \
  --boyue-url http://35.220.164.252:3888
```

使用 Claude Code 时，修改 backend 和模型即可：

```bash
paper-task \
  --continue \
  --paper ./data/example.pdf \
  --web ../inputweb/example-web \
  --model kimi-k3 \
  --backend claude \
  --workspace ../workspace/example-task-claude \
  --boyue-url http://35.220.164.252:3888
```

使用 Kimi Code 时：

```bash
paper-task \
  --continue \
  --paper ./data/example.pdf \
  --web ../inputweb/example-web \
  --model kimi-k3 \
  --backend kimi \
  --workspace ../workspace/example-task-kimi \
  --boyue-url http://35.220.164.252:3888
```

### 参数说明

| 参数 | 含义 |
| --- | --- |
| `--continue` | 从已有 Web 开始修改，不发送初始建站 prompt。 |
| `--paper PATH` | 指定论文 PDF。PDF 会复制到 workspace 的 `paper-source/`。 |
| `--web PATH` | 指定待修改的初始 Web 目录。该目录不会被直接修改。 |
| `--model NAME` | 指定修改 Web 使用的模型名称。 |
| `--backend codex\|claude\|kimi` | 选择使用 Codex、Claude Code 或 Kimi Code 执行修改。 |
| `--workspace PATH` | 指定新任务的工作目录。该目录必须不存在或为空。 |
| `--boyue-url URL` | 指定 Boyue 服务地址。默认为 `http://35.220.164.252:3888`。 |

任务启动后：

1. 程序从 `token_pool/.env.token` 测试并选择可用 token。
2. 初始 Web 被复制到 workspace 并记录为 `baseline`。
3. 每轮交互结束后，自动保存用户输入、模型回复和新的 Web 快照。
4. Codex/Claude/Kimi 的任务级配置和 session 保存在 workspace 的 `.recording/` 中。

## 5. 中断后恢复

如果终端关闭、会话中断或需要继续之前的任务，只需指定原 workspace：

```bash
paper-task resume \
  --workspace ../workspace/example-task
```

### 参数说明

| 参数 | 含义 |
| --- | --- |
| `resume` | 恢复原来的 Codex、Claude Code 或 Kimi Code 会话。 |
| `--workspace PATH` | 指定之前创建的任务 workspace。 |

恢复时会自动读取原任务的 backend、model、Boyue URL 和 session ID，并重新从 `token_pool/.env.token` 测试 token。不需要重新传入模型或 backend。

## 6. 修改完成后导出数据

```bash
paper-task export \
  --workspace ../workspace/example-task
```

### 参数说明

| 参数 | 含义 |
| --- | --- |
| `export` | 导出对话、论文和所有 Web 版本。 |
| `--workspace PATH` | 指定需要导出的任务 workspace。 |

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

## 最简流程

```bash
# 1. 开始
paper-task --continue \
  --paper ./data/example.pdf \
  --web ../inputweb/example-web \
  --model gpt-5.6-sol \
  --backend codex \
  --workspace ../workspace/example-task \
  --boyue-url http://35.220.164.252:3888

# 2. 中断后恢复
paper-task resume --workspace ../workspace/example-task

# 3. 导出
paper-task export --workspace ../workspace/example-task
```
