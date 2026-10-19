#!/usr/bin/env python3
"""
Harness Step Executor — phase 내 step을 순차 실행하고 자가 교정한다.
모든 step이 끝나면 교차 리뷰(scripts/review.py)로 검증하고 docs/PROGRESS.md에 기록한다.

Usage:
    python3 scripts/execute.py <phase-dir> [--push] [--no-review]
"""

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import types
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import config as cfg
import review

ROOT = Path(__file__).resolve().parent.parent
SELECTION_MAX_AGE_DAYS = 7
LIMIT_PATTERN = re.compile(r"usage limit|limit reached|rate.?limit|\b429\b", re.IGNORECASE)


def is_usage_limited(output: dict) -> bool:
    """worker가 사용량 한도로 멈췄는지. 성공한 응답 본문에 같은 단어가 있는 것은 무시한다."""
    text = output.get("stdout") or ""
    try:
        failed = output["exitCode"] != 0 or bool(json.loads(text).get("is_error"))
    except (ValueError, AttributeError):
        failed = output["exitCode"] != 0
    return failed and bool(LIMIT_PATTERN.search(text + (output.get("stderr") or "")))


def _today() -> date:
    return date.today()


@contextlib.contextmanager
def progress_indicator(label: str):
    """터미널 진행 표시기. with 문으로 사용하며 .elapsed 로 경과 시간을 읽는다."""
    frames = "◐◓◑◒"
    stop = threading.Event()
    t0 = time.monotonic()

    def _animate():
        idx = 0
        while not stop.wait(0.12):
            sec = int(time.monotonic() - t0)
            sys.stderr.write(f"\r{frames[idx % len(frames)]} {label} [{sec}s]")
            sys.stderr.flush()
            idx += 1
        sys.stderr.write("\r" + " " * (len(label) + 20) + "\r")
        sys.stderr.flush()

    th = threading.Thread(target=_animate, daemon=True)
    th.start()
    info = types.SimpleNamespace(elapsed=0.0)
    try:
        yield info
    finally:
        stop.set()
        th.join()
        info.elapsed = time.monotonic() - t0


class StepExecutor:
    """Phase 디렉토리 안의 step들을 순차 실행하는 하네스."""

    MAX_RETRIES = 3
    # 첫 리뷰 뒤 '수정 → 검증 리뷰(수정분만)'를 최대 몇 번 돌지. 넘으면 blocked로 두고 사람이 판단한다.
    MAX_REVIEW_FIX_ROUNDS = review.MAX_VERIFY_ROUNDS
    FEAT_MSG = "feat({phase}): step {num} — {name}"
    CHORE_MSG = "chore({phase}): step {num} output"
    FIX_MSG = "fix({phase}): address review findings"
    PROGRESS_REL = "docs/PROGRESS.md"
    PUSH_BLOCKED_RISK_LEVELS = ("high", "critical")
    TIMEOUT_EXIT_CODE = -1
    # 사용량 한도: 5분부터 두 배씩 최대 30분 간격으로, 합계 timeouts_sec.limit_wait(기본 6시간)까지 기다린다.
    LIMIT_WAIT_FIRST_SEC = 300
    LIMIT_WAIT_MAX_SEC = 1800
    LIMIT_WAIT_TOTAL_DEFAULT_SEC = 21600
    AC_OUTPUT_TAIL_CHARS = 2000
    TZ = timezone(timedelta(hours=9))

    def __init__(self, phase_dir_name: str, *, auto_push: bool = False, review_enabled: bool = True):
        self._root = str(ROOT)
        self._phases_dir = ROOT / "phases"
        self._phase_dir = self._phases_dir / phase_dir_name
        self._phase_dir_name = phase_dir_name
        self._top_index_file = self._phases_dir / "index.json"
        self._auto_push = auto_push
        self._review_enabled = review_enabled
        self._base_branch = "main"
        self._config = cfg.load_config()

        if not self._phase_dir.is_dir():
            print(f"ERROR: {self._phase_dir} not found")
            sys.exit(1)

        self._index_file = self._phase_dir / "index.json"
        if not self._index_file.exists():
            print(f"ERROR: {self._index_file} not found")
            sys.exit(1)

        idx = self._read_json(self._index_file)
        self._project = idx.get("project", "project")
        self._phase_name = idx.get("phase", phase_dir_name)
        self._total = len(idx["steps"])

    def run(self):
        self._print_header()
        self._check_blockers()
        self._checkout_branch()
        self._ensure_created_at()
        self._execute_all_steps()
        review_result = self._review_phase() if self._review_enabled else None
        self._finalize(review_result)

    # --- timestamps ---

    def _stamp(self) -> str:
        return datetime.now(self.TZ).strftime("%Y-%m-%dT%H:%M:%S%z")

    # --- JSON I/O ---

    @staticmethod
    def _read_json(p: Path) -> dict:
        return json.loads(p.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(p: Path, data: dict):
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- git ---

    def _run_git(self, *args) -> subprocess.CompletedProcess:
        cmd = ["git"] + list(args)
        # git pre-push 리뷰를 건너뛴다. phase 끝의 리뷰 게이트가 같은 일을 한다.
        env = {**os.environ, "HARNESS_SKIP_GIT_REVIEW": "1"}
        return subprocess.run(cmd, cwd=self._root, capture_output=True, text=True, env=env)

    def _checkout_branch(self):
        branch = f"feat-{self._phase_name}"

        r = self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        if r.returncode != 0:
            print(f"  ERROR: git을 사용할 수 없거나 git repo가 아닙니다.")
            print(f"  {r.stderr.strip()}")
            sys.exit(1)

        current = r.stdout.strip()
        index = self._read_json(self._index_file)
        if current == branch:
            # 재실행: 최초 실행 때 기록한 base로 리뷰해야 phase 전체 diff를 본다.
            self._base_branch = index.get("base_branch", self._base_branch)
            return
        self._base_branch = current
        index.setdefault("base_branch", current)
        self._write_json(self._index_file, index)

        r = self._run_git("rev-parse", "--verify", branch)
        r = self._run_git("checkout", branch) if r.returncode == 0 else self._run_git("checkout", "-b", branch)

        if r.returncode != 0:
            print(f"  ERROR: 브랜치 '{branch}' checkout 실패.")
            print(f"  {r.stderr.strip()}")
            print(f"  Hint: 변경사항을 stash하거나 commit한 후 다시 시도하세요.")
            sys.exit(1)

        print(f"  Branch: {branch}")

    def _commit_step(self, step_num: int, step_name: str):
        output_rel = f"phases/{self._phase_dir_name}/step{step_num}-output.json"
        index_rel = f"phases/{self._phase_dir_name}/index.json"

        self._run_git("add", "-A")
        self._run_git("reset", "HEAD", "--", output_rel)
        self._run_git("reset", "HEAD", "--", index_rel)

        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self.FEAT_MSG.format(phase=self._phase_name, num=step_num, name=step_name)
            r = self._run_git("commit", "-m", msg)
            if r.returncode == 0:
                print(f"  Commit: {msg}")
            else:
                print(f"  WARN: 코드 커밋 실패: {r.stderr.strip()}")

        self._run_git("add", "-A")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self.CHORE_MSG.format(phase=self._phase_name, num=step_num)
            r = self._run_git("commit", "-m", msg)
            if r.returncode != 0:
                print(f"  WARN: housekeeping 커밋 실패: {r.stderr.strip()}")

    # --- top-level index ---

    def _update_top_index(self, status: str):
        if not self._top_index_file.exists():
            return
        top = self._read_json(self._top_index_file)
        ts = self._stamp()
        for phase in top.get("phases", []):
            if phase.get("dir") == self._phase_dir_name:
                phase["status"] = status
                ts_key = {"completed": "completed_at", "error": "failed_at", "blocked": "blocked_at"}.get(status)
                if ts_key:
                    phase[ts_key] = ts
                break
        self._write_json(self._top_index_file, top)

    # --- context ---
    # 프로젝트 규칙·문서는 claude가 CLAUDE.md(@import 포함)를 직접 로드하므로 프롬프트에 주입하지 않는다.

    @staticmethod
    def _build_step_context(index: dict) -> str:
        lines = [
            f"- Step {s['step']} ({s['name']}): {s['summary']}"
            for s in index["steps"]
            if s["status"] == "completed" and s.get("summary")
        ]
        if not lines:
            return ""
        return "## 이전 Step 산출물\n\n" + "\n".join(lines) + "\n\n"

    def _build_preamble(self, step_context: str, prev_error: Optional[str] = None) -> str:
        commit_example = self.FEAT_MSG.format(
            phase=self._phase_name, num="N", name="<step-name>"
        )
        retry_section = ""
        if prev_error:
            retry_section = (
                f"\n## ⚠ 이전 시도 실패 — 아래 에러를 반드시 참고하여 수정하라\n\n"
                f"{prev_error}\n\n---\n\n"
            )
        return (
            f"당신은 {self._project} 프로젝트의 개발자입니다. 아래 step을 수행하세요.\n\n"
            f"{step_context}{retry_section}"
            f"## 작업 규칙\n\n"
            f"1. 이전 step에서 작성된 코드를 확인하고 일관성을 유지하라.\n"
            f"2. 이 step에 명시된 작업만 수행하라. 추가 기능이나 파일을 만들지 마라.\n"
            f"3. 기존 테스트를 깨뜨리지 마라.\n"
            f"4. AC(Acceptance Criteria) 검증을 직접 실행하라.\n"
            f"5. /phases/{self._phase_dir_name}/index.json의 해당 step status를 업데이트하라:\n"
            f"   - AC 통과 → \"completed\" + \"summary\" 필드에 이 step의 산출물을 한 줄로 요약\n"
            f"   - {self.MAX_RETRIES}회 수정 시도 후에도 실패 → \"error\" + \"error_message\" 기록\n"
            f"   - 사용자 개입이 필요한 경우 (API 키, 인증, 수동 설정 등) → \"blocked\" + \"blocked_reason\" 기록 후 즉시 중단\n"
            f"6. 재시도 끝에 고친 실수가 있으면 원인→규칙 한 줄을 docs/LESSONS.md에 추가하라 "
            f"(형식: `- [YYYY-MM-DD] 규칙. 원인: … (출처: {self._phase_name}/step N) [키: 관련 경로나 단어, …]`).\n"
            f"7. 모든 변경사항을 커밋하라:\n"
            f"   {commit_example}\n\n---\n\n"
        )

    # --- Claude 호출 ---

    def _run_claude(self, prompt: str) -> subprocess.CompletedProcess:
        """worker 역할 모델로 claude를 실행한다. 타임아웃은 예외 대신 실패 결과로 돌려준다."""
        worker = self._config["roles"]["worker"]
        timeout = self._config["timeouts_sec"]["step"]
        cmd = ["claude", "-p", "--dangerously-skip-permissions", *cfg.claude_args(worker),
               "--output-format", "json", prompt]
        try:
            return subprocess.run(cmd, cwd=self._root, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, self.TIMEOUT_EXIT_CODE, "", f"timeout after {timeout}s")

    def _invoke_claude(self, step: dict, preamble: str) -> dict:
        step_num, step_name = step["step"], step["name"]
        step_file = self._phase_dir / f"step{step_num}.md"

        if not step_file.exists():
            print(f"  ERROR: {step_file} not found")
            sys.exit(1)

        step_text = step_file.read_text()
        lessons = review.relevant_lessons([step_text], Path(self._root) / "docs" / "LESSONS.md")
        result = self._run_claude(preamble + review.lessons_section("지켜야 할 LESSONS", lessons)
                                  + self._fixed_ac_section(step) + step_text)

        if result.returncode != 0:
            print(f"\n  WARN: Claude가 비정상 종료됨 (code {result.returncode})")
            if result.stderr:
                print(f"  stderr: {result.stderr[:500]}")

        output = {
            "step": step_num, "name": step_name,
            "exitCode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr,
        }
        out_path = self._phase_dir / f"step{step_num}-output.json"
        with open(out_path, "w") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        return output

    def _invoke_until_unlimited(self, step: dict, preamble: str) -> dict:
        """사용량 한도에 걸리면 기다렸다가 같은 시도를 다시 한다. 재시도 횟수는 쓰지 않는다."""
        total = self._config["timeouts_sec"].get("limit_wait", self.LIMIT_WAIT_TOTAL_DEFAULT_SEC)
        delay, waited = self.LIMIT_WAIT_FIRST_SEC, 0
        while True:
            output = self._invoke_claude(step, preamble)
            if not is_usage_limited(output) or waited >= total:
                return output
            pause = min(delay, total - waited)
            print(f"  ⏳ 사용량 한도 — {pause // 60}분 기다린 뒤 같은 시도를 다시 합니다 (누적 {waited // 60}분)")
            time.sleep(pause)
            waited += pause
            delay = min(delay * 2, self.LIMIT_WAIT_MAX_SEC)

    # --- 고정 AC ---
    # step에 ac(커맨드)·ac_files(오케스트레이터가 미리 쓴 인수 테스트)가 있으면 worker의 자기 보고보다 이 결과를 믿는다.

    @staticmethod
    def _fixed_ac_section(step: dict) -> str:
        files = step.get("ac_files") or []
        if not files:
            return ""
        return ("## 고정 AC\n\n오케스트레이터가 미리 쓴 인수 테스트다. 고치지 마라. "
                "execute.py가 끝난 뒤 AC를 직접 다시 돌리고 이 파일이 바뀌었는지 확인한다.\n"
                + "".join(f"- {f}\n" for f in files) + "\n")

    def _ac_base(self) -> str:
        """고정 AC 비교 기준 커밋. phase 첫 실행 때 한 번만 정해 index.json에 남긴다.
        실행마다 HEAD로 잡으면 앞 실행·앞 step에서 커밋된 AC 수정이 새 기준이 되어 검출되지 않는다."""
        index = self._read_json(self._index_file)
        if "ac_base" not in index:
            index["ac_base"] = self._run_git("rev-parse", "HEAD").stdout.strip()
            self._write_json(self._index_file, index)
        return index["ac_base"]

    def _uncommitted_ac_files(self, step: dict, base: str) -> list:
        return [f for f in step.get("ac_files") or [] if self._run_git("cat-file", "-e", f"{base}:{f}").returncode != 0]

    def _restore_ac_files(self, step: dict, base: str) -> Optional[str]:
        """worker가 AC 파일을 고쳤으면 기준 커밋으로 되돌리고 에러를 돌려준다."""
        files = step.get("ac_files") or []
        if not files or self._run_git("diff", "--quiet", base, "--", *files).returncode == 0:
            return None
        if self._run_git("checkout", base, "--", *files).returncode != 0:
            return f"고정 AC 파일이 바뀌었는데 되돌리지 못했다: {', '.join(files)}. 사람이 확인해야 한다."
        return f"고정 AC 파일을 고쳤다(되돌림): {', '.join(files)}. AC 파일은 그대로 두고 구현을 고쳐라."

    def _verify_ac(self, step: dict, base: str) -> Optional[str]:
        """고정 AC를 직접 확인한다. 통과하면 None, 아니면 다음 시도에 넘길 에러."""
        return self._restore_ac_files(step, base) or self._run_ac(step)

    def _run_ac(self, step: dict) -> Optional[str]:
        if not step.get("ac"):
            return None
        try:
            r = subprocess.run(step["ac"], shell=True, cwd=self._root, capture_output=True, text=True,
                               timeout=self._config["timeouts_sec"]["step"])
        except subprocess.TimeoutExpired:
            return f"AC 커맨드 시간 초과: {step['ac']}"
        if r.returncode == 0:
            return None
        return f"execute.py가 AC를 직접 돌렸더니 실패 ({step['ac']}):\n{(r.stdout + r.stderr)[-self.AC_OUTPUT_TAIL_CHARS:]}"

    def _block_step(self, step_num: int, step_name: str, reason: str):
        self._set_step(step_num, status="blocked", blocked_reason=reason, blocked_at=self._stamp())
        print(f"  ⏸ Step {step_num}: {step_name} blocked\n    Reason: {reason}")
        self._update_top_index("blocked")
        self._record_progress("blocked", f"step {step_num} {step_name} — {reason}")
        sys.exit(2)

    def _set_step(self, step_num: int, **fields):
        index = self._read_json(self._index_file)
        for s in index["steps"]:
            if s["step"] == step_num:
                s.update(fields)
        self._write_json(self._index_file, index)

    def _judge_attempt(self, step: dict, base: str) -> tuple:
        """한 시도의 결과를 (status, 에러)로 판정한다."""
        step_num = step["step"]
        current = next(s for s in self._read_json(self._index_file)["steps"] if s["step"] == step_num)
        status = current.get("status", "pending")
        err = current.get("error_message", "Step did not update status")
        # status와 관계없이 먼저 되돌린다. error 분기의 커밋이 고친 AC 파일을 담으면 안 된다.
        files_error = self._restore_ac_files(step, base)
        if status in ("blocked", "error") or not (step.get("ac") or step.get("ac_files")):
            return status, err
        ac_error = files_error or self._run_ac(step)
        if ac_error:
            return "failed", ac_error
        if status == "completed" or not step.get("ac"):
            return status, err
        # 체크포인트: worker가 시간 초과 등으로 status를 못 바꿨어도 AC가 통과하면 그 작업은 끝났다.
        summary = current.get("summary") or f"{step['name']}: worker가 마무리 전에 끝났지만 고정 AC 통과로 execute.py가 완료 처리"
        self._set_step(step_num, status="completed", summary=summary)
        print(f"  ✓ Step {step_num}: worker는 끝내지 못했지만 AC가 통과해 완료로 처리합니다")
        return "completed", err

    # --- 헤더 & 검증 ---

    def _print_header(self):
        print(f"\n{'='*60}")
        print(f"  Harness Step Executor")
        print(f"  Phase: {self._phase_name} | Steps: {self._total}")
        if self._auto_push:
            print(f"  Auto-push: enabled")
        self._print_models()
        print(f"{'='*60}")

    def _print_models(self):
        roles = self._config["roles"]
        named = [("worker", roles["worker"])] + [(r["name"], r) for r in roles["reviewers"]]
        print("  Models: " + ", ".join(f"{n}: {r['model'] or 'CLI 기본'}" for n, r in named))
        age = cfg.selection_age_days(self._config, _today())
        selected_at = self._config.get("selection", {}).get("selected_at") or "없음"
        print(f"  Selected at: {selected_at} (artificialanalysis.ai/models)")
        if age is None or age > SELECTION_MAX_AGE_DAYS:
            print(f"  WARN: 모델 선택이 없거나 {SELECTION_MAX_AGE_DAYS}일이 지났습니다. "
                  f"오케스트레이터가 사이트를 확인해 harness.config.json을 갱신하세요.")

    def _check_blockers(self):
        index = self._read_json(self._index_file)
        for s in reversed(index["steps"]):
            if s["status"] == "error":
                print(f"\n  ✗ Step {s['step']} ({s['name']}) failed.")
                print(f"  Error: {s.get('error_message', 'unknown')}")
                print(f"  Fix and reset status to 'pending' to retry.")
                sys.exit(1)
            if s["status"] == "blocked":
                print(f"\n  ⏸ Step {s['step']} ({s['name']}) blocked.")
                print(f"  Reason: {s.get('blocked_reason', 'unknown')}")
                print(f"  Resolve and reset status to 'pending' to retry.")
                sys.exit(2)
            if s["status"] != "pending":
                break

    def _ensure_created_at(self):
        index = self._read_json(self._index_file)
        if "created_at" not in index:
            index["created_at"] = self._stamp()
            self._write_json(self._index_file, index)

    # --- 실행 루프 ---

    def _execute_single_step(self, step: dict) -> bool:
        """단일 step 실행 (재시도 포함). 완료되면 True, 실패/차단이면 False."""
        step_num, step_name = step["step"], step["name"]
        done = sum(1 for s in self._read_json(self._index_file)["steps"] if s["status"] == "completed")
        prev_error = None
        base = self._ac_base()
        missing = self._uncommitted_ac_files(step, base)
        if missing:
            self._block_step(step_num, step_name, f"고정 AC 파일이 기준 커밋({base[:7]})에 없음: {', '.join(missing)}. "
                             "커밋하고 index.json의 ac_base를 지운 뒤 다시 실행")

        for attempt in range(1, self.MAX_RETRIES + 1):
            index = self._read_json(self._index_file)
            step_context = self._build_step_context(index)
            preamble = self._build_preamble(step_context, prev_error)

            tag = f"Step {step_num}/{self._total - 1} ({done} done): {step_name}"
            if attempt > 1:
                tag += f" [retry {attempt}/{self.MAX_RETRIES}]"

            with progress_indicator(tag) as pi:
                output = self._invoke_until_unlimited(step, preamble)
                elapsed = int(pi.elapsed)

            if is_usage_limited(output):
                self._set_step(step_num, status="blocked",
                               blocked_reason="사용량 한도가 limit_wait 안에 풀리지 않음. 풀린 뒤 pending으로 바꿔 다시 실행")
            status, err_msg = self._judge_attempt(step, base)
            index = self._read_json(self._index_file)
            ts = self._stamp()

            if status == "completed":
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["completed_at"] = ts
                self._write_json(self._index_file, index)
                self._commit_step(step_num, step_name)
                print(f"  ✓ Step {step_num}: {step_name} [{elapsed}s]")
                return True

            if status == "blocked":
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["blocked_at"] = ts
                self._write_json(self._index_file, index)
                reason = next((s.get("blocked_reason", "") for s in index["steps"] if s["step"] == step_num), "")
                print(f"  ⏸ Step {step_num}: {step_name} blocked [{elapsed}s]")
                print(f"    Reason: {reason}")
                self._update_top_index("blocked")
                self._record_progress("blocked", f"step {step_num} {step_name} — {reason}")
                sys.exit(2)

            if attempt < self.MAX_RETRIES:
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["status"] = "pending"
                        s.pop("error_message", None)
                self._write_json(self._index_file, index)
                prev_error = err_msg
                print(f"  ↻ Step {step_num}: retry {attempt}/{self.MAX_RETRIES} — {err_msg}")
            else:
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["status"] = "error"
                        s["error_message"] = f"[{self.MAX_RETRIES}회 시도 후 실패] {err_msg}"
                        s["failed_at"] = ts
                self._write_json(self._index_file, index)
                self._commit_step(step_num, step_name)
                print(f"  ✗ Step {step_num}: {step_name} failed after {self.MAX_RETRIES} attempts [{elapsed}s]")
                print(f"    Error: {err_msg}")
                self._update_top_index("error")
                self._record_progress("error", f"step {step_num} {step_name} — {err_msg}")
                sys.exit(1)

        return False  # unreachable

    def _execute_all_steps(self):
        while True:
            index = self._read_json(self._index_file)
            pending = next((s for s in index["steps"] if s["status"] == "pending"), None)
            if pending is None:
                print("\n  All steps completed!")
                return

            step_num = pending["step"]
            for s in index["steps"]:
                if s["step"] == step_num and "started_at" not in s:
                    s["started_at"] = self._stamp()
                    self._write_json(self._index_file, index)
                    break

            self._execute_single_step(pending)

    # --- 리뷰 게이트 ---

    def _run_review(self, out: Path, previous: Optional[dict] = None) -> Optional[dict]:
        print("\n  ▶ " + ("검증 리뷰(수정분만) 실행 중 ..." if previous else "리뷰 실행 중 ..."))
        try:
            result = review.run_review(self._base_branch, out, config=self._config, previous=previous)
        except review.ReviewError as err:
            print(f"  ✗ {err}")
            return None
        print(review.format_summary(result))
        return result

    def _fix_review_findings(self, result: dict):
        with progress_indicator("리뷰 지적 수정"):
            self._run_claude(review.build_fix_prompt(result, self._project))
        self._run_git("add", "-A")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            self._run_git("commit", "-m", self.FIX_MSG.format(phase=self._phase_name))

    def _block_phase(self, reason: str):
        index = self._read_json(self._index_file)
        index["blocked_reason"] = reason
        index["blocked_at"] = self._stamp()
        self._write_json(self._index_file, index)
        self._update_top_index("blocked")
        self._record_progress("blocked", reason)
        print(f"\n  ⏸ Phase blocked: {reason}")
        print("  해결 후 다시 실행하면 리뷰부터 재개한다.")
        sys.exit(2)

    def _unresolved_review(self, out: Path) -> Optional[dict]:
        """blocked 뒤 재실행이면 지난 리뷰 결과. 전체를 다시 보지 않고 그 지적만 검증한다."""
        if not out.exists():
            return None
        previous = self._read_json(out)
        return previous if previous.get("blocking") and previous.get("head") else None

    def _review_phase(self) -> dict:
        """P0/P1이 있으면 수정 세션 후 수정분만 검증 리뷰한다. 남으면 phase를 blocked로 두고 종료한다."""
        out = self._phase_dir / "review.json"
        rounds = 0
        result = self._run_review(out, previous=self._unresolved_review(out))
        while result and result["blocking"] and rounds < self.MAX_REVIEW_FIX_ROUNDS:
            rounds += 1
            self._fix_review_findings(result)
            result = self._run_review(out, previous=result)
        if result is None:
            self._block_phase("review: 모든 리뷰어 실패 — 리뷰어 설정을 확인하거나 --no-review로 실행")
        index = self._read_json(self._index_file)
        index["review"] = {"risk_score": result["risk"]["score"], "risk_level": result["risk"]["level"],
                           "blocking": result["blocking"], "rounds": rounds}
        self._write_json(self._index_file, index)
        if result["blocking"]:
            self._block_phase(f"review: P0/P1 {result['blocking']}건 남음 — phases/{self._phase_dir_name}/review.json 참고")
        return result

    # --- 진행 기록 ---

    def _progress_entry(self, status: str, detail: str) -> str:
        index = self._read_json(self._index_file)
        head = f"## {self._stamp()[:10]} {self._phase_name} — {status}" + (f": {detail}" if detail else "")
        rv = index.get("review")
        if rv:
            head += f" (risk {rv['risk_level']} {rv['risk_score']}, review rounds {rv['rounds']})"
        lines = [head] + [
            f"- step {s['step']} {s['name']}: {s['summary']}"
            for s in index["steps"] if s["status"] == "completed" and s.get("summary")
        ]
        sha = self._run_git("rev-parse", "--short", "HEAD").stdout.strip()
        lines.append(f"- 커밋: {self._base_branch}..{sha}")
        return "\n".join(lines) + "\n\n"

    def _record_progress(self, status: str, detail: str = ""):
        """phase 결과를 docs/PROGRESS.md에 append하고 그 파일만 커밋한다(다음 세션의 중복 작업 방지)."""
        path = Path(self._root) / self.PROGRESS_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("# PROGRESS\n\n", encoding="utf-8")
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + self._progress_entry(status, detail))
        self._run_git("add", self.PROGRESS_REL)
        self._run_git("commit", "-m", f"docs(progress): {self._phase_name} {status}", "--", self.PROGRESS_REL)

    def _finalize(self, review_result: Optional[dict] = None):
        index = self._read_json(self._index_file)
        index["completed_at"] = self._stamp()
        index.pop("blocked_reason", None)
        index.pop("blocked_at", None)
        self._write_json(self._index_file, index)
        self._update_top_index("completed")

        self._run_git("add", "-A")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = f"chore({self._phase_name}): mark phase completed"
            r = self._run_git("commit", "-m", msg)
            if r.returncode == 0:
                print(f"  ✓ {msg}")
        self._record_progress("completed")
        if review_result and not review_result["blocking"]:
            # 완료 표시·PROGRESS 커밋까지 리뷰 통과로 기록해 pre-push가 같은 범위를 다시 리뷰하지 않게 한다.
            head = self._run_git("rev-parse", "HEAD").stdout.strip()
            review.record_passed(self._run_git("merge-base", self._base_branch, head).stdout.strip(), head)
            review.archive_result(review_result, head)

        risk = (review_result or {}).get("risk", {}).get("level")
        if self._auto_push and risk in self.PUSH_BLOCKED_RISK_LEVELS:
            print(f"  ⚠ risk {risk} — push 생략. 사람이 review.json 확인 후 직접 push하세요.")
        elif self._auto_push:
            branch = f"feat-{self._phase_name}"
            r = self._run_git("push", "-u", "origin", branch)
            if r.returncode != 0:
                print(f"\n  ERROR: git push 실패: {r.stderr.strip()}")
                sys.exit(1)
            print(f"  ✓ Pushed to origin/{branch}")

        print(f"\n{'='*60}")
        print(f"  Phase '{self._phase_name}' completed!")
        print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="Harness Step Executor")
    parser.add_argument("phase_dir", help="Phase directory name (e.g. 0-mvp)")
    parser.add_argument("--push", action="store_true", help="Push branch after completion")
    parser.add_argument("--no-review", action="store_true", help="Skip cross review gate")
    args = parser.parse_args()

    StepExecutor(args.phase_dir, auto_push=args.push, review_enabled=not args.no_review).run()


if __name__ == "__main__":
    main()
