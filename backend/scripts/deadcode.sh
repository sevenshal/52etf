#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_BIN="${ROOT_DIR}/../../.venv/bin"
VENV_PYTHON="${VENV_BIN}/python"
CONFIG_FILE="${ROOT_DIR}/scripts/deadcode.config"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

if [ ! -x "${VENV_PYTHON}" ]; then
  echo "未找到虚拟环境: ${VENV_PYTHON}" >&2
  echo "请确认已在 /Users/sevenshal/Dev/github/quant/.venv 建好依赖环境。"
  exit 1
fi

if [ -f "${CONFIG_FILE}" ]; then
  # shellcheck disable=SC1090
  source "${CONFIG_FILE}"
fi

DEADCODE_PROFILE="${DEADCODE_PROFILE:-balanced}"

case "${DEADCODE_PROFILE}" in
  strict)
    VULTURE_EXCLUDE="${VULTURE_EXCLUDE:-tests/**,lab/**}"
    VULTURE_MIN_CONFIDENCE="${VULTURE_MIN_CONFIDENCE:-90}"
    RUFF_SELECT="${RUFF_SELECT:-F401,F841,F811,F821,F823}"
    VULTURE_EXTRA_ARGS="${VULTURE_EXTRA_ARGS:-}"
    RUFF_EXTRA_ARGS="${RUFF_EXTRA_ARGS:-}"
    DEPTRY_EXTRA_ARGS="${DEPTRY_EXTRA_ARGS:-}"
    ;;
  balanced|*)
    if [ "${DEADCODE_PROFILE}" != "balanced" ]; then
      echo "未知 profile: ${DEADCODE_PROFILE}，将回退到 balanced。可选值: balanced / strict"
    fi
    VULTURE_EXCLUDE="${VULTURE_EXCLUDE:-tests/**,lab/**}"
    VULTURE_MIN_CONFIDENCE="${VULTURE_MIN_CONFIDENCE:-95}"
    RUFF_SELECT="${RUFF_SELECT:-F401,F841,F811}"
    VULTURE_EXTRA_ARGS="${VULTURE_EXTRA_ARGS:-}"
    RUFF_EXTRA_ARGS="${RUFF_EXTRA_ARGS:-}"
    DEPTRY_EXTRA_ARGS="${DEPTRY_EXTRA_ARGS:---ignore DEP003}"
    ;;
esac

mkdir -p "${ROOT_DIR}/.artifacts/deadcode"

install_if_missing() {
  local tool="$1"
  if ! command -v "${VENV_BIN}/${tool}" >/dev/null 2>&1; then
    echo "未检测到 ${tool}，先安装开发依赖..."
    "${VENV_PYTHON}" -m pip install -r "${ROOT_DIR}/requirements-dev.txt"
  fi
}

install_if_missing vulture
install_if_missing ruff
install_if_missing deptry

cd "${ROOT_DIR}"

run_check() {
  local name="$1"
  local log_file="$2"
  shift 2

  echo "================= ${name} ================="

  "$@" 2>&1 | tee "${log_file}"
  local status=${PIPESTATUS[0]}

  if [ "${status}" -ne 0 ]; then
    echo "${name} 检测到问题，退出码: ${status}"
  else
    echo "${name} 无新增问题"
  fi
  return "${status}"
}

set +e
run_check "1/3 vulture" "${ROOT_DIR}/.artifacts/deadcode/vulture_${TIMESTAMP}.log" \
  ../../.venv/bin/vulture src \
  --exclude "${VULTURE_EXCLUDE}" \
  --min-confidence "${VULTURE_MIN_CONFIDENCE}" \
  ${VULTURE_EXTRA_ARGS}
VULTURE_STATUS=$?

run_check "2/3 ruff" "${ROOT_DIR}/.artifacts/deadcode/ruff_${TIMESTAMP}.log" \
  ../../.venv/bin/ruff check src \
  --select "${RUFF_SELECT}" \
  ${RUFF_EXTRA_ARGS}
RUFF_STATUS=$?

run_check "3/3 deptry" "${ROOT_DIR}/.artifacts/deadcode/deptry_${TIMESTAMP}.log" \
  ../../.venv/bin/deptry src \
  ${DEPTRY_EXTRA_ARGS}
DEPTRY_STATUS=$?
set -e

OVERALL_STATUS=0
if [ "${VULTURE_STATUS}" -ne 0 ] || [ "${RUFF_STATUS}" -ne 0 ] || [ "${DEPTRY_STATUS}" -ne 0 ]; then
  OVERALL_STATUS=1
fi

if [ "${OVERALL_STATUS}" -ne 0 ]; then
  echo
  echo "检测发现问题："
  [ "${VULTURE_STATUS}" -ne 0 ] && echo "  - vulture"
  [ "${RUFF_STATUS}" -ne 0 ] && echo "  - ruff"
  [ "${DEPTRY_STATUS}" -ne 0 ] && echo "  - deptry"
  echo "日志：${ROOT_DIR}/.artifacts/deadcode/"
fi

echo
echo "扫描完成，结果保存在: ${ROOT_DIR}/.artifacts/deadcode/"

if [ "${OVERALL_STATUS}" -ne 0 ]; then
  exit 1
fi
