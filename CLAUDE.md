# 프로젝트: {프로젝트명}

## 기술 스택
- {프레임워크 (예: Next.js 15)}
- {언어 (예: TypeScript strict mode)}
- {스타일링 (예: Tailwind CSS)}

## 아키텍처 규칙
- CRITICAL: {절대 지켜야 할 규칙 1 (예: 모든 API 로직은 app/api/ 라우트 핸들러에서만 처리)}
- CRITICAL: {절대 지켜야 할 규칙 2 (예: 클라이언트 컴포넌트에서 직접 외부 API를 호출하지 말 것)}
- {일반 규칙 (예: 컴포넌트는 components/ 폴더에, 타입은 types/ 폴더에 분리)}

## 개발 프로세스
- CRITICAL: 새 기능 구현 시 반드시 테스트를 먼저 작성하고, 테스트가 통과하는 구현을 작성할 것 (TDD)
- 커밋 메시지는 conventional commits 형식을 따를 것 (feat:, fix:, docs:, refactor:)
- 코드를 쓴 에이전트는 자기 코드를 승인하지 않는다. 리뷰는 `python3 scripts/review.py`(Claude+Codex 교차)가 맡는다.

## 문서 지도
- 항상 로드(아래 import): ARCHITECTURE(구조·공유 컨벤션), ADR(결정 로그), LESSONS(실수→규칙)
- 설계할 때만: `docs/PRD.md`(무엇을·왜), `docs/PROGRESS.md`(이미 끝난 작업 — 다시 만들지 말 것)
- UI step에서만: `docs/UI_GUIDE.md`
- 리뷰어만: `REVIEW.md` / 모델 선택 기록·병렬 설정: `harness.config.json`(오케스트레이터가 artificialanalysis.ai를 보고 갱신, `/harness` F절)

## 기록 규칙
- 실수를 고쳤으면(재시도 끝 성공, 리뷰 P0/P1 수정, 사용자 지적) 원인→규칙 한 줄을 `docs/LESSONS.md`에 추가한다. 같은 규칙이 있으면 그 줄을 갱신한다.
- 같은 실수가 2번 기록되면 이 파일의 CRITICAL 규칙으로 승격하고 LESSONS에서 지운다.
- `docs/PROGRESS.md`는 `scripts/execute.py`가 자동 기록한다. 손으로 고치지 않는다.

## 명령어
npm run dev      # 개발 서버
npm run build    # 프로덕션 빌드
npm run lint     # ESLint
npm run test     # 테스트
python3 scripts/execute.py <phase>   # phase 실행 (+ 교차 리뷰 게이트)
python3 scripts/review.py --base main  # 교차 리뷰만 실행

@docs/ARCHITECTURE.md
@docs/ADR.md
@docs/LESSONS.md
