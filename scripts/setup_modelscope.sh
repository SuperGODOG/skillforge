#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

echo "==> 安装 modelscope..."
if [ -f "./.venv/bin/pip" ]; then
    ./.venv/bin/pip install --index-url https://mirrors.aliyun.com/pypi/simple/ modelscope
elif command -v uv >/dev/null 2>&1; then
    uv pip install modelscope --python .venv/bin/python
else
    ./.venv/bin/python -m pip install --index-url https://mirrors.aliyun.com/pypi/simple/ modelscope
fi

echo "==> 下载 bge-small-zh-v1.5 模型至 ./models..."
./.venv/bin/python -c "from modelscope import snapshot_download; snapshot_download('AI-ModelScope/bge-small-zh-v1.5', cache_dir='./models')"

echo "==> 模型准备完毕：./models/models/AI-ModelScope--bge-small-zh-v1.5/snapshots/master"
