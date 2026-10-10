# REVIEW — 코드 리뷰 기준

`scripts/review.py`의 모든 리뷰어(Claude, Codex)와 `.claude/agents/reviewer.md`가 이 파일을 읽는다.
짧게 유지한다. 길어지면 중요한 규칙이 희석된다.

## 범위
- base 대비 diff가 **새로 만든** 문제만 지적한다. 원래 있던 문제는 `pre_existing: true`로 표시한다(차단하지 않음).
- 모든 지적에는 `file`과 줄 범위를 단다. 근거 없는 추측은 쓰지 않는다.
- 포맷·린트는 지적하지 않는다. lint 훅이 처리한다.
- 제외: lockfile, `phases/**/*-output.json`, `.review/`, 빌드 산출물.

## 심각도 (P0·P1이 차단 기준)
| 등급 | 의미 | 예 |
|---|---|---|
| P0 | 즉시 차단 | 보안 취약점(인젝션, 비밀값 노출, 권한 우회), 데이터 손실, 빌드·테스트 파괴 |
| P1 | 머지 전 수정 | 명백한 버그(null, off-by-one, 미처리 예외·Promise), 신규 로직의 테스트 누락, CLAUDE.md CRITICAL 위반, **docs/LESSONS.md 규칙 재발** |
| P2 | 개선 권장 | 성능 문제(N+1, 불필요한 재렌더), 에러 처리 미흡, 문서 갱신 필요 |
| P3 | nit | 네이밍, 가독성. **리뷰당 최대 5개** |

## 체크리스트
1. 아키텍처: `docs/ARCHITECTURE.md`의 디렉토리 구조와 공유 컨벤션을 따르는가
2. 기술 스택: `docs/ADR.md`의 결정을 벗어나지 않는가
3. 테스트: 새로 추가하거나 바꾼 로직마다 테스트가 있는가. 기존 테스트를 지우거나 약하게 만들지 않았는가
4. CRITICAL 규칙과 LESSONS 규칙 위반이 없는가
5. 보안: 입력 검증, 인증·인가, 비밀값, shell·SQL 호출
6. Breaking change: 공개 API 시그니처, DB 스키마, 환경 변수
7. 문서: 이 변경 때문에 ARCHITECTURE나 ADR 내용이 틀려지지 않았는가

## Risk Score (0–100, 축 합계)
| 축 | 최대 | 기준 |
|---|---|---|
| security | 30 | auth·권한·비밀값·외부 패키지·shell/SQL을 건드림 |
| scope | 20 | 파일 5개·100줄 이하면 0–4, 파일 30개·2000줄 초과면 20 |
| breaking | 20 | 공개 API, DB 스키마, 환경 변수, 하위 호환 |
| tests | 15 | 신규 로직 대비 테스트 부족, 기존 테스트 삭제·수정 |
| migration | 15 | DB 마이그레이션, 데이터 백필, 롤백 전략 필요 |

등급(`review.py`가 계산): low 0–15 / medium 16–35 / high 36–60 / critical 61+.
high 이상이면 자동 push를 하지 않고 사람이 확인한다.

## 출력
`scripts/review_schema.json` 형식의 JSON 하나만 출력한다. 문제가 없으면 `findings: []`로 두고, `summary`에 남은 위험이나 테스트 공백을 적는다.
