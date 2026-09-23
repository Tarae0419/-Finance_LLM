# 초기 데이터 모델 구현

PRD의 8개 엔터티를 [0001_core.sql](../migrations/0001_core.sql)에 정의했다. 원문 수집과 스냅샷은 [수집 결과](corpus_status.md)를 참고한다.

- `source_document → document_version → provision`으로 출처·버전·계층을 연결한다. 조문 부모는 같은 버전의 조문만 참조할 수 있다.
- `applicability`는 문서 시행일과 별개다. `verified`에는 적용 시작일·검수자·검수 시각이 필요하며 빈 종료일만으로 지원 기간을 확정하지 않는다.
- `provision_relation`의 미확보 대상은 `to_id = null`, `verified = false`, 원문 링크의 `target_locator`로 남긴다. 링크의 JavaScript는 실행하지 않는다.
- `corpus_snapshot.version_ids`라는 논리 관계는 `snapshot_version` 연결 테이블로 구현해 존재하는 버전만 포함한다. 현재 활성 스냅샷은 `corpus_state`의 단일 포인터로 관리한다.
- 스냅샷 생성은 후보 등록만 수행한다. 활성화에는 검증 완료 상태·명시적인 지원 기간·시점 검수된 근거 목록이 필요하며 트랜잭션으로 포인터를 교체한다. 현재 실제 수집본은 모두 미검수이므로 활성화하지 않았다.
- `model_release`는 기반 리비전과 선택적 어댑터·학습 데이터·평가 보고서를 연결한다. 아직 실제 모델 릴리스를 등록하지 않았다.
- `query_trace`는 요청 ID·기준일·버전·근거·상태·지연 시간을 보존하며 질문 원문 컬럼은 없다. 실제 사용자 질의 경로와 추적 기록 쓰기는 S2 작업이다.
- 예제·검수 이력은 [0002_dataset_review.sql](../migrations/0002_dataset_review.sql)에 추가했다. `dataset_group`이 분할을 고정하고, `example_revision`과 `example_evidence`가 수정본·근거를 보존한다. `review_log`는 정확한 리비전과 내용 해시를 참조한다.
- `example_review_state`와 `current_example` 뷰의 `review_status`는 최신 검수 이력으로 계산한다. 검수 없는 새 버전은 `draft`다. 네 항목 모두 통과 및 외부 검토 또는 공식 교차 대조 기록 조건이 충족되어야 `reviewed`가 된다. 사람이 검수하지 않은 자동 초안은 승격하지 않는다.
- 검수 이력·문항 버전·근거·그룹 분할의 UPDATE/DELETE를 막는다. 수정은 새 리비전, 재검수는 새 로그로 남긴다. 이 구현은 로컬 개발 도구이며 DB 소유자의 직접 조작까지 차단하는 인증 체계는 아니다.

## 실행

```powershell
uv run --frozen python -m finreg.migrate
uv run --frozen python scripts/collect_corpus.py
# 확보한 원문을 그대로 재등록할 때
uv run --frozen python scripts/collect_corpus.py --offline-manifest data/manifests/corpus-20260922T135852661978Z.json
```

`configs/sources.json`은 실제 확인한 고정 버전 목록이다. 최신 버전을 자동으로 탐색하는 갱신기는 아니다. 수집한 원문과 해시가 다르면 등록을 거절한다. 같은 원문 재등록은 기존 레코드를 덮어쓰지 않는다.

마이그레이션은 파일명과 LF로 정규화한 SQL의 해시를 기록한다. 적용한 SQL 파일의 내용이 바뀌면 실패하므로 이후 스키마 변경은 새 마이그레이션 파일로 추가한다. 여러 수집 자료의 등록은 하나의 트랜잭션으로 묶어 실패 시 부분 스냅샷을 남기지 않는다.
