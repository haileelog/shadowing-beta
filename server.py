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
def pick_focus(words):
    ranked=[]
    for w in words or []:
        pa=w.get('PronunciationAssessment') or {}; score=pa.get('AccuracyScore'); word=w.get('Word') or w.get('Display') or ''; err=pa.get('ErrorType','None')
        if word and score is not None and (float(score)<85 or err not in ('None','')): ranked.append((float(score),word))
    ranked.sort(key=lambda x:x[0]); return [w for _,w in ranked[:4]]

def build_feedback(azure,previous):
    n=(azure.get('NBest') or [{}])[0]; pa=n.get('PronunciationAssessment') or {}; words=n.get('Words') or []
    acc=float(pa.get('AccuracyScore',0)); flu=float(pa.get('FluencyScore',0)); comp=float(pa.get('CompletenessScore',0)); pros=float(pa.get('ProsodyScore',flu))
    m={'clarity':round(clamp((acc*.45+flu*.25+comp*.30)/10),1),'pronunciation':round(clamp(acc/10),1),'stress':round(clamp(pros/10),1),'rhythm':round(clamp((flu*.55+pros*.45)/10),1),'connection':round(clamp((flu*.55+pros*.25+comp*.20)/10),1)}
    overall=round(sum(m.values())/5,1); focus=pick_focus(words); phrase=', '.join(focus[:3]) if focus else '문장 전체의 리듬'
    labels={'clarity':'명료도','pronunciation':'개별 발음 정확도','stress':'강세','rhythm':'리듬','connection':'자연스러운 연결'}
    if previous:
        pm=previous.get('metrics') or {}; d={k:round(m[k]-float(pm.get(k,m[k])),1) for k in m}; best=max(d,key=d.get); worst=min(d,key=d.get)
        good=f'직전 시도보다 {labels[best]} 항목이 {d[best]:+.1f}점 변했습니다. 특히 {phrase} 부분의 변화를 확인해 보세요.'
        weak=f'{labels[worst]} 항목은 직전 시도 대비 {d[worst]:+.1f}점입니다. {phrase} 부분은 아직 더 안정적으로 만들 여지가 있습니다.'
        tip=f'다음 시도에서는 {phrase}를 한 덩어리로 천천히 한 번 말한 뒤, 문장 전체 속도로 다시 연결해 보세요.'
    else:
        good=f'문장 완성도 {comp:.0f}, 유창성 {flu:.0f}, 발음 정확도 {acc:.0f}를 기준으로 전체 수행을 분석했습니다.'
        weak=f'가장 먼저 확인할 부분은 {phrase}입니다. 단어별 정확도와 오류 유형을 기준으로 선택했습니다.'
        tip=f'{phrase}를 짧게 반복한 뒤 원문 전체에서 같은 강세와 리듬을 유지해 보세요.'
    return m,overall,good,weak,tip,focus

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
        # Replace PA completeness with an independent coverage score to avoid false 422s
        # caused by reference-alignment quirks.
        pa['CompletenessScore']=round(coverage*100,1)
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
    pool=[s for s in SENTENCES if s['level']==level and (not topic or s['topic']==topic) and s['id'] not in exclude_ids]
    if not pool: pool=[s for s in SENTENCES if s['level']==level and (not topic or s['topic']==topic)] or [s for s in SENTENCES if s['level']==level] or SENTENCES
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
    def translate_path(self,path):
        raw=super().translate_path(path); rel=os.path.relpath(raw,os.getcwd()); return str(ROOT/rel)
    def do_GET(self):
        u=urlparse(self.path)
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

            # Strong omission/truncation gate. Keep it conservative to avoid rejecting accented but complete reads.
            if coverage < 0.48 and length_ratio < 0.72:
                log('NO-COMMIT',{'requestId':request_id,'attempt':attempt,'reason':'incomplete-transcript','coverage':round(coverage,3),'transcript':plain_text})
                return self.j(422,{'error':'기준 문장의 상당 부분이 누락되었거나 문장이 끝까지 읽히지 않았습니다. 이번 시도는 채점하지 않았습니다.','recognizedText':plain_text,'coverage':round(coverage*100,1)})

            nbest=(azure.get('NBest') or [{}])[0]; pa=nbest.get('PronunciationAssessment') or {}
            m,o,g,w,t,f=build_feedback(azure,body.get('previous'))
            warning=None
            if coverage < .8:
                warning=f'문장 완성도가 약 {coverage*100:.0f}%로 인식되었습니다. 일부 단어가 빠졌거나 다르게 인식되었을 수 있어요.'
            log('ASSESS-SUCCESS',{'requestId':request_id,'attempt':attempt,'overall':o,'coverage':round(coverage,3),'transcript':plain_text})
            log('COMMIT-DEFERRED',{'requestId':request_id,'attempt':attempt,'reason':'DB not connected yet; browser keeps successful attempt only'})
            return self.j(200,{'metrics':m,'overall':o,'good':g,'weak':w,'tip':t,'focusTerms':f,'warning':warning,'azure':{'accuracy':pa.get('AccuracyScore'),'fluency':pa.get('FluencyScore'),'completeness':round(coverage*100,1),'prosody':pa.get('ProsodyScore'),'recognizedText':plain_text}})
        except Exception as e:
            log('NO-COMMIT',{'requestId':request_id,'reason':'assessment-error','error':str(e)})
            return self.j(500,{'error':f'발음 평가 처리 중 오류가 발생했습니다: {e}','requestId':request_id})
    def j(self,status,obj):
        data=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(data))); self.end_headers();
        if data: self.wfile.write(data)

if __name__=='__main__':
    os.chdir(ROOT); port=int(os.getenv('PORT','8000')); host=os.getenv('HOST','0.0.0.0')
    print(f'Shadowing Lab v1.3.5 → http://localhost:{port}', flush=True)
    print('Azure:', 'READY' if AZURE_KEY and AZURE_REGION else 'PREVIEW MODE', '/ SDK:', 'READY' if speechsdk else 'MISSING', flush=True)
    print('ElevenLabs:', 'READY' if ELEVEN_KEY and (VOICE_IDS['female'] or VOICE_IDS['male']) else 'BROWSER FALLBACK', flush=True)
    ThreadingHTTPServer((host,port),H).serve_forever()
