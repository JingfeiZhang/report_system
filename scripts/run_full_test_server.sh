#!/usr/bin/env bash
# server 上一键全流程测试：健康检查 → 提交任务 → 轮询 → 下载 HTML/PDF
#
# 用法:
#   chmod +x run_full_test_server.sh
#   ./run_full_test_server.sh
#   ./run_full_test_server.sh "美伊以战争"
#   API_BASE=http://127.0.0.1:28000 POLL_INTERVAL=60 ./run_full_test_server.sh
#
# 说明:
#   - 完整分析通常 30～60+ 分钟，默认最多等 3600 秒（与 .env ANALYSIS_ENGINE_MAX_WAIT_SECONDS 一致）
#   - 进度可能 22%→15%→51% 回跳，属正常（Insight 最慢）
#   - 另开终端可看 SSE: curl -N http://127.0.0.1:28000/api/v1/analysis/stream/TASK_ID

set -euo pipefail

API_BASE="${API_BASE:-http://127.0.0.1:28000}"
MOCK_URL="${MOCK_URL:-http://127.0.0.1:8787}"
QUERY="${1:-美伊以战争}"
EVENT_ID="${EVENT_ID:-full_test_$(date +%s)}"
POLL_INTERVAL="${POLL_INTERVAL:-30}"
MAX_WAIT="${MAX_WAIT:-3600}"
OUT_DIR="${OUT_DIR:-${HOME}/tfsj/deploy/test_runs/$(date +%Y%m%d_%H%M%S)}"

mkdir -p "$OUT_DIR"

echo "============================================================"
echo "BettaFish 全流程测试"
echo "  API:     $API_BASE"
echo "  Query:   $QUERY"
echo "  Event:   $EVENT_ID"
echo "  Output:  $OUT_DIR"
echo "============================================================"

preflight() {
  echo ""
  echo "[0] 前置检查"
  if ! curl -sf "$API_BASE/health" >/dev/null; then
    echo "[X] FastAPI 不可达: $API_BASE/health"
    exit 1
  fi
  if ! curl -sf "$MOCK_URL/health" >/dev/null; then
    echo "[X] mock-search-api 不可达: $MOCK_URL/health"
    exit 1
  fi
  echo "  [OK] FastAPI + mock 可达"
  curl -sS "$MOCK_URL/stats" | python3 -c "
import sys, json
d = json.load(sys.stdin)
total = d.get('data', {}).get('database_stats', {}).get('total', 0)
print(f'  [OK] mock 库内合计约 {total} 条')
if total <= 0:
    raise SystemExit('[X] 达梦无数据，请先 import_txt_to_dm.py')
" || exit 1
}

preflight

echo ""
echo "[1] health"
curl -sS "$API_BASE/health" | python3 -m json.tool | tee "$OUT_DIR/01_health.json"

echo ""
echo "[2] submit"
SUBMIT_JSON="$OUT_DIR/02_submit.json"
curl -sS -X POST "$API_BASE/api/v1/analysis/event" \
  -H 'Content-Type: application/json' \
  -d "{\"event_id\":\"${EVENT_ID}\",\"event_type\":\"analysis_request\",\"topic\":\"${QUERY}\",\"query\":\"${QUERY}\"}" \
  | python3 -m json.tool | tee "$SUBMIT_JSON"

TASK_ID="$(python3 -c "import json; print(json.load(open('${SUBMIT_JSON}')).get('task_id',''))")"
if [[ -z "$TASK_ID" ]]; then
  echo "[X] 未拿到 task_id"
  exit 1
fi
echo "$TASK_ID" > "$OUT_DIR/task_id.txt"
echo "  task_id: $TASK_ID"
echo "  SSE: curl -N ${API_BASE}/api/v1/analysis/stream/${TASK_ID}"

echo ""
echo "[3] poll (每 ${POLL_INTERVAL}s，最多 ${MAX_WAIT}s)"
START_TS=$(date +%s)
LAST_PROGRESS=-1

while true; do
  NOW=$(date +%s)
  ELAPSED=$((NOW - START_TS))
  if (( ELAPSED > MAX_WAIT )); then
    echo ""
    echo "[X] 轮询超时 (${MAX_WAIT}s)"
    curl -sS "$API_BASE/api/v1/analysis/status/${TASK_ID}" | python3 -m json.tool > "$OUT_DIR/03_final_timeout.json"
    exit 1
  fi

  STATUS_JSON="$OUT_DIR/03_status_latest.json"
  curl -sS "$API_BASE/api/v1/analysis/status/${TASK_ID}" > "$STATUS_JSON"

  read -r STATUS PROGRESS PDF_READY <<< "$(python3 -c "
import json
d=json.load(open('${STATUS_JSON}'))
print(d.get('status',''), d.get('progress',0), d.get('pdf_ready', False))
")"

  if [[ "$PROGRESS" != "$LAST_PROGRESS" ]] || [[ "$((ELAPSED % 120))" -lt "$POLL_INTERVAL" ]]; then
    printf "%s  #%03ds  status=%-10s  progress=%3s%%  pdf_ready=%s\n" \
      "$(date '+%H:%M:%S')" "$ELAPSED" "$STATUS" "$PROGRESS" "$PDF_READY"
    LAST_PROGRESS="$PROGRESS"
  fi

  case "$STATUS" in
    completed|error|cancelled)
      cp "$STATUS_JSON" "$OUT_DIR/04_final_status.json"
      echo ""
      echo "[4] final status"
      python3 -m json.tool "$OUT_DIR/04_final_status.json"
      break
      ;;
  esac
  sleep "$POLL_INTERVAL"
done

echo ""
echo "[5] download artifacts"
if [[ "$STATUS" == "completed" ]]; then
  if curl -sf "$API_BASE/api/v1/analysis/report/${TASK_ID}" -o "$OUT_DIR/report.html"; then
    echo "  [OK] HTML -> $OUT_DIR/report.html ($(wc -c < "$OUT_DIR/report.html") bytes)"
  else
    echo "  [!] HTML 下载失败"
  fi
  if [[ "$PDF_READY" == "True" ]] || [[ "$PDF_READY" == "true" ]]; then
    if curl -sfL "$API_BASE/api/v1/analysis/report/${TASK_ID}/pdf" -o "$OUT_DIR/report.pdf"; then
      echo "  [OK] PDF  -> $OUT_DIR/report.pdf ($(wc -c < "$OUT_DIR/report.pdf") bytes)"
    else
      echo "  [!] PDF 下载失败"
    fi
  else
    echo "  [!] pdf_ready=false，跳过 PDF（见 final status 的 pdf_error / error_reason）"
  fi
else
  echo "  [!] 任务未 completed，跳过报告下载"
  exit 1
fi

echo ""
echo "[OK] 全流程测试结束"
echo "  产物目录: $OUT_DIR"
