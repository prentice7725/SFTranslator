# PRD: Starfield Translation Automation

## 1. 목적

Starfield 모드 플러그인(`.esm`, `.esp`)의 텍스트를 추출하고, LLM 기반 번역/검수/말투 정리를 거쳐 최종적으로 게임에 적용 가능한 XML, Strings, ESM 패치 산출물을 생성하는 자동화 도구를 제공한다.

이 제품의 핵심 가치는 다음 세 가지다.

- 대형 모드도 중단/재개 가능한 안정적인 번역 파이프라인 제공
- 화자, 퀘스트, 선택지 문맥을 반영한 자연스러운 한국어 대사 번역
- LLM 비용과 실행 시간을 통제하면서 품질이 필요한 구간에만 고급 검수 적용

## 2. 배경

기존 수동 번역 흐름은 xTranslator, 문자열 추출 도구, 수동 문맥 확인, 개별 LLM 호출, 재병합 작업이 분리되어 있어 대형 모드 번역에 많은 시간이 든다. 특히 대사형 모드는 단순 문자열 번역만으로는 화자의 말투, 선택지 맥락, 퀘스트 흐름이 쉽게 무너진다.

초기 고품질 모드는 모든 청크에 다중 모델 후보 생성과 감수 모델을 적용했지만, 대형 모드에서 수 시간 이상 걸리고 토큰 비용이 크게 증가했다. 따라서 현재 제품 방향은 `fast first, orchestrate only when risky`이다.

## 3. 대상 사용자

- Starfield 모드 한국어 번역자
- 대형 퀘스트/대사 모드를 관리하는 번역 팀
- xTranslator XML 또는 `.strings` 기반 패치 산출물이 필요한 모드 제작자
- LLM 번역을 쓰되 비용, 재실행, 품질 검수를 통제하고 싶은 고급 사용자

## 4. 범위

### In Scope

- ESM/ESP에서 번역 가능한 문자열 추출
- 퀘스트/씬/대사/선택지 구조 추출
- 음성 또는 텍스트 기반 화자 톤 프로파일 생성
- Scene JSON 기반 LLM 번역
- 위험 청크만 오케스트레이션 재번역
- 화자별 말투 가이드를 반영한 JSON 기반 톤 정리
- XML 병합 및 잔여 XML 문자열 번역
- 태그 손상, 미번역 문자열 검수
- 최종 XML 및 선택적 ESM/Strings 패치 빌드
- GUI, CLI, 자동 파이프라인의 공통 실행 계약 유지

### Out of Scope

- 사람이 보는 번역 편집기 전체 기능
- 번역 메모리 충돌 해결 UI
- 모든 Starfield 레코드 타입의 완전한 수동 편집 UX
- Node/Electron 앱의 상세 PRD
- LLM 공급자별 계정/IAM 자동 설정

## 5. 제품 원칙

- **비용 우선 제어**: 모든 대사에 고급 모델을 쓰지 않는다.
- **문맥 보존**: Scene, Speaker, PlayerChoices 구조는 XML 병합 전까지 최대한 유지한다.
- **문장 단위 추적성**: 모든 번역 대상 문자열은 `step0`부터 `final.xml`/Step7 패치까지 `stable_id`, `source_hash`, `context_id`를 유지해야 하며, 병합은 원문 텍스트 매칭이 아니라 `stable_id` 기반으로 수행한다.
- **재개 무결성**: 장시간 작업은 `.progress`와 manifest로 중단 후 이어갈 수 있어야 하며, 입력/설정/모델/프롬프트 해시가 일치할 때만 이전 산출물을 재사용한다.
- **안전한 실패**: IAM 권한 오류, 인증 오류, 치명적 설정 오류는 청크 분할로 오해하지 않고 즉시 중단한다.
- **검수 가능성**: 위험 청크, 스캔 결과, 오케스트레이션 로그는 별도 산출물로 남긴다.

## 6. 핵심 사용자 시나리오

### 6.1 자동 번역

1. 사용자가 GUI 또는 CLI에서 `.esm/.esp` 파일과 config를 지정한다.
2. 파이프라인이 대화형 모드인지 일반 문자열 모드인지 자동 판정한다.
3. 대화형 모드라면 Scene 구조와 화자 정보를 보존한 JSON을 생성한다.
4. Step2가 fast 단일 모델로 1차 번역한다.
5. 실패/위험 청크만 오케스트레이션으로 재번역한다.
6. Step2 JSON 검수와 화자별 말투 정리를 수행한다.
7. Step3 이후 XML 병합, 잔여 번역, 최종 검수를 거친다.
8. 사용자는 `final.xml` 또는 Step7 결과 폴더를 받는다.

### 6.2 대형 모드 재개

1. 작업 중 사용자가 중지하거나 API 오류가 발생한다.
2. `.progress.json`과 `pipeline_manifest.json`이 진행 상태와 산출물 해시를 보존한다.
3. 사용자가 resume 옵션으로 다시 실행한다.
4. 입력 파일, config, 모델, 프롬프트 버전, 도구 버전 해시가 이전 manifest와 일치하는지 검증한다.
5. 일치하는 산출물만 건너뛰고, 불일치 단계는 재실행한다.

### 6.3 비용 절약형 고품질 번역

1. 사용자가 high_quality 모드를 선택한다.
2. 시스템은 `orchestrator.enabled=true`, `orchestrator.mode=risky_only`로 동작한다.
3. 대부분 청크는 fast 모델 1회 호출로 처리한다.
4. JSON 누락, 태그 손실, 미번역, 출력 과다 등 위험 청크만 고급 모델 검수를 적용한다.

## 7. 파이프라인 요구사항

### 7.1 공통 단계

```text
step0 -> step1 -> branch_detect
```

- `step0`: ESM/ESP에서 XML 추출
- `step1`: Scene/Dialogue 구조 JSON 추출
- `branch_detect`: Scene 존재 여부 판정

### 7.2 Scene 분기

```text
audio_extract/audio_profile(optional)
-> step2
-> review_step2
-> scene_refine(optional)
-> step3
-> step4
-> step5
-> step7(optional)
```

`scene_refine`은 기존 XML Step6가 아니라 Step3 이전의 JSON 기반 톤 정리 단계다. 이 단계는 `Speaker`와 `step2.profile.json`을 이용해 화자별 말투를 보정한다.

### 7.3 Direct XML 분기

```text
step3(direct-build)
-> step4
-> step5
-> step6(optional)
-> step7(optional)
```

Scene 정보가 없는 모드는 JSON 기반 톤 정리가 의미 없으므로 기존 XML Step6를 선택적으로 사용한다.

### 7.4 Preflight Check

LLM 호출 전 다음 항목을 검증하고, 실패 시 번역 단계에 진입하지 않는다.

- config schema와 필수 값
- API key와 credential 파일 존재 여부
- GCP IAM 권한 및 모델 접근 가능 여부
- 입력 파일 접근 가능 여부와 출력 폴더 쓰기 권한
- 외부 도구 경로
- 예상 문자열 수, 예상 청크 수, 예상 토큰 수, 예상 비용 범위
- 번역 금지 후보와 위험 예상 구간

### 7.5 Dry Run

`--dry-run`은 LLM 호출 없이 다음 리포트를 출력한다.

- 추출 가능한 문자열 수
- Scene 모드 여부
- 예상 청크 수와 토큰 수
- 예상 비용 범위
- 오디오 분석 대상 수
- 번역 금지 후보 수
- 위험 예상 구간

## 8. 식별자 및 데이터 계약

### 8.1 Stable ID

모든 번역 대상 문자열은 immutable `stable_id`를 가져야 한다. `BatchID`는 LLM 프롬프트 내부의 임시 처리 단위로만 사용하고, 안전 병합 또는 캐시 키의 기본 식별자로 사용하지 않는다.

기본 구성:

```text
stable_id = plugin_name + form_id + record_type + subrecord_path + field_index + source_hash
```

Scene 모드 추가 context:

```text
quest_id
scene_id
topic_id
topic_info_id
speaker_id
speaker_name
choice_group_id
line_order
```

Direct XML 모드 추가 context:

```text
xml_node_path
record_index
source_text_hash
```

동일 원문 문자열은 context 없이 단순 재사용하거나 병합하지 않는다.

### 8.2 Translation Class

Step1은 모든 문자열에 번역 처리 분류를 부여한다.

```text
TRANSLATE
COPY_AS_IS
REVIEW_ONLY
SKIP_INTERNAL
LOCKED_TERM
```

스크립트 property, Editor ID, Form ID, debug key, internal marker, animation event, sound event, quest alias, variable token은 기본적으로 `COPY_AS_IS`, `REVIEW_ONLY`, 또는 `SKIP_INTERNAL` 후보로 분류한다.

### 8.3 Step별 Schema

`step1` output item:

```text
stable_id: string
source: string
source_hash: string
context_id: string
record_type: string
form_id: string
subrecord_path: string
field_index: number
speaker_id?: string
speaker_name?: string
quest_id?: string
scene_id?: string
topic_id?: string
topic_info_id?: string
choice_group_id?: string
line_order?: number
tags: array
translatable: boolean
translation_class: string
```

`step2` output item:

```text
stable_id: string
source: string
source_hash: string
context_id: string
translation: string
model: string
chunk_id: string
risk_flags: array
glossary_hits: array
translation_class: string
```

`step3` merge input은 `stable_id`, `source_hash`, `translation`, `translation_class`를 필수로 요구한다.

### 8.4 Manifest Integrity

`pipeline_manifest.json`은 진행 상태뿐 아니라 재사용 가능성을 판단할 수 있는 해시를 저장한다.

```json
{
  "input_file_hash": "...",
  "step0_xml_hash": "...",
  "config_hash": "...",
  "prompt_version": "step2-v3",
  "model_translation": "gemini-...",
  "model_review": "gpt-...",
  "tool_version": "0.3.1",
  "artifact_hashes": {
    "step2.translated.json": "...",
    "step3.merged.xml": "..."
  }
}
```

config, 모델, prompt, 원본 ESM/ESP, 또는 중간 산출물 해시가 바뀌면 해당 단계 이후 resume을 무효화한다.

## 9. Step2 번역 요구사항

### 9.1 Fast First

Step2는 기본적으로 역할별 translation 모델을 사용해 단일 호출 번역을 수행한다.

설정:

```json
{
  "step2_chunk_size": 40,
  "step2_max_chunk_chars": 3500
}
```

- `step2_chunk_size`: 청크당 최대 대사 수
- `step2_max_chunk_chars`: 청크당 원문 총 글자 수 상한

대형 모드에서 `MAX_TOKENS`가 잦으면 `step2_max_chunk_chars`를 `2500~3000`으로 낮춘다.

### 9.2 Adaptive Chunking

청크는 대사 수와 원문 글자 수뿐 아니라 추정 토큰 기준으로도 분할되어야 한다. 토큰 추정기는 MVP 안정성 요구사항이며 향후 개선으로 미루지 않는다.

필수 동작:

- record_type 기준 1차 분할
- scene/quest/topic 단위 보존
- `estimated_input_tokens + estimated_output_tokens` 기준 분할
- 40개 미만이어도 긴 대사가 많으면 더 작은 청크로 분할
- `MAX_TOKENS` 발생 시 binary split
- `MAX_TOKENS` 발생 시 오케스트레이션보다 청크 분할을 우선
- 분할 후에도 기존 context window는 짧게 유지

청크 로그는 다음 필드를 남긴다.

```json
{
  "chunk_id": "scene_001_chunk_003",
  "line_count": 28,
  "source_chars": 2840,
  "estimated_input_tokens": 1900,
  "estimated_output_tokens": 1600,
  "model": "fast_translation",
  "retry_count": 1,
  "split_reason": "MAX_TOKENS"
}
```

### 9.3 Risk Detection

위험 판정은 `Fatal Risk`, `Quality Risk`, `Review Hint`로 분류한다.

Fatal Risk:

- JSON 파싱 실패
- `stable_id` 누락
- 번역 결과가 비어 있음
- 태그/변수 토큰 손상
- XML 병합 불가

Quality Risk:

- 원문과 번역이 동일함
- 출력이 원문 대비 과도하게 김
- 한국어 비율이 낮음
- 종결어미 혼용
- speaker tone 불일치

Review Hint:

- 고유명사 다수
- 선택지 문장
- 퀘스트 목표문
- 짧은 UI 문자열
- lore/터미널 장문

위험 청크는 `*.risk_report.json`에 기록한다.

Fatal Risk는 즉시 재시도, 분할, 또는 중단 대상이다. Quality Risk는 risky-only orchestration 대상이다. Review Hint는 저비용 스캔 또는 리포트 대상이며, 단독으로 고급 오케스트레이션을 강제하지 않는다.

### 9.4 Risky-only Orchestration

설정:

```json
{
  "orchestrator": {
    "enabled": true,
    "mode": "risky_only",
    "generation_models": [],
    "review_model": {}
  }
}
```

동작:

- `mode=off`: 단일 모델만 사용
- `mode=always`: 모든 청크를 오케스트레이션
- `mode=risky_only`: fast 번역 후 위험 청크만 오케스트레이션

`MAX_TOKENS`는 위험 품질 문제가 아니라 청크 크기 문제이므로 오케스트레이션하지 않고 먼저 분할한다.

### 9.5 Glossary and Term Lock

Step2, review_step2, scene_refine은 동일한 glossary와 locked term 규칙을 참조한다.

```json
{
  "source": "Constellation",
  "target": "컨스텔레이션",
  "type": "faction",
  "lock": true,
  "case_sensitive": false,
  "notes": "공식 번역 우선"
}
```

참조 우선순위:

```text
locked user glossary
> official/base game terminology
> existing approved translation
> context-based LLM translation
> model default
```

## 10. 화자별 톤 정리 요구사항

### 10.1 JSON 기반 처리

화자 말투 정리는 XML이 아니라 Scene JSON에서 수행해야 한다. XML 단계에서는 `Speaker`, `QuestID`, `PlayerChoices` 문맥이 손실될 수 있기 때문이다.

입력:

- `*.step2.reviewed.json` 또는 `*.step2.translated.json`
- `*.step2.profile.json`

출력:

- `*.step2.tone_refined.json`

### 10.2 교정 대상

모든 대사를 LLM에 다시 보내지 않는다. 다음과 같이 명백히 말투가 어긋난 NPC 대사만 선별한다.

- 반말 캐릭터인데 `습니다`, `습니까`, `세요`, `해요` 등 존댓말 종결 사용
- 해요체 캐릭터인데 반말/문어체 종결이 강함
- 하오체 캐릭터인데 현대 구어체나 하십시오체가 섞임
- 하십시오체 캐릭터인데 반말/해요체/하오체가 섞임

Player는 상황에 따라 말투가 바뀔 수 있으므로 자동 강제 교정 대상에서 제외한다.

### 10.3 Tone Profile Confidence

`step2.profile.json`은 화자별 confidence와 근거 대사를 포함한다.

```json
{
  "speaker_id": "NPC_xxx",
  "speaker_name": "Some NPC",
  "tone": "banmal",
  "confidence": 0.72,
  "evidence_lines": ["..."],
  "allow_mixed_register": true,
  "locked_rules": [
    "do_not_change_names",
    "do_not_change_lore_terms"
  ]
}
```

자동 말투 교정은 `confidence >= 0.8`일 때만 강하게 적용한다. `allow_mixed_register=true`인 화자는 강제 교정보다 Review Hint로 처리한다.

## 11. XML 병합 및 패치 검증

### 11.1 Step3 Merge Validation

Step3는 병합 후 다음 조건을 검증한다.

- 원본 XML 노드 수와 병합 후 XML 노드 수가 동일함
- 번역 대상 노드 수가 번역 삽입 노드 수와 intentionally skipped 수의 합과 동일함
- 각 `stable_id`의 `source_hash`가 병합 시점에도 동일함
- 태그, 변수, 엔티티가 원문과 동일하게 보존됨
- 중복 원문은 `context_id` 없이 단순 매칭하지 않음
- `translation_class`가 `COPY_AS_IS`, `SKIP_INTERNAL`, `LOCKED_TERM`인 항목은 의도치 않게 LLM 번역으로 덮어쓰지 않음

검증 실패 시 Step3는 final XML을 생성하지 않고 오류 리포트를 남긴다.

### 11.2 Step7 Verify

Step7은 빌드와 검증을 분리한다.

```text
step7_build
-> step7_verify
```

`step7_verify` 검증 항목:

- 생성된 Strings 파일 row count 확인
- 원본 FormID 매핑 유지 확인
- 빈 문자열과 깨진 인코딩 확인
- 게임 적용 전 dry-run import 확인
- 백업 생성 확인
- xTranslator 재오픈 가능 여부 확인

## 12. 산출물

입력 파일이 `mod.esm`일 때 주요 산출물은 다음과 같다.

- `mod.step0.extracted.xml`
- `mod.step1.dump.json`
- `mod.step1.priority.json`
- `mod.audio.tone_profiles.json`
- `mod.step2.translated.json`
- `mod.step2.profile.json`
- `mod.step2.reviewed.json`
- `mod.step2.scan.json`
- `mod.step2.chunk_log.json`
- `mod.step2.risk_report.json`
- `mod.step2.tone_refined.json`
- `mod.step3.merged.xml`
- `mod.step3.merge_report.json`
- `mod.step4.translated.xml`
- `mod.step5.reviewed.xml`
- `mod.step5.scan.json`
- `mod.step6.refined.xml`
- `mod.final.xml`
- `mod_Translated/`
- `mod.step7.verify.json`
- `mod.pipeline_manifest.json`
- `risk_report.html`
- `tone_diff.html`
- `untranslated_report.html`
- `tag_damage_report.html`
- `cost_report.json`

## 13. 설정 요구사항

필수 또는 주요 설정:

- `api_provider`
- `provider`
- `models.audio_profile`
- `models.translation`
- `models.review`
- `model_name`
- `gcp_project_id`
- `gcp_location`
- `gcp_key_json`
- `orchestrator.enabled`
- `orchestrator.mode`
- `step2_chunk_size`
- `step2_max_chunk_chars`
- `step2_max_input_tokens`
- `step2_max_output_tokens`
- `auto_audio_analysis`
- `pipeline_mode`
- `use_ja_ref`
- `dry_run`
- `glossary_path`
- `official_ko_glossary_path`
- `existing_translation_path`

환경변수는 config보다 우선한다.

- `GCP_PROJECT_ID`
- `GCP_LOCATION`
- `GCP_KEY_JSON`
- `GOOGLE_APPLICATION_CREDENTIALS`
- `GEMINI_API_KEY`
- `OPENAI_API_KEY`
- `MIN1AI_API_KEY`

## 14. 오류 처리 요구사항

### 14.1 즉시 중단

다음 오류는 청크 분할이나 재시도로 해결되지 않으므로 즉시 중단한다.

- IAM permission denied
- `aiplatform.endpoints.predict` 권한 없음
- credential 파일 없음/손상
- DefaultCredential 오류
- manifest 무결성 불일치
- Step3 stable_id/source_hash 병합 불일치

### 14.2 분할 재시도

다음 오류는 청크 크기 또는 출력 크기 문제로 보고 분할 재시도한다.

- `MAX_TOKENS`
- 부분 번역 누락
- JSON 구조 불완전

### 14.3 비용 보호

- 불필요한 orchestration 호출을 피한다.
- GUI 중지 시 자식 프로세스까지 종료한다.
- 진행 중 `.progress.json`을 저장해 재실행 비용을 줄인다.
- LLM 캐시는 `source_hash + context_hash + glossary_hash + prompt_version + model_name` 기준으로 사용한다.

## 15. 성공 지표

### 기능 지표

- Scene 모드와 Direct XML 모드 모두 end-to-end 성공
- manifest 무결성 검증을 통과한 중단 후 resume 성공
- Step2 JSON에서 `Speaker`와 `Translate` 보존
- Step3 이후 `stable_id` 기반 XML 병합 성공
- Step7 선택 시 패치 산출물 생성과 verify 성공

### 품질 지표

- 미번역 문자열 비율 감소
- 태그/변수 손상률 0에 근접
- 번역 금지 문자열 오번역률 0에 근접
- NPC 말투 혼용 사례 감소
- Player 선택지는 과도하게 고정된 말투로 교정되지 않음

### 비용/성능 지표

- high_quality 모드에서도 대부분 청크는 단일 모델 1회 호출로 처리
- orchestration 호출은 위험 청크에 한정
- 대형 모드에서 `MAX_TOKENS -> orchestration -> MAX_TOKENS` 반복 방지
- 재실행 시 완료 산출물과 progress 파일을 활용해 중복 호출 최소화

## 16. 비기능 요구사항

- Python 3.11+에서 실행
- CLI, GUI, auto pipeline이 동일한 산출물 규칙 사용
- 모든 Step은 성공 시 `[OK] output=<path>` 출력
- 모든 Step은 명시된 입출력 schema를 유지
- 종료 코드는 `pipeline_runner.py`의 공통 계약을 따른다.
- 네트워크/API 실패가 전체 파일 손상을 유발하지 않아야 한다.

## 17. 우선순위

### P0

- `stable_id`, `source_hash`, `context_id` 명세
- Step별 JSON schema
- Step3 XML 병합 검증 조건
- manifest에 config/model/prompt/input hash 저장
- Preflight Check
- 번역 금지 문자열 분류

### P1

- 토큰 추정 기반 adaptive chunking
- glossary / locked term
- risk severity 분류: Fatal / Quality / Hint
- `step7_verify`
- dry-run 비용/청크 예측
- tone profile confidence

### P2

- speaker consistency score
- `scene_refine` diff report
- HTML 리뷰 리포트
- 캐시 재사용 정책
- GUI에서 위험 청크만 재실행
- 기존 번역/공식 용어 참조 레이어

## 18. 향후 개선

- speaker consistency score 정밀화
- GUI에서 위험 청크만 재실행하는 UX 추가
- GUI에서 `step2_max_chunk_chars`, `orchestrator.mode` 직접 조정
- LLM 공급자별 모델 지원 여부 사전 검사
- Node/Electron 앱과 Python CLI 산출물 계약 동기화
