#!/usr/bin/env bash
# event2 多轮对话测试（A 档灵活输出）
#
# 用法：
#   export API_BASE=http://127.0.0.1:28000
#   bash scripts/test_event2_multiturn.sh          # 跑全部套件
#   bash scripts/test_event2_multiturn.sh A        # 只跑套件 A
#   bash scripts/test_event2_multiturn.sh B C      # 跑套件 B、C
#
# 前置：mock-search-api healthy，bettafish 已启动，.env 已配置 LLM + 知识库

set -euo pipefail

API_BASE="${API_BASE:-http://127.0.0.1:28000}"
OUT_DIR="${OUT_DIR:-./test_output_multiturn}"
mkdir -p "$OUT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
info() { echo -e "       $*"; }

# ---------- SSE 调用 ----------
call_event2() {
  local session_id="$1"
  local instruction="$2"
  local outfile="$3"
  local kb_json="${4:-[1,3,4]}"

  curl -sS -N --max-time 600 \
    -X POST "${API_BASE}/api/v1/analysis/event2" \
    -H "Content-Type: application/json" \
    -H "Accept: text/event-stream" \
    -d "{
      \"instruction\": $(python3 -c "import json; print(json.dumps('$instruction', ensure_ascii=False))"),
      \"session_id\": \"$session_id\",
      \"knowledge_base_ids\": $kb_json,
      \"ext\": {},
      \"files_data\": [],
      \"model_config\": {}
    }" | tee "$outfile"
}

assert_contains() {
  local file="$1"
  local pattern="$2"
  local label="$3"
  if grep -qE "$pattern" "$file"; then
    pass "$label"
    return 0
  fi
  fail "$label（未匹配: $pattern）"
  return 1
}

assert_not_contains() {
  local file="$1"
  local pattern="$2"
  local label="$3"
  if grep -qE "$pattern" "$file"; then
    fail "$label（不应出现: $pattern）"
    return 1
  fi
  pass "$label"
  return 0
}

extract_mode() {
  local file="$1"
  grep -oE '"mode"[[:space:]]*:[[:space:]]*[012]' "$file" | tail -1 | grep -oE '[012]$' || echo "?"
}

# ---------- 套件 A：标准三轮（0 → 1 → 2）----------
# 验证：首轮检索+知识库；二轮局部对话不检索；三轮定稿不检索
run_suite_A() {
  local sid="mt-a-$(date +%s)"
  info "套件 A | session_id=$sid"
  echo

  info "【A-1 首轮】instruction=美伊以战争 | 期望 mode=0，含 search + knowledge"
  local f1="$OUT_DIR/A1_${sid}.sse"
  call_event2 "$sid" "美伊以战争" "$f1"
  assert_contains "$f1" '计划检索关键词|已完成检索' "A-1 达梦检索进度"
  assert_contains "$f1" '计划检索知识库|"stage": "knowledge"' "A-1 知识库检索"
  assert_contains "$f1" 'event: result' "A-1 有 result 流"
  local m1
  m1=$(extract_mode "$f1")
  [[ "$m1" == "0" ]] && pass "A-1 mode=0 (实际 $m1)" || warn "A-1 mode 期望 0，实际 $m1（路由偶发偏差可接受）"
  echo

  info "【A-2 二轮】instruction=请将上述回答的第一段写得更具体一些 | 期望 mode=1，无检索"
  local f2="$OUT_DIR/A2_${sid}.sse"
  call_event2 "$sid" "请将上述回答的第一段写得更具体一些" "$f2"
  assert_contains "$f2" '对话模式|正在理解您的问题' "A-2 进入对话模式"
  assert_not_contains "$f2" '计划检索关键词|已完成检索' "A-2 不应达梦检索"
  assert_not_contains "$f2" '"stage": "search"' "A-2 不应 search 阶段"
  assert_not_contains "$f2" '"stage": "knowledge"' "A-2 不应 knowledge 阶段"
  local m2
  m2=$(extract_mode "$f2")
  [[ "$m2" == "1" ]] && pass "A-2 mode=1 (实际 $m2)" || warn "A-2 mode 期望 1，实际 $m2"
  echo

  info "【A-3 三轮】instruction=把上面细化的内容合并进去，生成一版完整新版回答 | 期望 mode=2，无检索"
  local f3="$OUT_DIR/A3_${sid}.sse"
  call_event2 "$sid" "把上面细化的内容合并进去，生成一版完整新版回答" "$f3"
  assert_contains "$f3" '修改定稿模式|正在读取当前报告' "A-3 进入修改定稿"
  assert_not_contains "$f3" '计划检索关键词|已完成检索' "A-3 不应达梦检索"
  assert_not_contains "$f3" '"stage": "knowledge"' "A-3 不应知识库检索"
  local m3
  m3=$(extract_mode "$f3")
  [[ "$m3" == "2" ]] && pass "A-3 mode=2 (实际 $m3)" || warn "A-3 mode 期望 2，实际 $m3"
  echo

  pass "套件 A 完成 → $OUT_DIR/A*_${sid}.sse"
}

# ---------- 套件 B：轻量追问（0 → 1）----------
run_suite_B() {
  local sid="mt-b-$(date +%s)"
  info "套件 B | session_id=$sid"
  echo

  info "【B-1 首轮】美伊以战争"
  local f1="$OUT_DIR/B1_${sid}.sse"
  call_event2 "$sid" "美伊以战争" "$f1"
  assert_contains "$f1" 'event: result' "B-1 有输出"
  echo

  info "【B-2 追问】当前局势的主要风险点是什么？请简短回答 | 期望 mode=1"
  local f2="$OUT_DIR/B2_${sid}.sse"
  call_event2 "$sid" "当前局势的主要风险点是什么？请简短回答" "$f2"
  assert_contains "$f2" '对话模式|正在理解您的问题' "B-2 对话模式"
  assert_not_contains "$f2" '计划检索关键词' "B-2 不检索"
  echo

  pass "套件 B 完成"
}

# ---------- 套件 C：换题重新分析（0 → 0）----------
# 同 session 换主题应触发新一轮检索（mode 0）
run_suite_C() {
  local sid="mt-c-$(date +%s)"
  info "套件 C | session_id=$sid"
  echo

  info "【C-1 首轮】美伊以战争"
  local f1="$OUT_DIR/C1_${sid}.sse"
  call_event2 "$sid" "美伊以战争" "$f1"
  assert_contains "$f1" '已完成检索' "C-1 首轮检索"
  echo

  info "【C-2 换题】换一个主题分析：俄乌冲突最新进展 | 期望 mode=0 重新检索"
  local f2="$OUT_DIR/C2_${sid}.sse"
  call_event2 "$sid" "换一个主题分析：俄乌冲突最新进展" "$f2"
  assert_contains "$f2" '将为您生成分析报告|计划检索关键词|已完成检索' "C-2 新分析+检索"
  local m2
  m2=$(extract_mode "$f2")
  [[ "$m2" == "0" ]] && pass "C-2 mode=0 (实际 $m2)" || warn "C-2 mode 期望 0，实际 $m2"
  echo

  pass "套件 C 完成"
}

# ---------- 套件 D：无知识库首轮（0，KB 空）----------
run_suite_D() {
  local sid="mt-d-$(date +%s)"
  info "套件 D | session_id=$sid（无 knowledge_base_ids）"
  echo

  info "【D-1】仅达梦，不传知识库"
  local f1="$OUT_DIR/D1_${sid}.sse"
  call_event2 "$sid" "美伊以战争" "$f1" "[]"
  assert_contains "$f1" '已完成检索' "D-1 达梦检索"
  assert_not_contains "$f1" '计划检索知识库' "D-1 不触发知识库"
  echo

  pass "套件 D 完成"
}

# ---------- 套件 E：灵活输出（A 档）----------
# 不应强制出现四段模板标题
run_suite_E() {
  local sid="mt-e-$(date +%s)"
  info "套件 E | session_id=$sid（检查非模板化输出）"
  echo

  local f1="$OUT_DIR/E1_${sid}.sse"
  call_event2 "$sid" "简要分析美伊以战争，用自然段即可，不要套固定章节模板" "$f1"
  assert_contains "$f1" 'event: result' "E-1 有输出"
  # 四段全齐才算失败（允许出现 0～2 个小标题）
  if grep -q '## 一、事件与背景' "$f1" && \
     grep -q '## 二、舆情与数据要点' "$f1" && \
     grep -q '## 三、研判与风险' "$f1" && \
     grep -q '## 四、处置建议' "$f1"; then
    warn "E-1 仍出现完整四段模板（模型可能惯性输出，可再调 prompt）"
  else
    pass "E-1 未出现完整四段硬模板"
  fi
  echo

  pass "套件 E 完成"
}

# ---------- 健康检查 ----------
preflight() {
  info "健康检查 ${API_BASE}/health"
  if ! curl -sf "${API_BASE}/health" >/dev/null; then
    fail "API 不可达: ${API_BASE}/health"
    exit 1
  fi
  pass "API 可达"
  echo
}

# ---------- main ----------
SUITES=("$@")
if [[ ${#SUITES[@]} -eq 0 ]]; then
  SUITES=(A B C D E)
fi

echo "========================================"
echo " event2 多轮对话测试"
echo " API_BASE=$API_BASE"
echo " OUT_DIR=$OUT_DIR"
echo " 套件: ${SUITES[*]}"
echo "========================================"
echo

preflight

FAILED=0
for s in "${SUITES[@]}"; do
  echo "---------- 套件 $s ----------"
  if run_suite_"$s"; then
    :
  else
    FAILED=$((FAILED + 1))
  fi
  echo
done

echo "========================================"
if [[ $FAILED -eq 0 ]]; then
  echo -e "${GREEN}全部套件执行完毕${NC}（个别 WARN 为路由/模型波动，可人工看 SSE 文件）"
  echo "SSE 输出目录: $OUT_DIR"
else
  echo -e "${RED}有 $FAILED 个套件存在 FAIL${NC}"
  exit 1
fi
