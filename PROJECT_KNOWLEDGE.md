# Starfield Translation Automation 프로젝트 지식 문서

## 1. 목적

이 프로젝트는 Starfield의 모드 플러그인 파일(`.esm`, `.esp`)에서 번역 가능한 문자열을 추출하고, 대사/일반 XML/UI 문자열을 단계적으로 번역해 최종 XML 결과물을 만드는 자동화 도구다.

지원 범위는 대략 다음과 같다.

- ESM/ESP 레코드 파싱
- `.strings`, `.dlstrings`, `.ilstrings`, `.ba2` 기반 현지화 문자열 참조
- 대사 Scene 추출
- 오디오 샘플 추출과 화자 톤 프로파일 생성
- Scene 번역
- xTranslator 호환 XML 생성
- XML 후반 번역
- 태그 검수 / 미번역 보완
- 외부 XML/Strings 기반 추가 보정

---

## 2. 2026-04-15 기준 핵심 구조

이번 리팩토링의 핵심 원칙은 다음 한 줄이다.

`CLI = 엔진`, `auto_pipeline = 오케스트레이터`, `GUI = 실행 UI`

현재 레이어는 이렇게 본다.

```text
GUI / auto_pipeline
  -> pipeline_runner
    -> step0 ~ step6 CLI
```

각 레이어의 책임은 다음과 같다.

- `step0` ~ `step6`
  - 실제 작업 수행
  - 단독 실행 가능
  - 실질 로직 보유
- `pipeline_runner.py`
  - 공통 CLI 계약 정의
  - step별 표준 커맨드 생성
  - 산출물 네이밍 규칙 관리
  - manifest 경로와 step 메타 관리
- `auto_pipeline.py`
  - step 순서 orchestration만 담당
  - step 로직 직접 구현하지 않음
  - CLI만 호출
- `main_gui.py`
  - 사용자 입력 / 실행 / 로그 표시
  - 직접 인자 조립 대신 공통 커맨드 빌더 사용

---

## 3. 진짜 엔트리 포인트

중요한 진입점은 아래 셋이다.

- `main_gui.py`
  - PyQt6 GUI
- `auto_pipeline.py`
  - 자동 일괄 실행 진입점
- `step0_extract_xml.py` ~ `step6_mod_update.py`
  - 개별 step CLI

실제 유지보수 관점에서는 다음 순서로 읽는 것이 좋다.

1. `pipeline_runner.py`
2. `auto_pipeline.py`
3. `main_gui.py`
4. `step1_extract_scene.py`
5. `step2_translate_scene.py`
6. `step3_build_xml.py`
7. `step4_translate_xml.py`
8. `step5_review_xml.py`
9. `step0_extract_xml.py`
10. `step6_mod_update.py`
11. `db_manager.py`
12. `llm_backend.py`
13. `orchestrator.py`
14. `extract_audio.py`
15. `audition_profiler.py`

---

## 4. 공통 실행 계약

### CLI 네이밍 규칙

- 입력: `--input-*`
- 출력: `--output-*`
- 옵션: `--flag`
- bool: `--flag`

기존 짧은 인자(`-i`, `-o`)도 일부 남아 있지만, 앞으로 기준은 표준 long option이다.

### 종료 코드

- `0`: 성공
- `2`: 인자 오류
- `3`: 입력 파일 없음
- `4`: 출력 실패
- `5`: 내부 예외

### 성공 로그 규칙

각 step는 성공 시 마지막에 다음 형식을 출력한다.

```text
[OK] output=<path>
```

### 공통 커맨드 빌더

`pipeline_runner.py`의 `build_step_command(step_name, **kwargs)`가 GUI와 자동 파이프라인의 단일 커맨드 생성 지점이다.

이 함수가 관리하는 것:

- step별 스크립트 파일명
- 표준 인자 이름
- legacy 인자와 신규 인자 매핑
- 선택 옵션 연결

---

## 5. 현재 표준 산출물 규칙

입력 모드 파일명을 `<mod>.esm`이라고 할 때 기본 산출물은 다음과 같다.

- Step 0: `<mod>.step0.extracted.xml`
- Step 1: `<mod>.step1.dump.json`
- Step 1 Priority: `<mod>.step1.priority.json`
- Audio Profile: `<mod>.audio.tone_profiles.json`
- Step 2: `<mod>.step2.translated.json`
- Step 2 Profile: `<mod>.step2.profile.json`
- Step 3: `<mod>.step3.merged.xml`
- Step 4: `<mod>.step4.translated.xml`
- Step 5: `<mod>.step5.reviewed.xml`
- Step 5 Scan: `<mod>.step5.scan.json`
- Step 6: `<mod>.step6.refined.xml`
- Final: `<mod>.final.xml`
- Manifest: `<mod>.pipeline_manifest.json`

이 규칙의 목적은 덮어쓰기 충돌을 없애는 것이다.

특히 주의할 점:

- Step 0 XML과 Step 3 XML은 더 이상 같은 파일명이 아니다.
- Step 5 scan JSON도 별도 파일이다.
- 최종 배포용 산출물은 `final.xml` 계열로 분리된다.

---

## 6. 전체 실행 흐름

자동 파이프라인 기준 기본 순서는 아래와 같으며, **입력 ESM/ESP 내 대화(scene) 존재 여부에 따라 자동으로 분기**합니다.

```text
step0 -> step1 -> [대화 존재 여부 판정 (branch_detect)]
```

### [A] Scene-분기 (대화가 존재할 때)
```text
(오디오 추출 선택) -> audio_profile (audio or string 모드) -> step2 -> review_step2(신규) -> step3 -> step4 -> step5
```

### [B] Direct XML-분기 (아이템/UI 등 일반 문자열만 존재할 때)
```text
step3 (direct_build) -> step4 -> step5
```

선택적으로 Step 6을 포함할 수 있습니다 (`--include-step6`).

단계별 의미는 다음과 같다.

- `step0_extract_xml.py`
  - 원본 ESM/ESP에서 xTranslator 스타일 XML 추출
- `step1_extract_scene.py`
  - Scene/대사/화자 구조를 JSON으로 추출
  - 오디오 분석용 priority list 생성
- `extract_audio.py`
  - priority list를 기반으로 음성 샘플 확보
- `audition_profiler.py`
  - 화자 톤/말투 가이드 생성 (`--mode audio` 또는 텍스트 기반의 `--mode string` 지원)
- `step2_translate_scene.py`
  - Scene JSON 번역
  - quest별 프로파일 생성
- **[신규]** `review_step2` (via `step5_review_xml.py --mode step2`)
  - 번역된 JSON 자체를 검수(미번역/태그 무결성)하여 톤 프로파일에 맞춰 재번역 자동 반영
- `step3_build_xml.py`
  - Step 0 XML과 Step 2 결과를 합쳐 XML 생성 (`--direct-build` 지원으로 우회 가능)
- `step4_translate_xml.py`
  - 나머지 XML/UI 문자열 번역
- `step5_review_xml.py`
  - 최종 XML 태그 오류/미번역 스캔 및 선택 번역
- `step6_mod_update.py`
  - 외부 XML/Strings에 대한 refine/update

---

## 7. 주요 파일 역할

### `pipeline_runner.py`

이번 리팩토링의 중심 파일이다.

주요 책임:

- `PipelinePaths`
  - 작업별 표준 산출물 경로 묶음
- `StepSpec`
  - step 이름, 스크립트, 필수 입력, 출력 정의
- `build_job_paths(...)`
  - 입력 mod 기준 경로 묶음 계산
- `build_step_command(...)`
  - 실행 커맨드 생성
- `PipelineManifest`
  - manifest 로드/저장
- `print_ok(...)`
  - 성공 로그 표준 출력

### `auto_pipeline.py`

지금은 얇은 오케스트레이터다.

핵심 특징:

- step 직접 import 대신 subprocess 호출
- `--resume` 지원
- `--from-step` 지원
- `--include-step6` 지원
- `--work-dir` 지원
- manifest 업데이트
- 마지막에 `step5.reviewed.xml` 또는 `step6.refined.xml`을 `final.xml`로 복사

자동 파이프라인은 더 이상 step별 세부 로직을 소유하지 않는 것이 원칙이다.

### `main_gui.py`

PyQt6 기반 GUI다.

핵심 구성:

- `ConfigManager`
  - `config.json` 로드/저장
- `WorkerThread`
  - subprocess 실행
  - stdout 로그를 GUI로 전달
  - 현재는 `build_step_command(...)`를 사용
- `MainApp`
  - 탭 생성
  - 파일 선택
  - 실행 버튼 핸들러
  - 로그 출력

중요한 변화:

- 예전처럼 GUI가 step별 CLI를 직접 조립하지 않는다.
- 공통 커맨드 빌더를 따라간다.

### `db_manager.py`

SQLite 기반 DB 계층이다.

주요 테이블:

- `glossary`
- `translation_memory`
- `npc_names`
- `reference_strings`

주요 용도:

- 용어집
- 번역 메모리
- NPC 이름 참조
- 일본어/기타 레퍼런스 문자열 저장

주요 인터페이스:

- `load_glossary_db()`
- `DBRAG.find_exact()`
- `DBRAG.find_fuzzy()`
- `DBRAG.get_reference_string()`
- `DBRAG.save_reference_string()`

### `llm_backend.py`

모든 LLM 백엔드의 공통 추상화다.

지원 백엔드:

- Vertex AI (provider: `vertexai`)
- OpenAI API (provider: `openai` 등)
- 1min.ai (provider: `1minai`)

핵심 기능:
- `get_llm_backend(config_dict, step_prompt_key, role=None, ...)`
- **역할별 모델 라우팅**: config 내 `models.audio_profile`, `models.translation`, `models.review` 설정값을 읽고 지정된 역할에 맞는 최적의 LLM을 자동으로 라우팅한다.

### `orchestrator.py`

복수 모델 generation + review 조합을 담당한다.

핵심 개념:

- 여러 생성 모델 병렬 호출
- review 모델이 최종 선택
- 캐시 사용 가능

연결 위치:

- Step 2
- Step 4

### `step0_extract_xml.py`

ESM/ESP를 직접 읽어 XML을 만든다.

핵심 요소:

- `StringsLoader`
  - `.strings`, `.dlstrings`, `.ilstrings`, `.ba2` 처리
- `EspParser`
  - 레코드 파싱
- `write_xml(...)`
  - xTranslator 호환 XML 작성

Step 0은 번역이 아니라 추출 단계다.

### `step1_extract_scene.py`

대사 구조 추출의 핵심 파일이다.

주요 결과:

- quest / scene / dial / dialogue 구조화
- speaker 추론
- `priority_list.json` 생성

핵심 클래스:

- `StarfieldSceneExtractor`

중요한 출력:

- Scene 번역용 dump JSON
- 오디오 분석용 priority JSON

### `extract_audio.py`

`priority_list.json`을 읽고 음성 샘플을 확보한다.

핵심:

- loose file 우선 탐색
- BA2 탐색
- basename fallback 탐색

### `audition_profiler.py`

오디오 샘플과 텍스트를 바탕으로 화자 톤 가이드를 만든다.

출력:

- `tone_profiles.json` 또는 표준 `*.audio.tone_profiles.json`

### `step2_translate_scene.py`

Scene 번역의 핵심이다.

주요 역할:

- quest별 장면 분위기 프로파일 생성
- 대사/선택지 번역
- tone profile 반영
- 일본어 참조 사용 가능

핵심 출력:

- translated JSON
- profile JSON

### `step3_build_xml.py`

두 가지 모드가 있다.

- ESM 기반 XML 생성 모드
- 기존 XML + JSON 머지 모드

현재 표준 파이프라인에서는 사실상 다음 조합이 핵심이다.

- `--input-esp`
- `--base-xml`
- `--input-json`
- `--output-xml`

### `step4_translate_xml.py`

Step 2에서 다루지 않은 나머지 XML/UI 문자열을 번역한다.

핵심 특징:

- 중복 문장 dedupe
- exact/fuzzy RAG 사용
- 태그 보존
- progress XML 저장
- orchestrator 사용 가능

### `step5_review_xml.py`

후반 검수 단계다.

주요 기능:

- `--scan-only`
  - 미번역 / 태그 오류 스캔
- `--translate-indices`
  - 선택 항목만 추가 번역

출력:

- reviewed XML
- scan JSON

### `step6_mod_update.py`

본선 자동 파이프라인의 기본 단계는 아니고 보정 단계에 가깝다.

지원 모드:

- `refine`
- `update`

입력:

- XML
- Strings 계열 파일

---

## 8. Step별 표준 CLI

### Step 0

```bash
python step0_extract_xml.py \
  --input-esp mod.esm \
  --output-xml mod.step0.extracted.xml
```

### Step 1

```bash
python step1_extract_scene.py \
  --input-esp mod.esm \
  --output-json mod.step1.dump.json \
  --output-priority mod.step1.priority.json
```

### Step 2

```bash
python step2_translate_scene.py \
  --input-json mod.step1.dump.json \
  --config config.json \
  --output-json mod.step2.translated.json \
  --profile-json mod.step2.profile.json \
  --tone-profile mod.audio.tone_profiles.json \
  --use-ja-ref
```

### Step 3

```bash
python step3_build_xml.py \
  --input-esp mod.esm \
  --base-xml mod.step0.extracted.xml \
  --input-json mod.step2.translated.json \
  --output-xml mod.step3.merged.xml
```

### Step 4

```bash
python step4_translate_xml.py \
  --input-xml mod.step3.merged.xml \
  --output-xml mod.step4.translated.xml
```

### Step 5

```bash
python step5_review_xml.py \
  --input-xml mod.step4.translated.xml \
  --output-xml mod.step5.reviewed.xml \
  --scan-output mod.step5.scan.json
```

### Step 6

```bash
python step6_mod_update.py \
  --mode refine \
  --input-file mod.step5.reviewed.xml \
  --output-xml mod.step6.refined.xml \
  --profile-json mod.step2.profile.json
```

---

## 9. 설정 파일과 데이터 파일

### `config.json`

가장 중요한 설정 파일이다.

포함 내용:

- API provider (`vertexai` 또는 `1minai` 명시 지원)
- 모델 라우팅 (`models`: `audio_profile`, `translation`, `review` 분리)
- 파이프라인 옵션 (`pipeline`: `tone_profile_method` 등)
- API 키
- `use_ja_ref`
- `auto_audio_analysis`
- `game_data_dir`
- orchestrator 설정

### `glossary.json`

초기 용어 데이터 소스 역할을 한다.

실제 런타임에서는 SQLite 쪽 데이터도 함께 본다.

### `translation_db.sqlite`

실전용 참조 데이터 저장소다.

### `session_credits.json`

1min.ai 사용량 기록용이다.

### progress 파일

현재 중간 저장 파일로 남을 수 있는 것은 주로 다음이다.

- `*.progress.json`
- `*.progress.xml`

---

## 10. 유지보수 포인트

### 무엇을 고치면 어디가 깨질 수 있는가

- CLI 인자 이름 변경
  - auto pipeline
  - GUI
  - 수동 실행 문서
- 산출물 파일명 변경
  - 다음 step 입력 경로
  - resume / manifest
  - GUI 기본값
- DB schema 변경
  - Step 1 / Step 2 / Step 4 / Step 5 참조 경로
- `llm_backend.py` 변경
  - Step 2 / Step 4 / Step 5 / 오디오 프로파일링 영향

### 현재 중복/함정

아직 구조 정리가 덜 끝난 부분도 있다.

- 문자열 로딩 / 번역 대상 판정 코드가 여러 파일에 흩어져 있음
- 용어집 / 태그 보호 로직이 Step 4, Step 5 등에 분산
- 프롬프트 경로와 설정 참조가 step별로 따로 존재

장기적으로는 아래 분리가 필요하다.

- `shared_strings.py`
- `shared_xml_translation.py`

### Step 6에 대한 판단

Step 6은 본선 자동 번역 메인 플로우라기보다 후처리 성격이 강하다.

따라서 기본 자동 파이프라인에서는 제외하고, 필요 시 `--include-step6`로 켜는 현재 방향이 맞다.

---

## 11. 현재 리팩토링 상태

완료된 것:

- `pipeline_runner.py` 추가
- `auto_pipeline.py` 얇은 오케스트레이터화
- Step 0 ~ Step 6 표준 long option 수용
- 성공 시 `[OK] output=...` 출력
- GUI의 subprocess 커맨드 조립을 공통 빌더로 통일
- 산출물 파일명 규칙 표준화
- manifest 파일 경로 도입

검증된 것:

- 수정 파일 `py_compile` 통과
- `auto_pipeline.py --help` 확인
- `step0` ~ `step6 --help` 확인
- 공통 커맨드 빌더 출력 확인

아직 남은 것:

- 실제 샘플 ESM/ESP 기준 end-to-end 실행 검증
- GUI에서 Step 5 scan/translate UX 실제 동작 점검
- legacy 옵션과 신규 옵션의 현장 사용성 재점검
- 공통 모듈 분리

---

## 12. 이 프로젝트를 이해할 때의 정신모형

가장 중요하게 기억할 점 5개만 남기면 아래다.

1. 기준은 항상 CLI다.
2. 자동 파이프라인과 GUI는 CLI를 호출하는 껍데기여야 한다.
3. Step 0 XML과 Step 3 XML은 다른 파일이어야 한다.
4. Scene 번역과 XML 번역은 다른 문제이므로 Step 2와 Step 4는 분리되어야 한다.
5. Step 6은 메인 파이프라인이 아니라 선택적 보정 단계다.

---

## 13. 빠른 파일 인덱스

- `pipeline_runner.py`
  - 공통 실행 계약, 산출물 경로, command builder, manifest
- `auto_pipeline.py`
  - 얇은 자동 오케스트레이터
- `main_gui.py`
  - PyQt6 GUI, WorkerThread, step 실행 UI
- `step0_extract_xml.py`
  - ESM/ESP -> XML 추출
- `step1_extract_scene.py`
  - Scene 구조 추출
- `extract_audio.py`
  - 오디오 샘플 추출
- `audition_profiler.py`
  - 화자 톤 프로파일 생성
- `step2_translate_scene.py`
  - Scene 번역
- `step3_build_xml.py`
  - XML 생성 / 머지
- `step4_translate_xml.py`
  - XML 후반 번역
- `step5_review_xml.py`
  - 검수 / 스캔 / 선택 번역
- `step6_mod_update.py`
  - refine / update
- `db_manager.py`
  - SQLite / RAG
- `llm_backend.py`
  - LLM 백엔드 추상화
- `orchestrator.py`
  - 멀티 모델 generation + review

---

## 14. 한 줄 결론

이 프로젝트는 이제 "개별 step CLI가 실제 엔진이고, 자동 실행과 GUI는 그 엔진을 공통 계약으로 호출하는 구조"로 이해하면 된다.
