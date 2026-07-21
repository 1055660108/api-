#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -f .env ]; then
  echo "验收失败：缺少 .env，请从 .env.acceptance.example 创建独立验收配置" >&2
  exit 2
fi

export DOLA_TEST_ENV="${DOLA_TEST_ENV:-test}"
export DOLA_ACCEPTANCE_ENV="${DOLA_ACCEPTANCE_ENV:-acceptance}"

exec python3 -m scripts.acceptance_linux "$@"
