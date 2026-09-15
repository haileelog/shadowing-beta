#!/usr/bin/env python3
import os, json, base64, urllib.request, urllib.error, hashlib, random, re, tempfile, uuid
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    import azure.cognitiveservices.speech as speechsdk
except Exception:
    speechsdk = None

ROOT=Path(__file__).resolve().parent
APP_VERSION='1.4.0'
CACHE=ROOT/'_cache'/'tts'; CACHE.mkdir(parents=True,exist_ok=True)
SENTENCES=json.loads((ROOT/'sentences.json').read_text(encoding='utf-8'))
AZURE_KEY=os.getenv('AZURE_SPEECH_KEY','').strip(); AZURE_REGION=os.getenv('AZURE_SPEECH_REGION','').strip()
ELEVEN_KEY=os.getenv('ELEVENLABS_API_KEY','').strip(); ELEVEN_MODEL=os.getenv('ELEVENLABS_MODEL_ID','eleven_flash_v2_5').strip()
VOICE_IDS={'female':os.getenv('ELEVENLABS_FEMALE_VOICE_ID','').strip(),'male':os.getenv('ELEVENLABS_MALE_VOICE_ID','').strip()}

def log(tag, payload):
    try:
        print(f'[{tag}] {json.dumps(payload, ensure_ascii=False)}', flush=True)
    except Exception:
        print(f'[{tag}] {payload}', flush=True)

def clamp(v,a=0,b=10): return max(a,min(b,float(v)))

def pct(values, q, default=100.0):
    vals=sorted(float(v) for v in values if v is not None)
    if not vals: return float(default)
    idx=max(0,min(len(vals)-1,round((len(vals)-1)*q)))
    return vals[idx]

def strict_scale(score):
    """Map Azure 0-100 to a deliberately strict learner-facing 1-10 scale.
    Scores above 9 should be rare and reserved for genuinely polished delivery."""
    x=max(0.0,min(100.0,float(score or 0)))
    anchors=[(0,1.0),(50,3.0),(60,4.1),(70,5.2),(80,6.4),(85,7.0),(90,7.7),(95,8.6),(98,9.2),(100,9.7)]
    for (x0,y0),(x1,y1) in zip(anchors,anchors[1:]):
        if x<=x1:
            t=(x-x0)/(x1-x0) if x1>x0 else 0
            return round(max(1.0,min(10.0,y0+(y1-y0)*t)),2)
    return 10.0

def sentence_meta_for(ref):
    norm=' '.join(normalize_tokens(ref))
    for item in SENTENCES:
        if ' '.join(normalize_tokens(item.get('text','')))==norm:
            return item
    return {}

def word_diagnostics(azure):
    n=(azure.get('NBest') or [{}])[0]
    words=n.get('Words') or []
    out=[]
    for w in words:
        pa=w.get('PronunciationAssessment') or {}
        word=w.get('Word') or w.get('Display') or ''
        if not word: continue
        phonemes=[]
        for ph in (w.get('Phonemes') or []):
            ppa=ph.get('PronunciationAssessment') or {}
            ph_text=ph.get('Phoneme') or ph.get('Display') or ''
            if ph_text:
                phonemes.append({'phoneme':ph_text,'accuracy':float(ppa.get('AccuracyScore',100) or 100)})
        out.append({
            'word':word,
            'accuracy':float(pa.get('AccuracyScore',100) or 100),
            'error':pa.get('ErrorType','None') or 'None',
            'phonemes':phonemes,
        })
    return out

def phrase_score(phrase, word_rows):
    toks=normalize_tokens(phrase)
    if not toks: return None
    scores=[]
    rows=[(normalize_tokens(r['word']) or [''])[0] for r in word_rows]
    for tok in toks:
        candidates=[r['accuracy'] for r,rt in zip(word_rows,rows) if rt==tok]
        if candidates: scores.append(min(candidates))
    return sum(scores)/len(scores) if scores else None

def build_feedback(azure, baseline, ref):
    n=(azure.get('NBest') or [{}])[0]
    pa=n.get('PronunciationAssessment') or {}
    words=word_diagnostics(azure)
    acc=float(pa.get('AccuracyScore',0) or 0)
    flu=float(pa.get('FluencyScore',0) or 0)
    comp=float(pa.get('CompletenessScore',0) or 0)
    pros=float(pa.get('ProsodyScore',flu) or flu)
    word_scores=[w['accuracy'] for w in words]
    phoneme_scores=[ph['accuracy'] for w in words for ph in w['phonemes']]
    word_p25=pct(word_scores,.25,acc)
    word_p50=pct(word_scores,.50,acc)
    phoneme_p20=pct(phoneme_scores,.20,word_p25)
    phoneme_p35=pct(phoneme_scores,.35,word_p50)
    phoneme_p50=pct(phoneme_scores,.50,word_p50)
    break_errors=[w for w in words if w['error'] in ('UnexpectedBreak','MissingBreak','Monotone')]
    mispronounced=[w for w in words if w['error']=='Mispronunciation' or w['accuracy']<78]

    raw={
        # Keep articulation separate from fluency: a learner who reads slowly word-by-word
        # can still pronounce individual sounds accurately even if rhythm is weak.
        # Individual pronunciation should stay independent from reading speed, rhythm, and prosody.
        # Use median word/phoneme articulation only, so slow but clearly pronounced speech can still score well here.
        'pronunciation': word_p50*.55 + phoneme_p50*.45,
        'clarity': acc*.38 + word_p50*.30 + comp*.20 + phoneme_p35*.12,
        'stress': pros*.78 + acc*.12 + flu*.10,
        'rhythm': flu*.52 + pros*.48,
        'connection': flu*.50 + pros*.25 + word_p25*.15 + comp*.10,
    }
    # Real break / monotone errors should visibly matter.
    penalty=min(12.0,len(break_errors)*3.0)
    raw['stress']-=penalty*.75
    raw['rhythm']-=penalty
    raw['connection']-=penalty
    metrics={k:round(strict_scale(v),1) for k,v in raw.items()}
    ordered=sorted(metrics.values())
    # Similar to Microsoft's weakness-sensitive PronScore philosophy: the weakest area matters most.
    overall=round(ordered[0]*.40 + ordered[1]*.20 + ordered[2]*.15 + ordered[3]*.15 + ordered[4]*.10,1)

    meta=sentence_meta_for(ref)
    focus_items=meta.get('focus') or []
    focus_rank=[]
    for phrase in focus_items:
        ps=phrase_score(phrase,words)
        if ps is not None: focus_rank.append((ps,phrase))
    focus_rank.sort(key=lambda x:x[0])

    weak_words=sorted(words,key=lambda w:w['accuracy'])[:4]
    weak_terms=[]
    for w in weak_words:
        if w['accuracy']<90 or w['error']!='None': weak_terms.append(w['word'])
    for _,phrase in focus_rank[:2]:
        if phrase not in weak_terms: weak_terms.append(phrase)
    weak_terms=weak_terms[:5]

    strongest=sorted(words,key=lambda w:w['accuracy'],reverse=True)[:3]
    strengths=[]; needs=[]; tips=[]
    if comp>=95: strengths.append('문장을 빠뜨리지 않고 끝까지 읽어서 전체 완성도가 좋았어요.')
    if flu>=88: strengths.append('말의 흐름이 크게 끊기지 않고 자연스럽게 이어졌어요.')
    if pros>=88: strengths.append('강세와 억양의 변화가 비교적 자연스럽게 들렸어요.')
    if strongest:
        names=', '.join(w['word'] for w in strongest[:2])
        strengths.append(f'{names} 같은 단어는 비교적 또렷하고 안정적으로 발음됐어요.')
    if not strengths: strengths.append('문장을 끝까지 읽은 점은 좋아요. 이제 세부 발음과 리듬을 조금씩 다듬어 볼 수 있어요.')

    # Aggregate similar word-level issues instead of repeating the same sentence for every word.
    weak_sound_words=[]
    unstable_words=[]
    for w in weak_words[:4]:
        if w['accuracy']>=90 and w['error']=='None':
            continue
        low_ph=sorted(w['phonemes'],key=lambda x:x['accuracy'])[:2]
        has_low_sound=any(x['accuracy']<88 for x in low_ph)
        (weak_sound_words if has_low_sound else unstable_words).append(w['word'])
    if weak_sound_words:
        names=', '.join(weak_sound_words[:4])
        needs.append(f'{names}에서는 몇몇 자음·모음이 목표 발음보다 덜 분명하게 들렸어요. 단어의 첫소리와 끝소리를 조금 더 선명하게 살려 보면 좋아요.')
    elif unstable_words:
        names=', '.join(unstable_words[:4])
        needs.append(f'{names}는 다른 단어보다 발음이 조금 덜 안정적으로 들렸어요. 사전 발음을 한 번 확인한 뒤 같은 입모양과 소리 길이로 다시 말해 보세요.')

    if break_errors or flu<82:
        needs.append('단어 하나씩 따로 읽는 느낌이 조금 강해 문장 흐름이 끊겨 들렸어요. 의미가 이어지는 부분은 한 호흡으로 묶어 읽어 보면 더 자연스러워요.')
    elif flu<88:
        needs.append('몇몇 단어 사이의 속도 차이가 커서 문장 흐름이 살짝 끊겨 들렸어요. 앞뒤 단어를 조금 더 부드럽게 이어 보세요.')
    if focus_rank and focus_rank[0][0]<84:
        needs.append(f'{focus_rank[0][1]} 부분은 단어를 하나씩 분리하기보다 한 덩어리처럼 이어 읽으면 더 자연스러워요.')
    if pros<84:
        needs.append('강하게 읽을 단어와 가볍게 지나갈 단어의 차이가 아직 작아요. 핵심 단어만 살짝 강조하고 나머지는 힘을 빼 보세요.')
    elif pros<90:
        needs.append('억양 변화가 조금 더 살아나면 훨씬 자연스럽게 들릴 수 있어요. 문장 안에서 가장 중요한 단어 한두 개만 골라 강조해 보세요.')
    needs=needs[:4] or ['큰 오류는 많지 않았어요. 다음 시도에서는 발음의 선명함과 문장 리듬을 한 단계 더 다듬어 보세요.']

    target = weak_terms[0] if weak_terms else (focus_items[0] if focus_items else '문장 전체')
    if target!='문장 전체':
        tips.append(f'{target}를 2~3번 천천히 또렷하게 읽은 뒤, 같은 소리를 유지한 채 문장 속에 다시 넣어 말해 보세요.')
    if break_errors or flu<88:
        tips.append('단어 하나씩 끊기보다 의미가 이어지는 부분을 묶어 읽고, 쉼표나 문장 끝에서만 자연스럽게 쉬어 보세요.')
    if pros<88:
        tips.append('핵심 단어에는 힘을 조금 주고, 관사·전치사·대명사는 짧고 가볍게 지나가면 문장 리듬이 더 살아나요.')
    if weak_words:
        w=weak_words[0]
        tips.append(f'{w["word"]}를 눌러 기준 발음을 들은 뒤, 자신의 녹음과 비교하면서 첫소리·끝소리와 소리 길이를 맞춰 보세요.')
    tips=tips[:4]

    baseline_delta=None
    if baseline and baseline.get('metrics'):
        bm=baseline['metrics']
        delta={k:round(metrics[k]-float(bm.get(k,metrics[k])),1) for k in metrics}
        baseline_delta=delta
        improved=sorted(delta.items(),key=lambda kv:kv[1],reverse=True)
        if improved and improved[0][1]>0:
            label={'clarity':'명료도','pronunciation':'개별 발음 정확도','stress':'강세','rhythm':'리듬','connection':'자연스러운 연결'}[improved[0][0]]
            strengths.insert(0,f'첫 시도보다 {subject_form(label)} {improved[0][1]:+.1f}점 좋아졌어요.')
        worsened=sorted(delta.items(),key=lambda kv:kv[1])
        if worsened and worsened[0][1]<0:
            label={'clarity':'명료도','pronunciation':'개별 발음 정확도','stress':'강세','rhythm':'리듬','connection':'자연스러운 연결'}[worsened[0][0]]
            needs.insert(0,f'첫 시도와 비교하면 {subject_form(label)} {abs(worsened[0][1]):.1f}점 낮아졌어요. 이번에는 이 부분을 조금 더 천천히 다듬어 보세요.')

    details={
        'azure':{'accuracy':acc,'fluency':flu,'completeness':comp,'prosody':pros},
        'wordP25':round(word_p25,1),'wordP50':round(word_p50,1),'phonemeP20':round(phoneme_p20,1),'phonemeP35':round(phoneme_p35,1),'phonemeP50':round(phoneme_p50,1),
        'weakWords':weak_words,'breakErrors':break_errors,'baselineDelta':baseline_delta,
        'focusScores':[{'text':p,'score':round(s,1)} for s,p in focus_rank]
    }
    return metrics,overall,strengths[:4],needs[:4],tips[:4],weak_terms,details


def subject_form(label):
    """Attach the natural Korean subject particle (이/가) to a Korean label."""
    for ch in reversed(label):
        if '가' <= ch <= '힣':
            jong=(ord(ch)-0xAC00)%28
            return label + ('이' if jong else '가')
    return label + '가'

def preview_feedback(previous):
    base={'clarity':7.3,'pronunciation':7.1,'stress':7.2,'rhythm':7.0,'connection':7.2}
    m={k:round(min(9.2,float(previous['metrics'].get(k,base[k]))+(0.25 if k in ('clarity','connection') else 0.15)),1) for k in base} if previous and previous.get('metrics') else base
    overall=round(sum(m.values())/5,1)
    g='현재는 Azure Speech 키가 없어 UI 미리보기 결과를 표시하고 있습니다.' if not previous else '직전 시도보다 문장 흐름과 연결이 조금 더 안정적으로 들렸다는 가정의 UI 미리보기입니다.'
    w='이 점수는 실제 녹음의 발음을 분석한 결과가 아닙니다.' if not previous else '실제 Azure 연결 전이라 정확한 오류 위치는 판독하지 않았습니다.'
    t='Azure Speech 환경변수를 설정하면 실제 발음평가 결과를 기반으로 채점합니다.'
    return m,overall,g,w,t,['therapist','talked me through','through']

def normalize_tokens(text):
    return re.findall(r"[a-z0-9']+", (text or '').lower())

def lcs_coverage(reference, hypothesis):
    a,b=normalize_tokens(reference),normalize_tokens(hypothesis)
    if not a: return 0.0,0.0,len(a),len(b)
    prev=[0]*(len(b)+1)
    for x in a:
        cur=[0]
        for j,y in enumerate(b,1):
            cur.append(prev[j-1]+1 if x==y else max(prev[j],cur[-1]))
        prev=cur
    lcs=prev[-1]
    return lcs/len(a), min(1.0,len(b)/len(a)), len(a), len(b)

def azure_sdk_recognize(audio_bytes, ref, request_id):
    if speechsdk is None:
        raise RuntimeError('Azure Speech SDK가 설치되지 않았습니다. Render Build Command를 pip install -r requirements.txt 로 바꿔 주세요.')
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        f.write(audio_bytes); wav_path=f.name
    try:
        speech_config=speechsdk.SpeechConfig(subscription=AZURE_KEY, region=AZURE_REGION)
        speech_config.speech_recognition_language='en-US'

        # 1) plain STT: sentence omission / truncation check. This is deliberately independent
        # from pronunciation assessment so that a strict reference text cannot hide missing words.
        plain_audio=speechsdk.audio.AudioConfig(filename=wav_path)
        plain_rec=speechsdk.SpeechRecognizer(speech_config=speech_config, audio_config=plain_audio)
        plain=plain_rec.recognize_once_async().get()
        plain_text=plain.text or ''
        if plain.reason==speechsdk.ResultReason.Canceled:
            c=speechsdk.CancellationDetails(plain)
            raise RuntimeError(f'Azure STT 취소: {c.reason} / {c.error_details or "no detail"}')

        # 2) pronunciation assessment: use stable scripted alignment. Missing-word validation
        # is handled by step 1 rather than letting miscue alignment reject a valid full read.
        pa_audio=speechsdk.audio.AudioConfig(filename=wav_path)
        recognizer=speechsdk.SpeechRecognizer(speech_config=speech_config, audio_config=pa_audio)
        pc=speechsdk.PronunciationAssessmentConfig(
            reference_text=ref,
            grading_system=speechsdk.PronunciationAssessmentGradingSystem.HundredMark,
            granularity=speechsdk.PronunciationAssessmentGranularity.Phoneme,
            enable_miscue=False
        )
        try: pc.enable_prosody_assessment()
        except Exception: pass
        pc.apply_to(recognizer)
        result=recognizer.recognize_once_async().get()
        if result.reason==speechsdk.ResultReason.Canceled:
            c=speechsdk.CancellationDetails(result)
            raise RuntimeError(f'Azure Pronunciation Assessment 취소: {c.reason} / {c.error_details or "no detail"}')
        if result.reason!=speechsdk.ResultReason.RecognizedSpeech:
            raise RuntimeError(f'Azure가 음성을 인식하지 못했습니다. reason={result.reason}')
        raw=result.properties.get(speechsdk.PropertyId.SpeechServiceResponse_JsonResult)
        if not raw: raise RuntimeError('Azure가 발음평가 JSON을 반환하지 않았습니다.')
        azure=json.loads(raw)
        n=(azure.get('NBest') or [{}])[0]; pa=n.get('PronunciationAssessment') or {}
        if not pa: raise RuntimeError('Azure가 음성은 인식했지만 PronunciationAssessment 점수를 반환하지 않았습니다.')

        coverage,length_ratio,ref_count,hyp_count=lcs_coverage(ref,plain_text)
        # Keep Azure Pronunciation Assessment completeness as the learner-facing signal.
        # Plain STT coverage is only a diagnostic hint because strong non-native accents
        # can cause transcription mismatches even when the learner read every word.
        n['PronunciationAssessment']=pa
        log('AZURE-ASSESS',{
            'requestId':request_id,'plainTranscript':plain_text,'assessmentDisplay':n.get('Display') or azure.get('DisplayText',''),
            'coverage':round(coverage,3),'lengthRatio':round(length_ratio,3),'refWords':ref_count,'recognizedWords':hyp_count,
            'accuracy':pa.get('AccuracyScore'),'fluency':pa.get('FluencyScore'),'prosody':pa.get('ProsodyScore')
        })
        return azure, plain_text, coverage, length_ratio
    finally:
        try: os.unlink(wav_path)
        except Exception: pass

def choose_sentence(level='intermediate', topic='', exclude_ids=None):
    exclude_ids=set(exclude_ids or [])
    pool=[s for s in SENTENCES if s['level']==level and (not topic or topic in (s.get('categories') or [s.get('topic')])) and s['id'] not in exclude_ids]
    if not pool: pool=[s for s in SENTENCES if s['level']==level and (not topic or topic in (s.get('categories') or [s.get('topic')]))] or [s for s in SENTENCES if s['level']==level] or SENTENCES
    return random.choice(pool)

def eleven_tts(text, voice):
    vid=VOICE_IDS.get(voice) or VOICE_IDS.get('female')
    if not ELEVEN_KEY or not vid: raise RuntimeError('ElevenLabs API key/voice ID not configured')
    key=hashlib.sha256(f'{ELEVEN_MODEL}|{vid}|{text}'.encode()).hexdigest(); path=CACHE/f'{key}.mp3'
    if path.exists(): return path.read_bytes(), True
    url=f'https://api.elevenlabs.io/v1/text-to-speech/{vid}?output_format=mp3_44100_128'
    payload=json.dumps({'text':text,'model_id':ELEVEN_MODEL,'voice_settings':{'stability':0.50,'similarity_boost':0.78,'style':0.10,'use_speaker_boost':True}}).encode()
    req=urllib.request.Request(url,data=payload,method='POST',headers={'xi-api-key':ELEVEN_KEY,'Content-Type':'application/json','Accept':'audio/mpeg'})
    with urllib.request.urlopen(req,timeout=30) as r: audio=r.read()
    path.write_bytes(audio); return audio, False

class H(SimpleHTTPRequestHandler):
    def end_headers(self):
        # Beta builds change frequently. Prevent browsers/proxies from keeping stale HTML/JS/CSS.
        self.send_header('Cache-Control','no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma','no-cache')
        self.send_header('Expires','0')
        self.send_header('X-Shadowing-Version',APP_VERSION)
        super().end_headers()
    def translate_path(self,path):
        raw=super().translate_path(path); rel=os.path.relpath(raw,os.getcwd()); return str(ROOT/rel)
    def do_GET(self):
        u=urlparse(self.path)
        if u.path=='/api/version':
            return self.j(200,{'version':APP_VERSION})
        if u.path=='/api/config':
            return self.j(200,{'azureReady':bool(AZURE_KEY and AZURE_REGION),'azureSdkReady':speechsdk is not None,'elevenLabsReady':bool(ELEVEN_KEY and (VOICE_IDS['female'] or VOICE_IDS['male'])),'voices':{'female':bool(VOICE_IDS['female']),'male':bool(VOICE_IDS['male'])},'ttsModel':ELEVEN_MODEL})
        if u.path=='/api/sentence':
            q=parse_qs(u.query); level=(q.get('level') or ['intermediate'])[0]; topic=(q.get('topic') or [''])[0]
            try: exclude=[int(x) for x in (q.get('exclude') or [''])[0].split(',') if x.strip()]
            except: exclude=[]
            return self.j(200,choose_sentence(level,topic,exclude))
        if u.path=='/api/tts':
            q=parse_qs(u.query); text=(q.get('text') or [''])[0].strip(); voice=(q.get('voice') or ['female'])[0]
            if not text: return self.j(400,{'error':'text is required'})
            if len(text)>600: return self.j(400,{'error':'text too long'})
            try:
                audio,cached=eleven_tts(text,voice)
                self.send_response(200); self.send_header('Content-Type','audio/mpeg'); self.send_header('Content-Length',str(len(audio))); self.send_header('X-TTS-Cache','HIT' if cached else 'MISS'); self.end_headers(); self.wfile.write(audio); return
            except RuntimeError as e: return self.j(503,{'error':str(e),'fallback':'browser'})
            except urllib.error.HTTPError as e:
                detail=e.read().decode(errors='ignore'); return self.j(502,{'error':f'ElevenLabs 요청 실패 ({e.code}) {detail[:180]}'})
            except Exception as e: return self.j(500,{'error':f'TTS 처리 오류: {e}'})
        return super().do_GET()
    def do_POST(self):
        u=urlparse(self.path)
        if u.path=='/api/log':
            try:
                n=int(self.headers.get('Content-Length','0')); body=json.loads(self.rfile.read(n) or b'{}')
                log('CLIENT-LOG',body); return self.j(204,{})
            except Exception: return self.j(204,{})
        if u.path!='/api/assess': self.send_error(404); return
        request_id=str(uuid.uuid4())[:8]
        try:
            n=int(self.headers.get('Content-Length','0')); body=json.loads(self.rfile.read(n) or b'{}')
            attempt=body.get('attempt')
            if not AZURE_KEY or not AZURE_REGION:
                m,o,g,w,t,f=preview_feedback(body.get('previous'))
                log('ASSESS-PREVIEW',{'requestId':request_id,'attempt':attempt})
                return self.j(200,{'metrics':m,'overall':o,'good':g,'weak':w,'tip':t,'focusTerms':f,'warning':'현재는 Azure 미연결 미리보기 모드입니다.','preview':True})
            audio=base64.b64decode(body['audioBase64']); ref=body['referenceText']
            azure,plain_text,coverage,length_ratio=azure_sdk_recognize(audio,ref,request_id)

            # Do not reject a complete read merely because plain STT struggled with an accent.
            # Client-side audio checks already reject silence / implausibly short recordings.
            # Here, transcript coverage is diagnostic only; pronunciation quality belongs in the score.
            nbest=(azure.get('NBest') or [{}])[0]; pa=nbest.get('PronunciationAssessment') or {}
            m,o,g,w,t,f,details=build_feedback(azure,body.get('baseline'),ref)
            warning=None
            if coverage < .55:
                warning='일부 단어가 또렷하게 인식되지 않았어요. 단어를 빠뜨리지 않고 끝까지 읽었는지 한 번 확인해 보세요.'
            elif coverage < .78:
                warning='몇몇 단어가 또렷하게 인식되지 않았어요. 단어를 빠뜨리지 않고 끝까지 읽었는지 한 번 확인해 보세요.'
            log('ASSESS-SUCCESS',{'requestId':request_id,'attempt':attempt,'overall':o,'metrics':m,'coverage':round(coverage,3),'transcript':plain_text,'azure':details.get('azure'),'wordP25':details.get('wordP25'),'phonemeP20':details.get('phonemeP20'),'weakWords':[{'word':x.get('word'),'accuracy':x.get('accuracy'),'error':x.get('error')} for x in details.get('weakWords',[])[:4]]})
            log('COMMIT-DEFERRED',{'requestId':request_id,'attempt':attempt,'reason':'DB not connected yet; browser keeps successful attempt only'})
            return self.j(200,{'metrics':m,'overall':o,'good':g,'weak':w,'tip':t,'focusTerms':f,'warning':warning,'details':details,'azure':{'accuracy':pa.get('AccuracyScore'),'fluency':pa.get('FluencyScore'),'completeness':pa.get('CompletenessScore'),'transcriptCoverage':round(coverage*100,1),'prosody':pa.get('ProsodyScore'),'recognizedText':plain_text}})
        except Exception as e:
            log('NO-COMMIT',{'requestId':request_id,'reason':'assessment-error','error':str(e)})
            return self.j(500,{'error':f'발음 평가 처리 중 오류가 발생했습니다: {e}','requestId':request_id})
    def j(self,status,obj):
        data=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(data))); self.end_headers();
        if data: self.wfile.write(data)

if __name__=='__main__':
    os.chdir(ROOT); port=int(os.getenv('PORT','8000')); host=os.getenv('HOST','0.0.0.0')
    print(f'Shadowing Lab v{APP_VERSION} → http://localhost:{port}', flush=True)
    print('Azure:', 'READY' if AZURE_KEY and AZURE_REGION else 'PREVIEW MODE', '/ SDK:', 'READY' if speechsdk else 'MISSING', flush=True)
    print('ElevenLabs:', 'READY' if ELEVEN_KEY and (VOICE_IDS['female'] or VOICE_IDS['male']) else 'BROWSER FALLBACK', flush=True)
    ThreadingHTTPServer((host,port),H).serve_forever()
