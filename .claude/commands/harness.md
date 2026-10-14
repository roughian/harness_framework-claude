이 프로젝트는 Harness 프레임워크를 사용한다. 아래 워크플로우에 따라 작업을 진행하라.

---

## 워크플로우

### A. 탐색

ARCHITECTURE는 CLAUDE.md import로 이미 로드되어 있다. 추가로 읽을 것:

1. `docs/PROGRESS.md`와 `phases/index.json`을 먼저 읽는다. 이미 completed인 기능은 다시 설계하지 않는다. blocked나 error인 phase가 있으면 그것부터 사용자에게 알린다.
2. `docs/PRD.md`를 읽고 기획 의도를 파악한다. 기술 스택이나 설계를 정해야 하면 `docs/ADR.md`도 읽는다.
3. `docs/LESSONS.md`를 읽는다. 매 세션 로드하지 않으므로, 관련 규칙은 C절 8번대로 step 파일에 옮겨 적어야 worker에게 전달된다.

코드베이스를 넓게 봐야 하면 Explore 에이전트를 병렬로 사용한다(읽기 전용이라 충돌이 없다).

### B. 논의

구현을 위해 구체화하거나 기술적으로 결정해야 할 사항이 있으면 사용자에게 제시하고 논의한다.

### C. Step 설계

사용자가 구현 계획 작성을 지시하면 여러 step으로 나뉜 초안을 작성해 피드백을 요청한다.

설계 원칙:

1. **Scope 최소화** — 하나의 step에서 하나의 레이어 또는 모듈만 다룬다. 여러 모듈을 동시에 수정해야 하면 step을 쪼갠다.
2. **자기완결성** — 각 step 파일은 독립된 Claude 세션에서 실행된다. "이전 대화에서 논의한 바와 같이" 같은 외부 참조는 금지한다. 필요한 정보는 전부 파일 안에 적는다.
3. **사전 준비 강제** — 관련 문서 경로와 이전 step에서 생성/수정된 파일 경로를 명시한다. 세션이 코드를 읽고 맥락을 파악한 뒤 작업하도록 유도한다.
4. **시그니처 수준 지시** — 함수/클래스의 인터페이스만 제시하고 내부 구현은 에이전트 재량에 맡긴다. 단, 설계 의도에서 벗어나면 안 되는 핵심 규칙(멱등성, 보안, 데이터 무결성 등)은 반드시 명시한다.
5. **AC는 실행 가능한 커맨드** — "~가 동작해야 한다" 같은 추상적 서술이 아닌 `npm run build && npm test` 같은 실제 실행 가능한 검증 커맨드를 포함한다.
6. **주의사항은 구체적으로** — "조심해라" 대신 "X를 하지 마라. 이유: Y" 형식으로 적는다.
7. **네이밍** — step name은 kebab-case slug로, 해당 step의 핵심 모듈/작업을 한두 단어로 표현한다 (예: `project-setup`, `api-layer`, `auth-flow`).
8. **LESSONS 반영** — `docs/LESSONS.md` 규칙 중 이 step에 해당하는 것을 금지사항에 옮겨 적는다. worker는 LESSONS를 자동으로 읽지 않으므로 이것이 유일한 전달 경로다.

### D. 파일 생성

사용자가 승인하면 아래 파일들을 생성한다.

#### D-1. `phases/index.json` (전체 현황)

여러 task를 관리하는 top-level 인덱스. 이미 존재하면 `phases` 배열에 새 항목을 추가한다.

```json
{
  "phases": [
    {
      "dir": "0-mvp",
      "status": "pending"
    }
  ]
}
```

- `dir`: task 디렉토리명.
- `status`: `"pending"` | `"completed"` | `"error"` | `"blocked"`. execute.py가 실행 중 자동으로 업데이트한다.
- 타임스탬프(`completed_at`, `failed_at`, `blocked_at`)는 execute.py가 상태 변경 시 자동 기록한다. 생성 시 넣지 않는다.

#### D-2. `phases/{task-name}/index.json` (task 상세)

```json
{
  "project": "<프로젝트명>",
  "phase": "<task-name>",
  "steps": [
    { "step": 0, "name": "project-setup", "status": "pending" },
    { "step": 1, "name": "core-types", "status": "pending" },
    { "step": 2, "name": "api-layer", "status": "pending" }
  ]
}
```

필드 규칙:

- `project`: 프로젝트명 (CLAUDE.md 참조).
- `phase`: task 이름. 디렉토리명과 일치시킨다.
- `steps[].step`: 0부터 시작하는 순번.
- `steps[].name`: kebab-case slug.
- `steps[].status`: 초기값은 모두 `"pending"`.
- `review`: `{risk_score, risk_level, blocking, rounds}`. 리뷰 게이트가 끝나면 execute.py가 기록한다. 생성 시 넣지 않는다.

상태 전이와 자동 기록 필드:

| 전이 | 기록되는 필드 | 기록 주체 |
|------|-------------|----------|
| → `completed` | `completed_at`, `summary` | Claude 세션 (summary), execute.py (timestamp) |
| → `error` | `failed_at`, `error_message` | Claude 세션 (message), execute.py (timestamp) |
| → `blocked` | `blocked_at`, `blocked_reason` | Claude 세션 (reason), execute.py (timestamp) |

`summary`는 step 완료 시 산출물을 한 줄로 요약한 것으로, execute.py가 다음 step 프롬프트에 컨텍스트로 누적 전달한다. 따라서 다음 step에 유용한 정보(생성된 파일, 핵심 결정 등)를 담아야 한다.

`created_at`은 execute.py가 최초 실행 시 task 레벨에 한 번만 기록한다. step 레벨의 `started_at`도 execute.py가 각 step 시작 시 자동 기록한다. 생성 시 넣지 않는다.

#### D-3. `phases/{task-name}/step{N}.md` (각 step마다 1개)

```markdown
# Step {N}: {이름}

## 읽어야 할 파일

먼저 아래 파일들을 읽어라. ARCHITECTURE는 CLAUDE.md로 이미 로드되어 있으니 다시 적지 않는다.

- {이전 step에서 생성/수정된 파일 경로}
- {UI step이면 `/docs/UI_GUIDE.md`}
- {기술 스택·라이브러리를 정하거나 바꾸는 step이면 `/docs/ADR.md`}

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라.

## 작업

{구체적인 구현 지시. 파일 경로, 클래스/함수 시그니처, 로직 설명을 포함.
코드 스니펫은 인터페이스/시그니처 수준만 제시하고, 구현체는 에이전트에게 맡겨라.
단, 설계 의도에서 벗어나면 안 되는 핵심 규칙은 명확히 박아넣어라.}

## Acceptance Criteria

```bash
npm run build   # 컴파일 에러 없음
npm test        # 테스트 통과
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택을 벗어나지 않았는가?
   - CLAUDE.md "함정"에 적힌 실수를 반복하지 않았는가?
3. 결과에 따라 `phases/{task-name}/index.json`의 해당 step을 업데이트한다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 수정 3회 시도 후에도 실패 → `"status": "error"`, `"error_message": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 수동 설정 등) → `"status": "blocked"`, `"blocked_reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- {이 step에서 하지 말아야 할 것. "X를 하지 마라. 이유: Y" 형식}
- 기존 테스트를 깨뜨리지 마라
```

### E. 실행

실행 전에 F절의 "모델 선택"을 확인한다(선택이 없거나 7일이 지났으면 먼저 고른다).

```bash
python3 scripts/execute.py {task-name}              # 순차 실행 + 리뷰 게이트
python3 scripts/execute.py {task-name} --push        # 실행 후 push (risk high 이상이면 생략)
python3 scripts/execute.py {task-name} --no-review   # 리뷰 게이트 생략
```

execute.py가 자동으로 처리하는 것:

- `feat-{task-name}` 브랜치 생성/checkout. 실행 직전 브랜치를 리뷰 base로 기억한다.
- 모델 — step은 `harness.config.json`의 `worker` 선택(model·effort·timeout)으로 실행한다. 헤더에 모델과 선택 일자를 보여 주고, 오래됐으면 경고한다.
- 컨텍스트 — 규칙 문서는 CLAUDE.md import로 claude가 직접 로드한다. 프롬프트에는 이전 step summary와 작업 규칙만 넣는다.
- 자가 교정 — 실패나 타임아웃 시 최대 3회 재시도하며, 이전 에러를 프롬프트에 피드백한다.
- 2단계 커밋 — 코드 변경(`feat`)과 메타데이터(`chore`)를 분리 커밋한다.
- **리뷰 게이트** — 모든 step이 끝나면 `scripts/review.py`로 리뷰한다(병렬 subprocess). 결과는 `phases/{task-name}/review.json`에 남는다.
  - 첫 리뷰: 변경이 작고(`review.single_reviewer_max_lines`, 기본 100줄) 민감 경로(`review.full_review_paths`)가 아니면 리뷰어 1명, 아니면 Claude+Codex 교차.
  - P0/P1이 있으면 worker가 수정(`fix(...)` 커밋)하고 **검증 리뷰**를 한다. 이전 차단 지적과 그 뒤의 수정 diff만 보고, 지적했던 리뷰어만 다시 돈다. 최대 2번이다.
  - 그래도 남으면 phase를 blocked로 두고 종료한다(exit 2). 재실행하면 전체를 다시 보지 않고 남은 지적만 검증한다.
  - 통과한 커밋은 `.review/passed`에 남아 pre-push가 다시 리뷰하지 않는다.
- **진행 기록** — completed, blocked, error로 끝날 때마다 `docs/PROGRESS.md`에 결과를 append하고 커밋한다.
- execute.py의 push는 pre-push 리뷰를 건너뛴다(`HARNESS_SKIP_GIT_REVIEW=1`). 위 리뷰 게이트가 같은 일을 하기 때문이다.

에러 복구:

- **step error**: `phases/{task-name}/index.json`에서 해당 step의 `status`를 `"pending"`으로 바꾸고 `error_message`를 삭제한 뒤 재실행한다.
- **step blocked**: `blocked_reason`의 사유를 해결한 뒤, `status`를 `"pending"`으로 바꾸고 `blocked_reason`을 삭제한 뒤 재실행한다.
- **리뷰 blocked** (task 레벨 `blocked_reason`이 `review:`로 시작): `review.json`의 P0/P1을 직접 고치거나, 고치지 않기로 정했으면 그 사유를 사람이 기록한 뒤 재실행한다. 재실행은 남은 지적만 검증한다.

### F. 오케스트레이션과 병렬 작업

**오케스트레이터는 사용자가 처음 명령을 내린 이 세션이다.** harness는 오케스트레이터 모델을 정하지 않는다.
worker·reviewer 모델도 고정하지 않는다. 오케스트레이터가 실행할 때 고른다.

herdr 화면 배치 (herdr 안일 때):
- 작업을 시작하면 먼저 `python3 scripts/herdr_layout.py setup`을 실행한다. 이 세션 pane 이름이 "오케스트레이션"이 되고, 오른쪽에 "리캡" pane이 열린다.
  - **탭1에는 오케스트레이션과 리캡만 둔다.** 사람이 보는 곳이다.
  - 리캡은 5초마다 갱신한다. 맨 위 "지금 무슨 일이 일어나고 있나요" 절은 고등학생도 읽을 수 있는 문장으로 진행 단계, 검사 중인 AI 수와 경과 시간, 찾은 문제와 예시, 위험도 이유, 걸린 시간을 보여 준다(`scripts/recap_story.py`). 그 아래 "⚠ 사람이 정해야 해요" 칸에 입력을 기다리는 pane, 멈춘 phase, 남은 차단 지적, 실패한 검사관, LESSONS 정리 필요를 모은다. "자세히" 아래에는 작업 탭별 pane 상태, `phases/index.json` 현황, 최근 리뷰 결과를 보여 준다.
- 작업 pane(phase 실행, 병렬 worker)은 `herdr pane split`을 직접 쓰지 말고 `python3 scripts/herdr_layout.py split --cwd <경로> --label <이름>`으로 연다(새 pane_id 출력).
  - "작업 1", "작업 2" … 탭에 **탭당 최대 8개**(2행×4열 격자)로 들어간다. 다 차면 다음 탭을 만든다. 포커스는 옮기지 않는다.
  - `--label`은 리캡에 표시되므로 `phase:1-api`처럼 무엇을 하는지 적는다.

모델 선택:

1. `harness.config.json`의 `selection.selected_at`이 비었거나 7일이 지났으면, https://artificialanalysis.ai/models 를 WebFetch로 확인한다.
2. 아래 기준으로 고른다.

   | 역할 | 후보 | 기준 |
   |---|---|---|
   | worker | `claude` CLI로 돌릴 수 있는 Anthropic 모델 | Intelligence Index 상위권 중 출력 속도·비용이 좋은 것. 순차 step이 많기 때문 |
   | reviewer `claude` | Anthropic 모델 | Index 최상위. **worker와 다른 모델**로 고른다. 작성자의 약점을 보완하기 위해서다 |
   | reviewer `codex` | `codex` CLI로 돌릴 수 있는 OpenAI 모델 | Index 최상위. model id가 불확실하면 비워서 codex 기본값을 쓴다 |
   | 세션 안 에이전트 | Agent tool의 `model` 인자 | 아래 배정표의 기본 등급에서 시작한다(frontmatter에는 모델이 없다) |

3. `roles.*.model`, `selection.selected_at`(YYYY-MM-DD), `selection.rationale`(점수·속도·비용 한 줄)을 갱신하고, 고른 결과를 사용자에게 알린다.
   - effort는 `max`를 유지한다.
   - model이 빈 값이면 CLI 기본 모델을 쓴다.

병렬화 판단 규칙 — 오케스트레이터는 작업을 나누기 전에 매번 이 순서로 판단한다:

1. **작업 목록을 만들고 의존성을 표시한다.** 각 작업이 건드릴 파일과 필요한 선행 산출물을 적는다.
2. **병렬 가능 = 겹치는 파일이 없고, 서로의 산출물이 필요 없는 작업.** 읽기 전용 작업(탐색·조사·리뷰)은 항상 병렬 가능하다.
3. **phase 안의 step은 직렬로 둔다.** step끼리는 서로의 산출물에 의존한다. 병렬로 돌리면 결과를 맞추는 비용이 이득보다 크다.
4. **병렬 가능한 작업은 한 번에 함께 띄운다**(Agent 호출은 한 메시지에 여러 개). 유형별 배정은 아래 표를 따른다. 모델은 위 "모델 선택" 기준으로 정한다.

   | 작업 유형 | 배정 | 기본 모델 등급 | 실행 위치 |
   |---|---|---|---|
   | 코드베이스 탐색·파일 찾기 | `Explore` 에이전트(범위별로 여러 개) | `haiku` | 세션 안 |
   | 웹·문서 조사 | `general-purpose` 에이전트 | `sonnet` | 세션 안 |
   | 설계 대안 비교 | `Plan` 에이전트 | `opus` | 세션 안 |
   | 파일이 겹치지 않는 작은 구현 | `implementer` 에이전트(worktree 격리) | worker와 같은 등급 | 세션 안 |
   | 독립 phase 전체 | `execute.py`를 작업 pane에서 실행 | `roles.worker` | 작업 탭 |
   | 구현 직후 1차 리뷰 | `reviewer` 에이전트 | worker와 다른 등급 | 세션 안 |
   | 정식 리뷰 | `scripts/review.py` (작으면 1명, 크거나 민감하면 Claude+Codex) | `roles.reviewers` | 백그라운드 subprocess |
   | 막힌 문제·2차 진단 | `codex:codex-rescue` | codex 기본 | 세션 안 |

   - 작은 모델(`haiku`)은 결과를 쉽게 확인할 수 있는 조회·검색에만 쓴다. 작은 모델이 잘못 읽어 메인 세션이 엉뚱한 파일을 헤매면, 그 작업 유형은 한 등급 올린다.
   - 등급은 시작점일 뿐이다. 모델 선택 절차에서 더 나은 선택이 나오면 그것을 따른다.

5. **동시 실행 상한**: 독립 phase는 `parallel.max_worktrees`(기본 3)개까지만 동시에 돌린다. 리뷰가 따라가야 하기 때문이다. 세션 안 읽기 전용 에이전트는 이 상한에 포함하지 않는다.
6. **독립 phase를 병렬로 돌리는 법**:
   ```bash
   # 1) 시작 전에 docs/ARCHITECTURE.md의 "공유 컨벤션"을 확정하고 커밋한다.
   # 2) phase마다 worktree를 만들고, 작업 탭에 pane을 열어 실행한다.
   #    (herdr worktree create는 새 workspace를 열어 리캡에 안 보이므로 쓰지 않는다.)
   git worktree add ../wt-{task-name} -b feat-{task-name}
   P=$(python3 scripts/herdr_layout.py split --cwd ../wt-{task-name} --label phase:{task-name})
   herdr pane run "$P" "python3 scripts/execute.py {task-name}"
   ```
7. 병렬 작업이 끝나면 결과를 모아 사용자에게 한 번에 보고한다. 진행 중 상태는 리캡 pane이 보여 주므로 따로 중계하지 않는다. 보고는 아래 세 제목 순서로 쓴다. 사람이 먼저 봐야 할 것을 위에 둔다.
   - **내 결정 필요** — 사용자가 답하거나 승인해야 진행되는 것(차단 리뷰, blocked phase, 선택지)
   - **바뀐 것** — 변경 파일과 테스트 결과
   - **발견한 것** — 작업 중 알게 된 위험, 확인하지 못한 것과 어디까지 봤는지

### G. 리뷰가 도는 곳 (3곳)

| 시점 | 장치 | 리뷰 범위 | 막는 조건 |
|---|---|---|---|
| phase 끝 | `scripts/execute.py` 리뷰 게이트 | 첫 리뷰 → 수정 → 검증 리뷰(수정분만), 최대 2번 | 차단 지적이 남음(REVIEW.md 차단 규칙) → blocked |
| `git push` | `.githooks/pre-push` → `scripts/git_review_gate.py` | push 범위. 이미 통과한 범위(`.review/passed`)는 건너뛰고, 직전 push에서 막혔으면 그 지적과 수정분만 검증한다 | 차단 지적, 리뷰어 전원 실패, 새 브랜치의 기준(main·origin/main) 없음 |
| PR을 연 뒤 | `python3 scripts/pr_review.py --pr <번호>` (이 Mac에서) | 리뷰하지 않는다. push 때 남은 결과(`.review/results/<커밋>.json`)를 붙인다 | 위험도 라벨·코멘트·상태 체크 `AI Review`. 차단 지적이나 critical의 RFC/ADR 링크 누락이면 failure |

- 커밋에는 리뷰를 걸지 않는다. 커밋마다 리뷰하면 가장 느려지고, push 전 리뷰와 겹친다.
- 로컬 게이트는 **실수로 리뷰를 건너뛰는 것**만 막는다(dangerous-cmd-guard가 `--no-verify`를 막는다). 의도적 우회는 막지 않는다. 진짜 강제는 GitHub 상태 체크 `AI Review`를 required로 건 branch protection이 한다.
- 설치: 저장소를 받으면 `scripts/install_git_hooks.sh`를 한 번 실행한다(`core.hooksPath=.githooks`).
- Claude가 push할 때는 `run_in_background`로 실행한다. pre-push 리뷰가 Bash timeout보다 길 수 있다.
- 리뷰어(codex·claude)는 이 Mac의 로그인(OAuth)으로 돈다. API 키나 GitHub Actions가 필요 없다. PR 단계도 리뷰를 다시 돌리지 않는다(기록이 없는 커밋만 지금 리뷰).
- merge를 막으려면(선택) GitHub 저장소 Settings → Branches → main 규칙 → "Require status checks to pass"에서 `AI Review`를 고른다. 이 체크는 `pr_review.py`를 한 번 실행한 뒤부터 목록에 나타난다.
- `harness.config.json`의 `github.reviewers`(medium), `seniors`(high·critical), `notify`(critical 알림)는 사람 리뷰어의 GitHub 아이디다. 혼자 개발하면 비워 둔다(비어 있으면 아무것도 하지 않는다).

### H. 새 프로젝트로 시작하기

이 저장소를 그대로 clone하면 원격(origin)이 프레임워크 저장소를 가리킨다. 새 프로젝트는 원격을 바꿔 자기 저장소에 올린다.

```bash
git clone https://github.com/roughian/harness_framework-claude.git my-app
cd my-app
git remote rename origin framework        # 프레임워크 원본. 업데이트를 받을 때만 쓴다(필요 없으면 git remote remove origin)
# GitHub에서 빈 저장소 my-app을 만든 뒤
git remote add origin https://github.com/<아이디>/my-app.git
git push -u origin main
gh repo set-default <아이디>/my-app        # pr_review.py가 이 저장소를 쓰게 한다
scripts/install_git_hooks.sh               # pre-push 리뷰 훅
```

- 그다음 CLAUDE.md·`docs/`를 채우고 `/harness`로 진행한다. `phases/`, `docs/PROGRESS.md`, `docs/LESSONS.md`의 프레임워크 기록은 새 프로젝트에 맞게 비운다.
- 프레임워크 업데이트를 받으려면 `git fetch framework && git merge framework/main`(충돌은 프로젝트 쪽 문서에서 주로 난다).
