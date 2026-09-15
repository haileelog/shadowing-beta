# Shadowing Lab v1.3.9

Core beta build with Azure Pronunciation Assessment via the official Azure Speech SDK.

## Render settings

Build Command:

```bash
pip install -r requirements.txt
```

Start Command:

```bash
python3 server.py
```

Environment variables:

- `AZURE_SPEECH_KEY`
- `AZURE_SPEECH_REGION`
- `ELEVENLABS_API_KEY` (optional)
- `ELEVENLABS_FEMALE_VOICE_ID` (optional)
- `ELEVENLABS_MALE_VOICE_ID` (optional)

## Assessment diagnostics

Render logs use these tags:

- `[AZURE-ASSESS]`: transcript and Azure score diagnostics
- `[ASSESS-SUCCESS]`: valid assessment completed
- `[NO-COMMIT]`: failed/rejected recording; must not be persisted
- `[COMMIT-DEFERRED]`: successful attempt kept in browser only until Supabase is connected
- `[CLIENT-LOG]`: client-side preflight failure such as silence/too-short audio

The beta currently has no persistent database. Successful attempt persistence will be connected to Supabase later.


## Category mapping
- UI에는 학습자의 레벨 pill만 노출합니다.
- 문장 데이터는 `categories` 배열과 `primary_category`를 유지해 문장-카테고리 다대다 매핑으로 확장할 수 있습니다.
- 향후 Supabase에서는 `sentences`, `categories`, `sentence_categories` 조인 테이블 구조를 권장합니다.
- 사용자 관심사 변경과 무관하게 과거 세션에는 사용 당시 sentence_id를 저장해 카테고리 분석/도전율/재녹음률 통계를 낼 수 있게 합니다.


## v1.3.9
- Lock recording controls after a successful assessment until the next retry button is pressed.
- Keep failed attempts uncommitted and show a clear in-session “미채점” notice.
- Compare retry feedback against the first successful attempt, not the immediately previous attempt.
- Use stricter score calibration with word/phoneme lower-percentile penalties and break/prosody errors.
- Generate richer multi-item Strengths / Needs Work / Feedback from Azure word and phoneme diagnostics.
- Update reset confirmation copy and retry button emoji labels.
