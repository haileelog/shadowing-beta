# Shadowing Lab v1.3.2 — public beta candidate

이 버전은 GitHub → Render 배포를 바로 하기 위한 베타 후보입니다.

## 지금 동작하는 기능
- HTTPS/localhost 마이크 권한 + 녹음/재생/다운로드
- 최초 평가 + 최대 3회 재녹음
- 이전 평가 탭 다시 보기
- 명료도 / 개별 발음 정확도 / 강세 / 리듬 / 자연스러운 연결 5개 점수
- 파스텔 점수대별 fill bar
- 무음, 짧은 녹음, 너무 작은 음량, clipping, 문장 길이에 비해 비정상적으로 짧은 녹음 사전 차단
- Azure 연결 시 Pronunciation Assessment 실제 채점, 미연결 시 명확한 UI preview
- 초기화 커스텀 모달 + 최대 3회 + 실제 새 문장 교체
- 10개 주제 × 4레벨의 seed 문장 JSON 구조 (40개)
- 문장별 focus / useful expression / focus note 메타데이터
- Female Natural / Male Natural TTS 선택
- ElevenLabs 연결 시 서버 TTS 생성 + 파일 캐시 재사용
- ElevenLabs 미연결 시 브라우저 TTS fallback
- 동일 TTS 텍스트/voice/model은 서버 캐시에서 재사용
- normal 0.95 / slow 0.82 playbackRate
- 피드백 강조 단어나 구 클릭 시 발음 재생

## 로컬 실행
```bash
python3 server.py
```
열기: http://localhost:8000

## Azure 실제 평가 연결
```bash
export AZURE_SPEECH_KEY="..."
export AZURE_SPEECH_REGION="koreacentral"
python3 server.py
```
선택:
```bash
export AZURE_SPEECH_RESOURCE="resource-name"
```

## ElevenLabs TTS 연결
ElevenLabs에서 사용할 여성/남성 voice ID를 고른 뒤:
```bash
export ELEVENLABS_API_KEY="..."
export ELEVENLABS_FEMALE_VOICE_ID="..."
export ELEVENLABS_MALE_VOICE_ID="..."
export ELEVENLABS_MODEL_ID="eleven_flash_v2_5"
python3 server.py
```
`/api/tts`는 `mp3_44100_128`을 생성하고 `_cache/tts`에 저장합니다. 같은 text + voice + model 조합은 다시 API를 호출하지 않습니다.

## Render 배포
이 폴더의 파일을 GitHub repository 루트에 올린 뒤 Render Web Service를 연결합니다.
- Language: Python
- Build command: 비워두기
- Start command: `python3 server.py`
- `server.py`는 `0.0.0.0`과 Render의 `PORT` 환경변수를 자동 사용합니다.
- Azure/ElevenLabs 키는 GitHub에 넣지 말고 Render의 Environment Variables에만 넣으세요.

`render.yaml`을 쓰면 기본 Web Service 설정도 자동 인식할 수 있습니다.

## 아직 다음 단계인 것
- 회원가입/Auth/DB/Supabase
- 사용자별 seen sentence history 영구 저장
- 하루 초기화 횟수 서버 강제
- 평가 성공 후에만 audio를 permanent Storage에 commit하는 transaction
- OpenAI 기반의 더 정교한 자연어 비교 피드백
- 레벨 판독기 / 온보딩 / 결제 / 마이페이지

현재 브라우저 메모리의 초기화·시도 횟수 제한은 UI 베타용입니다. 공개 유료 서비스에서는 반드시 서버에서 다시 검증해야 합니다.
