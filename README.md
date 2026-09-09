# Paper Task Launcher

`paper-task` creates one isolated repository and one new interactive Codex or Claude Code session
per paper-learning website task. It imports the paper's PDF or LaTeX source into an ignored
`paper-source/` directory, asks
the selected coding agent to build a frontend that helps users read, understand, and learn the paper,
and records only user messages, final model responses, and code snapshots for that session. The fixed
task explicitly does not ask the model to implement the paper's algorithm or reproduce its experiments.

It can also start a new recorded modification session from an already generated web workspace with
`--continue`. In that mode, the selected paper is imported into `paper-source/`, the existing web is
captured as the baseline, and no paper-generation prompt is sent to the agent.

## Requirements

- Python 3.10+
- Git
- Codex CLI or Claude Code, already logged in
- `pdftotext` only when the input is a generic (non-arXiv) PDF URL
- Network access for arXiv or PDF URLs

## Install

```bash
git clone https://github.com/HungryFlo/paper_task_launcher.git
cd paper_task_launcher
python3 -m pip install -e .
```

## Quick start

The current Human Bench workflow uses the local PDFs in `data/`. Choose one PDF, create a separate
empty workspace for it, and pass the PDF path to `--paper`:

```bash
# Start paper 4
paper-task \
  --backend codex \
  --workspace ../workspace/4_paper \
  --paper ./data/4.pdf

# Resume the same workspace if the session was interrupted
paper-task resume \
  --workspace ../workspace/4_paper

# Start a new recording from an existing web without a generation prompt
paper-task \
  --continue \
  --backend codex \
  --workspace ../workspace/existing_web \
  --paper ./data/4.pdf

# Export the recorded conversation and per-turn web versions
paper-task export \
  --workspace ../workspace/4_paper \
  --output ../out/4_paper
```

## 中文快速开始

本项目当前的目标是：依次完成 `data/` 目录中的 10 篇 PDF 论文，为每篇论文建立独立的
Codex 会话和 workspace，最后导出用户输入、模型最终回复以及每轮结束后的完整 Web 版本。

### 1. 安装

在本目录下以可编辑模式安装：

```bash
python3 -m pip install -e .
```

### 2. 选择 PDF 并开始阅读

PDF 位于：

```text
data/
├── 4.pdf
├── 10.pdf
├── 15.pdf
├── 26.pdf
├── 52.pdf
├── 85.pdf
├── 100.pdf
├── 128.pdf
├── 184.pdf
└── 202.pdf
```

以 `paper_task_launcher` 为当前目录，选择一篇 PDF，将它的相对路径传给 `--paper`。例如开始阅读
`4.pdf`：

```bash
paper-task \
  --backend codex \
  --workspace ../workspace/4_paper \
  --paper ./data/4.pdf
```

参数含义：

- `--backend codex`：使用 Codex 完成论文阅读网页任务。
- `--paper`：指定本次要阅读的 PDF。
- `--workspace`：指定该论文的独立工作目录；第一次启动时，该目录必须不存在或为空目录。

建议使用 `../workspace/<论文编号>_paper` 的命名方式，例如 `10.pdf` 对应
`../workspace/10_paper`。一个 workspace 只对应一篇论文，不要为另一篇 PDF 重复使用。

### 3. 恢复中断的会话

如果终端关闭、网络中断或主动退出 Codex，使用原 workspace 恢复同一个会话：

```bash
paper-task resume \
  --workspace ../workspace/4_paper
```

恢复时不要再传入 `--paper`；启动器会从 workspace 中读取原会话 ID 和后端。

### 4. 从已有 Web 开始新的修改记录

如果 workspace 中已经有一份生成完成的 Web，但还没有 `paper-task` 的录制数据，可以使用
`--continue` 从它开始一次全新的修改会话：

例如，开始前已有 Web 位于：

```text
../workspace/existing_web/
├── index.html
├── styles.css
├── script.js
└── assets/
```

在 `paper_task_launcher` 目录中运行：

```bash
paper-task \
  --continue \
  --backend codex \
  --workspace ../workspace/existing_web \
  --paper ./data/4.pdf
```

该模式会：

- 要求 `--workspace` 已存在、非空，且包含待修改的 Web。
- 仍然要求使用 `--paper` 指定论文，并将论文导入 workspace 中的 `paper-source/`。
- 将当前 Web 完整记录为 `baseline`。
- 启动新的 Codex 会话，由用户在会话中输入修改要求。
- 不使用该论文生成 `initial-prompt.md`，也不向 Codex 发送论文建站 prompt。
- 按普通模式记录每轮用户输入、模型最终回复和修改后的 Web 快照。

会话结束后，可以像普通任务一样导出：

```bash
paper-task export \
  --workspace ../workspace/existing_web \
  --output ../out/existing_web
```

`--continue` 与 `resume` 的区别：

- `--continue`：对一份尚未录制的已有 Web 开始新会话、新录制。
- `resume`：恢复已经存在 `.recording/manifest.json` 的原会话。

如果 workspace 已有 `.recording/`，程序会拒绝 `--continue` 并提示使用：

```bash
paper-task resume --workspace ../workspace/existing_web
```

### 5. 导出数据

任务完成并退出 Codex 后，导出该论文的对话记录、PDF 和每轮 Web 代码版本：

```bash
paper-task export \
  --workspace ../workspace/4_paper \
  --output ../out/4_paper
```

建议输出目录使用 `../out/<论文编号>_paper`。`--output` 指定的目录必须不存在或为空目录；
导出器不会覆盖已有数据。如果对话记录缺少可恢复的用户输入或必要字段，导出会报错，避免产生表面成功但内容不完整的数据集。

### 6. 完成 10 篇论文

按以下顺序处理：

```text
4, 10, 15, 26, 52, 85, 100, 128, 184, 202
```

对每个编号重复“开始阅读 → 必要时恢复会话 → 导出数据”三个步骤。例如处理
`10.pdf` 时，将上述命令中的 `4.pdf`、`../workspace/4_paper` 和 `../out/4_paper` 分别替换为
`10.pdf`、`../workspace/10_paper` 和 `../out/10_paper`。

启动器在新会话第一轮发送给模型的初始提示词定义在
[`PROMPT_TEMPLATE`](paper_task_launcher/launcher.py#L18-L34)，可以直接查看当前任务要求和项目规则。

如需使用 Claude Code，将后端改为 `claude`，其他目录和导出命令保持不变：

```bash
paper-task \
  --backend claude \
  --workspace ../workspace/4_paper \
  --paper ./data/4.pdf
```

首次进入新工作目录时，请接受 Claude Code 显示的 workspace trust 提示，否则本次会话的
录制 hooks 不会运行。启动器通过 Claude Code 的 `SessionStart`、`UserPromptSubmit` 和
`Stop` hooks 采集会话；这些 hooks 只记录数据，不向 Claude 添加上下文或控制决策。

## Use

The workspace must be new or completely empty and must not be inside another Git worktree.

```bash
paper-task \
  --workspace ../workspace/4_paper \
  --paper ./data/4.pdf
```

To record changes to an existing web without sending the paper-generation prompt, use `--continue`
and still specify the paper to import into `paper-source/`:

```bash
paper-task \
  --continue \
  --backend codex \
  --workspace ../workspace/existing_web \
  --paper ./data/4.pdf
```

The existing web becomes the baseline snapshot. The directory must be non-empty and must not
already contain `.recording`; use `paper-task resume` for an existing recording.

The paper may instead be an arXiv ID, arXiv abstract/PDF URL, or a PDF URL:

```bash
paper-task --workspace ../workspace/arxiv-paper --paper 2401.01234
paper-task --workspace ../workspace/arxiv-paper --paper https://arxiv.org/abs/2401.01234
```

The launcher prints progress while it validates the workspace, identifies the paper, downloads and
extracts the source, initializes Git, launches the selected agent, and finalizes the recording. arXiv requests
retry automatically with a short backoff and fall back between the official HTTP API endpoint and
HTTPS endpoints.

When an arXiv ID or arXiv URL is provided directly, the launcher skips the metadata API and downloads
the LaTeX source immediately, so a metadata API timeout cannot block the task. The metadata API is
used only when a generic PDF URL must first be matched to an arXiv paper.

The defaults can be adjusted for a slow or proxied network:

```bash
PAPER_TASK_NETWORK_TIMEOUT=60 PAPER_TASK_NETWORK_RETRIES=3 \
  paper-task --workspace ../workspace/arxiv-paper --paper 2401.01234
```

For a generic PDF URL, the launcher downloads the first page text, searches arXiv by title, and
continues only when one result is an unambiguous match. It never silently chooses among ambiguous
papers.

Use `--prepare-only` to validate/import/init without opening the selected agent:

```bash
paper-task --workspace ../workspace/prepared-paper --paper ../local-latex-source --prepare-only
```

Resume an interrupted recording with the same backend and session ID:

```bash
paper-task resume --workspace ../workspace/4_paper
```

Resume validates the manifest, transcript turn count, and latest Git snapshot before launching the
agent. It appends new completed turns and preserves any in-progress workspace changes left by an
interruption. A prepared-only task cannot be resumed because it has no agent session ID yet.

## Output

```text
new-task/
  .git/
  .gitignore
  paper-source/                # ignored, never included in snapshots
  .recording/                  # ignored
    initial-prompt.md
    manifest.json
    transcript.jsonl
    snapshots.jsonl
    claude-settings.json        # Claude backend only; launcher-owned hooks
```

Each completed turn is written as one `transcript.jsonl` record. The corresponding code tree is a
Git commit reachable at:

```text
refs/recorder/<recording-id>/turn/0001
refs/recorder/<recording-id>/turn/0002
refs/recorder/<recording-id>/latest
```

The pre-conversation baseline is stored at `refs/recorder/<recording-id>/baseline`.

The recorder uses a private Git index, so it does not switch branches or modify the user's normal
staging area. Inspect a turn with `git show <ref>` or restore it into a separate worktree.

## Export a dataset

After the agent session ends, export all collected data and full code versions into a new or empty
directory:

```bash
paper-task export \
  --workspace ../workspace/4_paper \
  --output ../out/4_paper
```

The result is self-contained and does not require the task repository's `.git` directory:

```text
exported-dataset/
  dataset.json                 # stable schema, paper/session summary, baseline and relative paths
  recording-manifest.json      # original collection metadata and provenance
  initial-prompt.md
  turns.jsonl                  # one record per completed turn, including code_path and commit
  paper-source/                # imported LaTeX source
  versions/
    baseline/                  # code before the conversation
    turn-0001/                 # complete code after turn 1
    turn-0002/                 # complete code after turn 2
```

Each `turns.jsonl` record contains the user input, final model response, timestamps, original Git
snapshot metadata, and a relative `code_path`. The exporter verifies every commit before writing the
dataset and refuses to overwrite a non-empty output directory. Use `--no-paper-source` when the
downstream dataset should contain only conversation metadata and code versions.

Export also refuses to create a dataset when there are no completed turns, when any turn has an
empty/missing `user_input`, or when the `final_response` field is missing or malformed. An explicitly
empty `final_response` is preserved because Codex records `last_agent_message: null` for some
interrupted turns; the exporter does not invent content that was absent from the source session. The
error identifies affected turns so an incompatible recording can be repaired instead of being
mistaken for a complete dataset.

## Data and failure behavior

- Local source repositories are copied without `.git`; symbolic links are rejected.
- arXiv archives are checked for path traversal, links, devices, excessive size, and file count.
- The exact rendered first prompt, paper hashes, arXiv version, backend, session ID, and available
  agent metadata are stored in the manifest.
- If source identification/import fails, the selected agent is not launched.
- A failed preparation removes only launcher-created files and leaves the selected workspace empty,
  so the same directory can be retried.
- When the selected agent exits, recording stops and the manifest is finalized.
