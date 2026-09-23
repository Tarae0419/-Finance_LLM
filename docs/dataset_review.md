# 초기 문항과 검수 도구

S1-05의 초기 20문항을 [검수 목록](../data/review/initial-20/README.md)에 준비했다. 모두 실제 수집한 원문에서 만든 **draft**이며 학습·정답 평가에 사용할 수 없다. 날짜는 가상 질문의 고정 조건이고 해당 날짜의 법규 적용 확인을 뜻하지 않는다. 원문 전체와 버전·공식 URL은 같은 폴더의 `examples.json`에 담았다.

## 검수하기

JSON을 직접 편집하는 대신 [로컬 검수 화면](review_workbench.md)에서 개발 50문항의 원문·부모 문맥·별표를 함께 보고 입력할 수 있다. 화면에서 내려받은 검수 JSON은 아래와 같은 등록 명령을 사용한다. 작성 중 저장 파일은 검수 등록 파일과 구분한다.

1. `review-template.json`에서 검토할 문항의 객체를 복사해 `data/review/submissions/my-review.json`에 저장한다. 한 객체 또는 객체 배열을 받을 수 있다. 이 폴더는 검수자의 신원 기록이 의도치 않게 커밋되지 않도록 Git에서 제외한다.
2. `examples.json`의 질문·답변 후보·근거를 공식 원문과 직접 대조한다. 존재·의미·시점·완전성 네 항목의 `result`와 `reason`을 각각 작성한다. 결과는 `pass`, `fail`, `uncertain`, `pending` 중 하나다. 근거 없는 범위 밖 문항은 검수 항목을 적용한 방식도 사유에 적는다.
3. 실제 검수자 이름, `reviewer_role` (`developer`/`external`), 구체적인 역할, 시간대가 있는 `review_date`, 검수 방법과 소요 초를 기입한다. 시각 형식은 `YYYY-MM-DDTHH:MM:SS+09:00`이다. `human_attested`는 본인이 실제 검수를 수행한 경우에만 `true`로 바꾼다.
4. 개발자 검수에는 금융위·금감원 공식 FAQ/해석의 `source_url`, 해당 문항·페이지인 `locator`, 대조 내용인 `note`를 `official_cross_checks`에 작성한다. URL 존재만으로 의미 대조가 끝나는 것은 아니다. 대조 자료가 없으면 모든 체크가 pass여도 `needs_review`가 된다. 확정 불가능이면 `excluded_reason`에 이유를 적는다.
5. 작성한 결과를 등록하고 상태를 조회한다.

```powershell
uv run --frozen python scripts/review_dataset.py import-reviews data/review/submissions/my-review.json --attest-human-review
uv run --frozen python scripts/review_dataset.py status
```

빈 템플릿은 의도적으로 등록에 실패한다. 도구는 검수자의 신원을 인증하거나 사람의 판단이 맞는지 자동 검증하지 않는다. 로컬에서 사람이 작성한 결과를 명시적으로 등록하는 경로다. `external_review`는 역할에서 계산하며 개발자 검수를 법률 전문가 인증으로 표현하지 않는다. 근거 ID/스냅샷/발췌 자동 대조는 사람의 의미 검수를 대신하지 않는다.

문항을 수정할 때는 `examples.json`의 해당 객체를 별도 JSON 배열 파일로 복사해 편집한 뒤 등록한다. 파일의 `review_status`는 `draft`여야 한다.

```powershell
uv run --frozen python scripts/review_dataset.py import-drafts data/review/submissions/revised-drafts.json
```

동일 내용 재등록은 멱등적이다. 내용 변경은 새 리비전을 만들며 이전 검수 결과를 상속하지 않는다. 현재 리비전·해시와 다른 검수는 거절한다. 수정 후 아래 명령으로 현재 리비전과 해시가 담긴 빈 체크리스트를 준비할 수 있다. 기존 파일은 덮어쓰지 않는다.

```powershell
uv run --frozen python scripts/review_dataset.py prepare-review --example s1-dev-01 --output data/review/submissions/review-01-r2.json
```

초기 생성 스크립트 역시 기존 검수 폴더를 덮어쓰지 않으며, 새로운 초안 묶음이 필요하면 `--output`으로 다른 폴더를 지정한다. 새로 생성한 초안을 다시 검수하기 전 기존에 사람이 수정한 리비전과 충돌하지 않는지 확인한다.

## 데이터 분할과 내보내기

초기 20건은 모두 `loan-explanation-core-dev` 그룹의 개발용이다. 같은 조문 묶음과 유사 사례를 다루므로 무작위로 train/test에 나누지 않았다. 그룹별 분할은 DB에서 고정하며 변경·교차 분할을 거절한다. 새로운 그룹 이름을 붙인 유사 사례까지 자동으로 탐지하는 기능은 아직 없으므로 후속 문항 작성 시 사람이 쟁점·사례 중복을 확인해야 한다.

```powershell
uv run --frozen python scripts/review_dataset.py export-reviewed --dataset s1-initial-20-v1 --split dev --output data/processed/dev-reviewed.json
```

현재 리비전이 `reviewed`인 문항만 내보낸다. 0건이면 실패하며 기존 출력 파일도 덮어쓰지 않는다. 이 경로로 최종 평가 `test` 자료를 내보낼 수 없다. 최종 평가셋의 사람 검수·봉인·파일 권한 분리는 아직 완료하지 않았다. `data/sealed`의 Git 제외는 봉인 자체가 아니다. S1-08/S2-05의 그룹·저장 위치·해시·접근 경로를 별도로 검증해야 한다.

## 작업량 기록

생성 manifest는 스크립트 실행 시간을 기록한다. 이는 사람이 작성·검수하는 데 걸린 시간이 아니다. 아직 사람의 검수·수정 시간 실측은 없으며 `null`로 남겼다. 검수 로그의 `duration_seconds`를 최소 5건 이상 확보한 뒤 난이도별 작업량과 후속 50/100건 일정을 다시 추정한다. 초기 20건은 개발 50건에 포함하며 중복 집계하지 않는다.
