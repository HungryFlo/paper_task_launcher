#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$repo_dir"

commit_message="${1:-Update paper-task launcher}"
remote_name="${PAPER_TASK_GIT_REMOTE:-origin}"
branch_name="$(git branch --show-current)"

if [[ -z "$branch_name" ]]; then
  echo "paper-task submit: 当前处于 detached HEAD，请先切换到要提交的分支。" >&2
  exit 1
fi

if ! git remote get-url "$remote_name" >/dev/null 2>&1; then
  echo "paper-task submit: 找不到 Git remote：${remote_name}" >&2
  exit 1
fi

echo "[1/6] 运行测试……"
python3 -m unittest discover -s tests

echo "[2/6] 暂存代码改动……"
git add -A

staged_paths="$(git diff --cached --name-only --diff-filter=ACMR)"
if [[ -n "$staged_paths" ]]; then
  unsafe_paths="$(
    printf '%s\n' "$staged_paths" | awk '
      /(^|\/)\.recording(\/|$)/ ||
      /(^|\/)out(\/|$)/ ||
      /(^|\/)workspace(\/|$)/ ||
      /(^|\/)out_data(\/|$)/ ||
      /(^|\/)claude-config(\/|$)/ ||
      /(^|\/)codex-home(\/|$)/ ||
      /(^|\/)kimi-home(\/|$)/ ||
      /(^|\/)claude-settings\.json$/ ||
      /(^|\/)\.env([^\/]*$)/ ||
      /(^|\/)token_pool\/[^\/]+$/ && $0 !~ /(^|\/)token_pool\/\.gitkeep$/ ||
      /(^|\/)data\/[^\/]+/ && $0 !~ /(^|\/)data\/\.gitkeep$/ ||
      /\.pdf$/ { print }
    '
  )"
  if [[ -n "$unsafe_paths" ]]; then
    echo "paper-task submit: 检测到禁止上传的标注、配置或凭据文件：" >&2
    printf '%s\n' "$unsafe_paths" >&2
    echo "已停止提交。请检查 .gitignore 和暂存区。" >&2
    exit 1
  fi
fi

git diff --cached --check

if git diff --cached --quiet; then
  echo "[3/6] 没有本地代码改动需要提交。"
else
  echo "[3/6] 创建提交……"
  git commit -m "$commit_message"
fi

echo "[4/6] 拉取并衔接远端最新提交……"
git pull --rebase "$remote_name" "$branch_name"

echo "[5/6] 再次确认没有误跟踪本地标注数据……"
tracked_unsafe="$(
  git ls-files | awk '
    /(^|\/)\.recording(\/|$)/ ||
    /(^|\/)out(\/|$)/ ||
    /(^|\/)workspace(\/|$)/ ||
    /(^|\/)out_data(\/|$)/ ||
    /(^|\/)\.env([^\/]*$)/ ||
    /(^|\/)token_pool\/[^\/]+$/ && $0 !~ /(^|\/)token_pool\/\.gitkeep$/ ||
    /(^|\/)data\/[^\/]+/ && $0 !~ /(^|\/)data\/\.gitkeep$/ ||
    /\.pdf$/ { print }
  '
)"
if [[ -n "$tracked_unsafe" ]]; then
  echo "paper-task submit: Git 中存在不应上传的文件，拒绝 push：" >&2
  printf '%s\n' "$tracked_unsafe" >&2
  exit 1
fi

echo "[6/6] 推送到 ${remote_name}/${branch_name}……"
git push "$remote_name" "$branch_name"

echo "完成：代码已推送到 ${remote_name}/${branch_name}"
