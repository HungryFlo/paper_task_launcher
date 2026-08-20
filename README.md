# Paper Task Launcher

`paper-task` creates one isolated repository and one new interactive Codex session per paper-learning
website task. It imports the paper's LaTeX source into an ignored `paper-source/` directory, asks
Codex to build a frontend that helps users read, understand, and learn the paper, and records only
user messages, final model responses, and code snapshots for that session. The fixed task explicitly
does not ask Codex to implement the paper's algorithm or reproduce its experiments.

## Requirements

- Python 3.10+
- Git
- Codex CLI, already logged in
- `pdftotext` only when the input is a generic (non-arXiv) PDF URL
- Network access for arXiv or PDF URLs

## Install

```bash
git clone https://github.com/HungryFlo/paper_task_launcher.git
cd paper_task_launcher
python3 -m pip install -e .
```

## Quick start

```bash
# 开始阅读
paper-task \
  --workspace /absolute/path/to/new-task \
  --paper /absolute/path/to/local-latex-source

# 导出数据
paper-task export \
  --workspace /absolute/path/to/new-task \
  --output /absolute/path/to/exported-dataset
```

## 中文快速开始

首先克隆并安装启动器：

```bash
git clone https://github.com/HungryFlo/paper_task_launcher.git
cd paper_task_launcher
python3 -m pip install -e .
```

为每篇论文新建一个独立任务目录，然后启动全新的 Codex 论文阅读会话：

```bash
paper-task \
  --workspace /absolute/path/to/new-task \
  --paper /absolute/path/to/local-latex-source
```

`--paper` 也可以接受 arXiv ID、arXiv 链接或 PDF 链接。任务完成并退出 Codex 后，将完整
对话记录、论文源码以及每轮代码版本导出为便于后续处理的数据集：

```bash
paper-task export \
  --workspace /absolute/path/to/new-task \
  --output /absolute/path/to/exported-dataset
```

启动器在新会话第一轮发送给 Codex 的初始提示词定义在
[`PROMPT_TEMPLATE`](paper_task_launcher/launcher.py#L16-L32)，可以直接查看当前任务要求和项目规则。

## Use

The workspace must be new or completely empty and must not be inside another Git worktree.

```bash
paper-task \
  --workspace /absolute/path/to/new-task \
  --paper /absolute/path/to/local-latex-source
```

The paper may instead be an arXiv ID, arXiv abstract/PDF URL, or a PDF URL:

```bash
paper-task --workspace /absolute/path/to/new-task --paper 2401.01234
paper-task --workspace /absolute/path/to/new-task --paper https://arxiv.org/abs/2401.01234
```

The launcher prints progress while it validates the workspace, identifies the paper, downloads and
extracts the source, initializes Git, launches Codex, and finalizes the recording. arXiv requests
retry automatically with a short backoff and fall back between the official HTTP API endpoint and
HTTPS endpoints.

When an arXiv ID or arXiv URL is provided directly, the launcher skips the metadata API and downloads
the LaTeX source immediately, so a metadata API timeout cannot block the task. The metadata API is
used only when a generic PDF URL must first be matched to an arXiv paper.

The defaults can be adjusted for a slow or proxied network:

```bash
PAPER_TASK_NETWORK_TIMEOUT=60 PAPER_TASK_NETWORK_RETRIES=3 \
  paper-task --workspace /absolute/path/to/new-task --paper 2401.01234
```

For a generic PDF URL, the launcher downloads the first page text, searches arXiv by title, and
continues only when one result is an unambiguous match. It never silently chooses among ambiguous
papers.

Use `--prepare-only` to validate/import/init without opening Codex:

```bash
paper-task --workspace /absolute/path/to/new-task --paper /path/to/latex --prepare-only
```

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

After the Codex session ends, export all collected data and full code versions into a new or empty
directory:

```bash
paper-task export \
  --workspace /absolute/path/to/task \
  --output /absolute/path/to/exported-dataset
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

## Data and failure behavior

- Local source repositories are copied without `.git`; symbolic links are rejected.
- arXiv archives are checked for path traversal, links, devices, excessive size, and file count.
- The exact rendered first prompt, paper hashes, arXiv version, session ID, and Codex CLI version are
  stored in the manifest.
- If source identification/import fails, Codex is not launched.
- A failed preparation removes only launcher-created files and leaves the selected workspace empty,
  so the same directory can be retried.
- If Codex exits, recording stops and the manifest is finalized.
