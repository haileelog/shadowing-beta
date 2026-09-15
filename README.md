# Shadowing Lab v1.3.5

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
