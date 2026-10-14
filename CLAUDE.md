# 프로젝트: {프로젝트명}

## 기술 스택
- {프레임워크 (예: Next.js 15)}
- {언어 (예: TypeScript strict mode)}
- {스타일링 (예: Tailwind CSS)}

## 함정
이 저장소에서 틀리기 쉬운 것만 적는다. 코드나 폴더 구조를 보면 알 수 있는 내용은 적지 않는다. 형식: `- 규칙. 이유: 어기면 무엇이 깨지는지`
- {예: API 로직은 app/api/ 라우트 핸들러에만 둔다. 이유: 미들웨어 인증이 그 경로에만 걸린다}
- {예: 클라이언트 컴포넌트에서 외부 API를 직접 부르지 않는다. 이유: API 키가 번들에 노출된다}

## 개발 프로세스
- 새 기능은 테스트부터 작성하고, 그 테스트를 통과하는 구현을 쓴다. 이유: tdd-guard 훅이 테스트 없는 구현 파일 편집을 막고, 리뷰는 테스트 누락을 P1으로 본다.
- 커밋 메시지는 conventional commits 형식을 따를 것 (feat:, fix:, docs:, refactor:)
- 코드를 쓴 에이전트는 자기 코드를 승인하지 않는다. 리뷰는 `python3 scripts/review.py`가 맡는다(작은 변경은 리뷰어 1명, 크거나 민감하면 Claude+Codex 교차).

## 문서 지도
- 항상 로드(아래 import): ARCHITECTURE(구조·공유 컨벤션)
- 설계할 때만: `docs/LESSONS.md`(실수→규칙. 관련 줄을 step 파일 금지사항에 옮겨 적는다), `docs/PRD.md`(무엇을·왜), `docs/PROGRESS.md`(이미 끝난 작업 — 다시 만들지 말 것), `docs/ADR.md`(기술 결정 로그 — 스택·라이브러리를 정하거나 바꿀 때)
- UI step에서만: `docs/UI_GUIDE.md`
- 리뷰어만: `REVIEW.md` / 모델 선택 기록·병렬 설정: `harness.config.json`(오케스트레이터가 artificialanalysis.ai를 보고 갱신, `/harness` F절)

## 기록 규칙
- 실수를 고쳤으면(재시도 끝 성공, 리뷰 P0/P1 수정, 사용자 지적) 원인→규칙 한 줄(300자 이하, 1줄 1규칙)을 `docs/LESSONS.md`에 추가한다. 같은 규칙이 있으면 새 줄을 붙이지 말고 그 줄을 고친다. 50줄을 넘으면 합치거나 `## 함정`으로 옮긴다(리캡이 알려 준다).
- 같은 실수가 2번 기록되면 이 파일의 `## 함정`으로 옮기고(이유 포함) LESSONS에서 지운다.
- `docs/PROGRESS.md`는 `scripts/execute.py`가 자동 기록한다. 손으로 고치지 않는다.

## 명령어
npm run dev      # 개발 서버
npm run build    # 프로덕션 빌드
npm run lint     # ESLint
npm run test     # 테스트
python3 scripts/execute.py <phase>   # phase 실행 (+ 교차 리뷰 게이트)
python3 scripts/review.py --base main  # 리뷰만 실행
python3 scripts/pr_review.py --pr <번호>  # PR에 push 리뷰 결과 붙이기(라벨·코멘트·AI Review 체크)
scripts/install_git_hooks.sh           # git pre-push 리뷰 훅 설치 (클론 후 1회)

@docs/ARCHITECTURE.md
