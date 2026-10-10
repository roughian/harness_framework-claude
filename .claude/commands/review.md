이 프로젝트의 변경 사항을 교차 리뷰하라. 리뷰 로직은 `scripts/review.py` 하나뿐이다. 직접 리뷰하지 말고 그 결과를 정리하라.

1. 다음을 실행한다. base는 `$ARGUMENTS`이고, 비어 있으면 `main`을 쓴다.
   ```bash
   python3 scripts/review.py --base <base>
   ```
   - herdr 안이면 리뷰어마다 오른쪽 pane이 열려 병렬로 돈다. 아니면 로컬에서 병렬로 실행된다.
   - 리뷰어 구성과 모델은 `harness.config.json`의 `roles.reviewers`를 따른다.
   - 한 시간 가까이 걸릴 수 있으니 Bash timeout을 충분히 준다.
2. `.review/latest.json`을 읽고 아래 표로 요약한다. 심각도 순으로 정렬한다.

   | 등급 | 위치 | 문제 | 발견한 리뷰어 | 수정안 |
   |---|---|---|---|---|

   표 위에 verdict, risk 등급과 점수, 차단(P0/P1) 건수, 실패한 리뷰어를 한 줄로 적는다.
3. 두 리뷰어가 함께 찾은 지적(`found_by`가 2개 이상)은 신뢰도가 높다고 표시한다.
4. 코드는 고치지 않는다. 작성자와 리뷰어를 분리하기 위해서다. 사용자가 수정을 승인하면 그때 고치고, 고친 원인은 `docs/LESSONS.md`에 규칙 한 줄로 남긴다.
