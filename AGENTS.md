# FinReg 작업 지침

기능 개발 시 [개발 스킬](SKILL.md), [PRD](docs/FinReg_LLM_PRD_v1.md), [스프린트 계획](docs/FinReg_Sprint_Plan_v1.md)의 관련 요구사항과 완료 조건을 따른다.

## 기능 브랜치

- 새 기능의 구현을 시작하기 전에 해당 기능용 브랜치를 만들고 전환한다. 브랜치 이름은 사용자가 지정한 **`feat/<기능명>`** 형식을 사용한다.
- 기능명은 영어 소문자와 하이픈으로 작성하고, 구현 대상을 드러낸다. 예: `feat/project-setup`, `feat/corpus-ingestion`, `feat/hybrid-search`, `feat/citation-validation`.
- 같은 기능을 이어서 작업할 때는 해당 기능의 기존 브랜치를 사용한다. 서로 다른 기능은 별도 브랜치로 나누며, 하나의 기능에 필요한 연관 티켓은 같은 브랜치에서 작업할 수 있다.
- 분기 전 현재 브랜치와 작업 트리를 확인하고, 필요한 선행 변경이 포함된 기준 브랜치에서 분기한다. 기존 미커밋 변경을 임의로 삭제하거나 덮어쓰지 않는다.

## 프로젝트 Wiki

- 프로젝트 지식을 찾을 때 [Wiki 목차](wiki/index.md)에서 관련 개념과 출처를 찾고 원본 문서·코드로 확인한다. Wiki는 파생 요약이며 PRD·검수 정책·실제 구현보다 우선하지 않는다.
- Wiki를 변경할 때 [운영 규칙](wiki/AGENTS.md)을 따른다. 원본 변경에 영향을 받는 페이지·출처 해시·목차·로그를 함께 갱신하고 `uv run --frozen python scripts/check_wiki.py`로 검사한다.
- Wiki에는 평가 질문·정답·해설과 사용자 질문 원문을 축적하지 않는다. 법규 답변용 Wiki 검색은 [도입 설계](docs/llm_wiki.md)의 비교 평가 전까지 활성화하지 않는다.
