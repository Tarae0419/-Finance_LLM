# FinReg

은행 대출상품 설명에 필요한 금융 법규를 근거·기준일과 함께 확인하는 자체 운영 LLM 프로젝트다. 현재 Sprint 1에서 API·DB 기반, 공식 원문 수집, 초기 문항과 검수 이력, 로컬 모델 자원 측정 도구를 구현했다. 사용자용 법규 답변 API는 아직 제공하지 않는다.

## 개발 환경

Python 3.12, uv, PostgreSQL을 사용한다. 저장소 루트에서 실행한다.

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.cache\uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path (Get-Location) '.runtime\python'
$env:UV_PYTHON_INSTALL_BIN = '0'
$env:UV_PYTHON_INSTALL_REGISTRY = '0'
uv sync --locked
```

처음 설정할 때 `.env.example`을 `.env`로 복사하고 로컬 DB 접속 정보를 수정한다. 이미 `.env`가 있으면 덮어쓰지 않는다. `.env`와 원문·가중치·실행 캐시는 Git에서 제외한다.

Docker가 설치되어 있으면 다음 명령으로 개발 DB를 실행할 수 있다. PostgreSQL은 로컬 호스트의 55432 포트로만 노출한다. 기존 DB를 사용할 때는 `FINREG_DATABASE_URL`을 해당 로컬 연결로 지정한다.

```powershell
docker compose up -d db
uv run --frozen uvicorn finreg.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

- `GET /health/live`: API 프로세스 동작 확인.
- `GET /health/ready`: DB에 `SELECT 1`을 실행한다. 설정 누락·연결 실패는 503이며 접속 비밀값을 반환하지 않는다.
- `http://127.0.0.1:8000/docs`: API 명세.

현재 readiness는 API/DB 기반 환경 검사다. 법규 말뭉치·모델·법률 답변 품질이 준비됐다는 뜻은 아니다.

Windows에서 Docker가 없으면 다음 도구가 공식 PostgreSQL 바이너리를 `.runtime/postgres`에 받고 개발 DB 두 개와 무작위 비밀번호의 `.env`를 생성한다. 이 방식을 사용할 때는 `.env.example`을 먼저 복사하지 않는다. 기존 `.env`나 DB 데이터를 덮어쓰지 않으며 시스템 서비스를 등록하지 않는다.

```powershell
uv run --frozen python scripts/dev_db.py bootstrap
# 이후 실행과 종료
uv run --frozen python scripts/dev_db.py start
uv run --frozen python scripts/dev_db.py stop
```

`bootstrap`은 DB를 시작한 상태로 끝난다. 이미 실행 중일 때 `start`를 반복하지 않는다. Docker 방식과 같은 포트를 사용하므로 두 방식을 동시에 실행하지 않는다. 한글 경로에서 PostgreSQL 초기화가 실패하는 문제를 피하기 위해 `.runtime/postgres`에 임시 드라이브 별칭(R:~Z: 중 빈 문자)을 연결한다. 데이터는 프로젝트 안에 유지하고 `stop`에서 별칭을 해제한다.

## 프로젝트 Wiki

LLM이 출처를 읽고 개념·결정·관계를 축적하는 [Wiki](wiki/index.md)를 사용한다. 현재는 개발 지식 탐색용이며, 법규 답변의 근거는 버전이 고정된 공식 원문에서 확인한다. Wiki를 서비스 검색에 연결하는 방안은 기존 RAG와 비교한 뒤 채택한다.

- [근거·버전·시점](wiki/concepts/evidence-and-time.md)
- [RAG와 Wiki의 역할](wiki/concepts/retrieval-and-wiki.md)
- [검수·평가 데이터 경계](wiki/concepts/evaluation-boundary.md)
- [도입 판단과 운영 방법](docs/llm_wiki.md)

출처가 바뀌거나 파일 링크가 끊어지면 다음 검사가 실패한다. 검사는 의미 검수나 법규 최신성 검증을 대신하지 않는다.

```powershell
uv run --frozen python scripts/check_wiki.py
```

Wiki의 법규 검색 보조를 검증할 [오프라인 실험 도구](docs/wiki_retrieval.md)도 제공한다. BM25 원문 검색과 Wiki 후보 확장을 비교하며, 검증된 스냅샷·사람 검수된 개발 문항만 사용한다. 현재 실제 데이터는 이 선행 조건을 충족하지 않아 가상 규정으로 처리 경로를 검증했다. 서비스 검색이나 답변 생성은 활성화하지 않았다.

```powershell
uv run --frozen python scripts/experiment_wiki.py status
```

## 개발 문항 검수

이 PC에서는 [개발 50문항 검수 화면](artifacts/review-workbench/development-20260923/index.html)을 브라우저로 열어 질문·답변 초안과 원문·부모 조문·별표 PDF를 함께 확인할 수 있다. 작성 내용을 파일로 저장·복원하고 검수 결과를 기존 등록 명령용 JSON으로 내보낸다. DB 상태를 자동 변경하지 않는다.

다른 환경에서는 [검수 화면 생성·등록 안내](docs/review_workbench.md)에 따라 새 묶음을 만든다. 실제 검수와 원문 스냅샷 활성화는 별도 작업이다.

## 검증

```powershell
uv run --frozen ruff check .
uv run --frozen pytest -q
```

실제 PostgreSQL 연결 검사는 `FINREG_TEST_DATABASE_URL`에 별도 테스트 DB URL을 지정하면 실행한다. 지정하지 않으면 해당 검사는 건너뛴 것으로 보고한다.

## 문서

- [제품 요구사항](docs/FinReg_LLM_PRD_v1.md)
- [개발 스킬](SKILL.md)
- [스프린트 계획](docs/FinReg_Sprint_Plan_v1.md)
- [개발 결정 기록](docs/decisions.md)
- [Sprint 1 진행 기록](docs/Sprint_1_Progress.md)
- [원문 수집 결과](docs/corpus_status.md)
- [데이터 모델과 수집 명령](docs/data_model.md)
- [사람 검수 정책](docs/review_policy.md)
- [초기 20문항 검수 목록](data/review/initial-20/README.md)
- [검수 파일 작성·등록 방법](docs/dataset_review.md)
- [로컬 모델 실행·자원 측정](docs/model_runtime.md)
- [브랜치 지침](AGENTS.md)
