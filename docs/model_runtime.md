# 로컬 모델 실행과 자원 측정

S1-04는 로컬 장비에서 기반 모델을 실행하고 작은 어댑터 학습의 자원 사용을 확인하는 작업이다. 법규 답변 품질 평가나 본 학습은 포함하지 않는다. 무료 공개 가중치를 내려받아 RTX 3070 Ti 8GB에서 실행하며 유료 API·클라우드는 사용하지 않는다.

## 선택과 고정 조건

- 후보: [Qwen3-4B-Instruct-2507 공식 모델 카드](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507). 4B 비사고형 모델이며 공식 저장소에서 Apache-2.0 라이선스를 확인했다. 한국어 법규 품질 비교를 통한 최종 채택은 아직 아니다.
- 모델·토크나이저 리비전: `cdbee75f17c01a7cc42f958dc650907174af0554`. [설정](../configs/model_runtime.json)과 [파일 해시 manifest](../data/manifests/model-qwen3-4b.json)에 고정했다. 가중치와 LICENSE 원문은 `models/qwen3-4b-instruct-2507`에 보관한다.
- NF4 4비트, 이중 양자화, BF16 연산, SDPA attention, GPU 전체 로딩을 사용한다. `trust_remote_code=False`, safetensors, `local_files_only=True`로 실행하며 CPU 자동 대체나 외부 추론 API 호출은 하지 않는다.
- Python 3.12.13, PyTorch 2.10.0+cu128, Transformers 4.57.6, bitsandbytes 0.49.2, PEFT 0.18.1, Accelerate 1.15.0을 사용했다. 전체 의존성은 `uv.lock`에 고정한다. Windows/CUDA 환경에서 검증했으며 다른 OS의 실행을 보장하지 않는다.

설치 근거는 [PyTorch 공식 CUDA 12.8 패키지](https://pytorch.org/get-started/previous-versions/#v2100), [bitsandbytes Windows 지원](https://huggingface.co/docs/bitsandbytes/installation), [PEFT 양자화 학습 절차](https://huggingface.co/docs/peft/developer_guides/quantization)다.

## 실제 측정 결과 — 2026-09-23

[원시 보고서](reports/model-runtime-20260923.json)에 실행 조건·패키지 버전·입력 해시·코드/잠금 파일 해시·개별 측정값을 보존했다. 최종 코드의 해시와 보고서가 일치함을 확인했다.

| 작업 | 실측 시간 | GPU 할당 최고치 | GPU 예약 최고치 |
| --- | ---: | ---: | ---: |
| 모델 로딩 | 12.90초 | 원시 보고서 참조 | 원시 보고서 참조 |
| 입력 256 / 출력 64토큰, 3회 | 중앙값 3.94초, 범위 3.74~8.25초 | 2.59GiB | 2.97GiB |
| 입력 1,024 / 출력 64토큰, 3회 | 중앙값 8.22초, 범위 8.03~8.52초 | 3.03GiB | 3.13GiB |
| 가상 입력 어댑터 준비·학습 3단계 | 2.41초 | 4.58GiB | 4.72GiB |
| 새 기반 모델 + 어댑터 재로딩·8토큰 생성 | 10.83초 | 2.60GiB | 3.09GiB |

한국어 실행 확인에서 “사과 두 개와 사과 한 개를 합하면 사과 셋이 됩니다.”를 생성했다. 어댑터의 학습 가능 파라미터는 2,949,120개였으며 저장 전후 모든 대상 파라미터의 해시가 일치했다. 첫 개발 실행에서 발생했던 생성 설정·체크포인팅·기존 어댑터 속성 경고는 greedy 기본값 명시, 비재진입 체크포인팅 명시, 새 기반 모델 재로딩으로 정리하고 최종 실행을 확인했다.

256토큰 측정은 반복 간 편차가 크다. 디스플레이와 다른 앱이 실행 중인 로컬 PC의 소규모 측정이므로 단일 숫자를 안정적인 처리량으로 일반화하지 않는다. 이 조건에서는 작은 어댑터 실험이 가능함을 확인했으며 장문·실제 학습셋의 작업량 예측은 아직 확정하지 않는다.

## 재실행

저장소 루트에서 실행한다. 모델 패키지는 선택적인 `model` 의존성 그룹으로 분리했다. 모델 명령에는 `--group model`을 붙인다.

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.cache\uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path (Get-Location) '.runtime\python'
uv sync --locked --group model
uv run --frozen --group model python scripts/download_model.py
uv run --frozen --group model python scripts/probe_model.py
```

첫 다운로드에는 공개 모델 파일 약 8GB와 CUDA 실행 패키지를 저장할 공간이 필요하다. 다운로드는 공식 리비전의 파일을 받고 큰 파일의 공식 LFS 해시를 확인한다. 이후 실행은 로컬 파일 전체의 해시를 다시 검사한다. 파일 손상·리비전 불일치·CUDA 미지원이면 실패하며 다른 모델로 몰래 대체하지 않는다.

실행 결과는 `artifacts/probes/<UTC 실행 ID>/report.json`에 기록한다. 실행 중 예외가 발생해도 실패 상태와 원인을 남긴다. 스크립트 종료 후 GPU 모델 메모리는 해제된다. 이 명령은 서버를 기동하지 않는다.

## 측정 방식과 한계

- 먼저 간단한 한국어 문장으로 생성 동작을 확인한다. 이는 법규 이해 평가가 아니다.
- 고정 입력 256/1,024토큰, 출력 64토큰, 배치 1, greedy 생성으로 각 3회 측정한다. 각 입력 길이에서 8토큰 warm-up을 별도로 수행한다. 입력은 반복한 가상 문장이며 토큰 배열 해시를 기록한다.
- 지연은 CUDA 동기화를 포함한 prefill+생성 시간이다. 모델 다운로드·로딩·법규 검색·인용 검사는 포함하지 않는다. 3회 중앙값으로 보고하며 서비스 p95나 처리량 SLA를 달성한 것으로 해석하지 않는다.
- GPU 최고치는 PyTorch의 allocated/reserved로 구분한다. 프로세스 RSS는 20ms 주기의 샘플 최고치이며 VRAM과 다른 값이다. NVIDIA 드라이버·디스플레이·다른 프로세스의 GPU 메모리는 PyTorch 수치에 포함되지 않는다. GPU 전체 점유의 연속 최고치를 측정한 것은 아니다.
- 학습 실험은 seed 42의 무작위 토큰 256개, 배치 1, AdamW 3단계, LoRA rank 8/alpha 16, q_proj/v_proj, gradient checkpointing으로 수행한다. 법규 draft 문항과 최종 평가 문항은 읽지 않는다.
- 저장한 어댑터는 기반 모델을 새로 로딩한 뒤 다시 적용해 파라미터 해시와 생성 동작을 확인한다. `PROBE_ONLY.json`과 보고서에 `eligible_for_release=false`를 기록하며 모델 릴리스 DB에 등록하지 않는다.

이 실험의 손실 감소는 금융 법규 품질 개선이 아니다. 더 긴 학습 문맥, 모든 선형층 대상 LoRA, 큰 배치와 실제 검수 데이터의 학습은 별도 측정이 필요하다. 현재 설정의 생성 속도만으로 긴 RAG 답변의 30초 목표를 충족한다고 판정할 수 없다.
