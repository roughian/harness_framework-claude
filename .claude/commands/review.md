이 프로젝트의 변경 사항을 교차 리뷰하라. 리뷰 로직은 `scripts/review.py` 하나뿐이다. 직접 리뷰하지 말고 그 결과를 정리하라.

1. 다음을 실행한다. base는 `$ARGUMENTS`이고, 비어 있으면 `main`을 쓴다.
   ```bash
   python3 scripts/review.py --base <base>
   ```
   - 리뷰어는 병렬 subprocess로 돈다. 진행 상황은 리캡의 "지금 무슨 일이" 절에 나온다.
   - 변경이 작고(`review.single_reviewer_max_lines`) 민감 경로가 아니면 첫 리뷰어 1명, 아니면 `roles.reviewers` 전원이 돈다. `--reviewers`로 직접 고를 수 있다.
   - 오래 걸릴 수 있으니 `run_in_background`로 실행한다.
2. `.review/latest.json`을 읽고 아래 표로 요약한다. 심각도 순으로 정렬한다.

   | 등급 | 위치 | 문제 | 재현 | 확인 | 발견한 리뷰어 | 수정안 |
   |---|---|---|---|---|---|---|

   - 맨 위에 **결정 필요**를 먼저 적는다: 차단 지적(REVIEW.md 차단 규칙) 건수와 실패한 리뷰어. 없으면 "없음".
   - 그다음 줄에 verdict, risk 등급과 점수를 적는다.
   - 확인하지 못한 지적(`confirmed: false`)은 "미확인"으로 표시한다. 차단 여부는 REVIEW.md 차단 규칙을 따른다.
3. 두 리뷰어가 함께 찾은 지적(`found_by`가 2개 이상)은 신뢰도가 높다고 표시한다.
4. 코드는 고치지 않는다. 작성자와 리뷰어를 분리하기 위해서다. 사용자가 수정을 승인하면 그때 고치고, 고친 원인은 `docs/LESSONS.md`에 규칙 한 줄로 남긴다.
