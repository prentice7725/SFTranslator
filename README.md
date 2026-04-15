# Starfield Translation Automation

Starfield 모드 번역용 자동화 도구다.  
`ESM/ESP -> Scene 추출 -> Scene 번역 -> XML 생성 -> XML 후반 번역 -> 검수` 흐름을 지원한다.

현재 구조의 핵심은 다음과 같다.

- `step0` ~ `step6` CLI가 실제 엔진
- `auto_pipeline.py`는 얇은 오케스트레이터
- `main_gui.py`는 GUI 실행기
- `pipeline_runner.py`가 공통 실행 계약과 산출물 규칙을 관리

## 구조

```text
GUI / auto_pipeline
  -> pipeline_runner
    -> step0 ~ step6 CLI
```

## 주요 파일

- `pipeline_runner.py`: step 커맨드 생성, 산출물 경로, manifest 관리
- `auto_pipeline.py`: 자동 파이프라인 실행
- `main_gui.py`: PyQt6 GUI
- `step0_extract_xml.py`: ESM/ESP -> XML 추출
- `step1_extract_scene.py`: Scene/Dialogue 추출
- `extract_audio.py`: 오디오 샘플 추출
- `audition_profiler.py`: 화자 톤 프로파일 생성
- `step2_translate_scene.py`: Scene 번역
- `step3_build_xml.py`: XML 생성 / 머지
- `step4_translate_xml.py`: XML 후반 번역
- `step5_review_xml.py`: 스캔 / 검수 / 선택 번역
- `step6_mod_update.py`: refine / update 보정
- `db_manager.py`: SQLite + RAG
- `llm_backend.py`: LLM 백엔드 추상화
- `orchestrator.py`: 멀티 모델 generation + review

## 표준 산출물 규칙

입력 파일이 `mod.esm`일 때 기본 출력은 아래와 같다.

- `mod.step0.extracted.xml`
- `mod.step1.dump.json`
- `mod.step1.priority.json`
- `mod.audio.tone_profiles.json`
- `mod.step2.translated.json`
- `mod.step2.profile.json`
- `mod.step3.merged.xml`
- `mod.step4.translated.xml`
- `mod.step5.reviewed.xml`
- `mod.step5.scan.json`
- `mod.step6.refined.xml`
- `mod.final.xml`
- `mod.pipeline_manifest.json`

## 실행 순서

기본 자동 파이프라인은 입력 파일의 대화 유무에 따라 아래와 같은 순서로 분기하여 돈다.

**[경로 A] 대화(Scene)가 존재할 때**
```text
step0 -> step1 -> audio_extract/profile -> step2 -> review_step2 -> step3 -> step4 -> step5
```

**[경로 B] 일반 텍스트만 존재할 때**
```text
step0 -> step1 -> step3(direct-build) -> step4 -> step5
```

선택적으로 Step 6을 포함할 수 있다.

## 요구 사항

### Python

- Python 3.11+ 권장

### Python 패키지

현재 코드 기준으로 최소한 아래가 필요하다.

- `PyQt6`
- `requests`
- `json-repair`

프로젝트에 별도 `requirements.txt`는 아직 없다.

### 외부 도구

오디오 분석을 쓰려면 아래 도구가 필요하다.

- `vgmstream-cli`
- `ffmpeg`

## 설정 파일

핵심 설정 파일은 `config.json`이다.

주요 설정:

- API provider (`vertexai` / `1minai`)
- 모델 다중 라우팅 (`models.audio_profile`, `models.translation`, `models.review`)
- 파이프라인 분기 설정 (`pipeline.tone_profile_method` 등)
- API 키
- `use_ja_ref`
- `auto_audio_analysis`
- `game_data_dir`
- orchestrator 설정

추가 데이터 파일:

- `translation_db.sqlite`
- `glossary.json`
- `session_credits.json`

## 빠른 시작

### 1. GUI 실행

```bash
python main_gui.py
```

### 2. 자동 파이프라인 실행

```bash
python auto_pipeline.py --input-esp path\\to\\mod.esm --config config.json
```

resume:

```bash
python auto_pipeline.py --input-esp path\\to\\mod.esm --config config.json --resume
```

Step 6 포함:

```bash
python auto_pipeline.py --input-esp path\\to\\mod.esm --config config.json --include-step6
```

특정 단계부터 시작:

```bash
python auto_pipeline.py --input-esp path\\to\\mod.esm --from-step step3
```

## 개별 Step 실행 예시

### Step 0

```bash
python step0_extract_xml.py ^
  --input-esp mod.esm ^
  --output-xml mod.step0.extracted.xml
```

### Step 1

```bash
python step1_extract_scene.py ^
  --input-esp mod.esm ^
  --output-json mod.step1.dump.json ^
  --output-priority mod.step1.priority.json
```

### Step 2

```bash
python step2_translate_scene.py ^
  --input-json mod.step1.dump.json ^
  --config config.json ^
  --output-json mod.step2.translated.json ^
  --profile-json mod.step2.profile.json ^
  --tone-profile mod.audio.tone_profiles.json
```

### Step 3

```bash
python step3_build_xml.py ^
  --input-esp mod.esm ^
  --base-xml mod.step0.extracted.xml ^
  --input-json mod.step2.translated.json ^
  --output-xml mod.step3.merged.xml
```

### Step 4

```bash
python step4_translate_xml.py ^
  --input-xml mod.step3.merged.xml ^
  --output-xml mod.step4.translated.xml
```

### Step 5

```bash
# 기본 XML 검수 모드
python step5_review_xml.py \
  --input-xml mod.step4.translated.xml \
  --output-xml mod.step5.reviewed.xml \
  --scan-output mod.step5.scan.json

# Step 2 JSON 기반 검수/교정 (신규)
python step5_review_xml.py \
  --mode step2 \
  --input-json mod.step2.translated.json \
  --output-json mod.step2.reviewed.json \
  --scan-output mod.step2.scan.json \
  --tone-profile mod.audio.tone_profiles.json
```

### 오디오/텍스트 톤 프로파일러 (신규)

```bash
# 기본 음성 기반 생성
python audition_profiler.py -p mod.step1.priority.json -a temp/audition -o mod.audio.tone_profiles.json

# 음성 없이 대본 전집 텍스트 기반으로만 생성
python audition_profiler.py \
  --mode string \
  --input-json mod.step1.dump.json \
  -o mod.audio.tone_profiles.json
```

### Step 6

```bash
python step6_mod_update.py \
  --mode refine \
  --input-file mod.step5.reviewed.xml \
  --output-xml mod.step6.refined.xml \
  --profile-json mod.step2.profile.json
```

## 실행 계약

### 종료 코드

- `0`: 성공
- `2`: 인자 오류
- `3`: 입력 파일 없음
- `4`: 출력 실패
- `5`: 내부 예외

### 성공 출력

성공 시 각 step는 마지막에 아래 형식으로 출력한다.

```text
[OK] output=<path>
```

## 현재 상태

반영된 방향:

- CLI를 기준으로 자동/수동/GUI 실행 경로 통일
- 공통 커맨드 빌더 도입
- 표준 산출물 파일명 도입
- manifest 기반 resume 구조 도입

이미 확인한 것:

- 수정 파일 문법 검증 통과
- `auto_pipeline.py --help` 확인
- `step0` ~ `step6 --help` 확인

## 남은 작업

- 실제 샘플 ESM/ESP 기준 end-to-end 검증
- `requirements.txt` 정리
- 공통 문자열/태그 처리 모듈 분리
- Step 5 UX 세부 검증

## 문서

- 상세 내부 문서: [PROJECT_KNOWLEDGE.md](./PROJECT_KNOWLEDGE.md)

## 한 줄 요약

이 저장소는 이제 "개별 step CLI가 엔진이고, 자동 파이프라인과 GUI는 같은 CLI 계약을 호출하는 구조"로 이해하면 된다.
