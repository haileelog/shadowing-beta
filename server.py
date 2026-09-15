#!/usr/bin/env python3
import os, json, base64, urllib.request, urllib.error, hashlib, random, mimetypes
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT=Path(__file__).resolve().parent
CACHE=ROOT/'_cache'/'tts'; CACHE.mkdir(parents=True,exist_ok=True)
SENTENCES=json.loads((ROOT/'sentences.json').read_text(encoding='utf-8'))
AZURE_KEY=os.getenv('AZURE_SPEECH_KEY','').strip(); AZURE_REGION=os.getenv('AZURE_SPEECH_REGION','').strip(); AZURE_RESOURCE=os.getenv('AZURE_SPEECH_RESOURCE','').strip()
ELEVEN_KEY=os.getenv('ELEVENLABS_API_KEY','').strip(); ELEVEN_MODEL=os.getenv('ELEVENLABS_MODEL_ID','eleven_flash_v2_5').strip()
VOICE_IDS={'female':os.getenv('ELEVENLABS_FEMALE_VOICE_ID','').strip(),'male':os.getenv('ELEVENLABS_MALE_VOICE_ID','').strip()}

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
    if previous:
        pm=previous.get('metrics') or {}; d={k:round(m[k]-float(pm.get(k,m[k])),1) for k in m}; best=max(d,key=d.get); worst=min(d,key=d.get)
        good=f'직전 시도보다 {best} 항목이 {d[best]:+.1f}점 변했습니다. 특히 {phrase} 부분의 변화를 확인해 보세요.'
        weak=f'{worst} 항목은 직전 시도 대비 {d[worst]:+.1f}점입니다. {phrase} 부분은 아직 더 안정적으로 만들 여지가 있습니다.'
        tip=f'다음 시도에서는 {phrase}를 한 덩어리로 천천히 한 번 말한 뒤, 문장 전체 속도로 다시 연결해 보세요.'
    else:
        good=f'완성도 {comp:.0f}, 유창성 {flu:.0f}, 발음 정확도 {acc:.0f}를 기준으로 문장 전체 수행을 분석했습니다.'
        weak=f'가장 먼저 확인할 부분은 {phrase}입니다. 단어별 정확도와 오류 유형을 기준으로 선택했습니다.'
        tip=f'{phrase}를 짧게 반복한 뒤 원문 전체에서 같은 강세와 리듬을 유지해 보세요.'
    return m,overall,good,weak,tip,focus

def preview_feedback(previous):
    base={'clarity':7.3,'pronunciation':7.1,'stress':7.2,'rhythm':7.0,'connection':7.2}
    if previous and previous.get('metrics'):
        m={k:round(min(9.2,float(previous['metrics'].get(k,base[k]))+(0.25 if k in ('clarity','connection') else 0.15)),1) for k in base}
    else: m=base
    overall=round(sum(m.values())/5,1)
    if previous:
        g='직전 시도보다 문장 흐름과 연결이 조금 더 안정적으로 들렸다는 가정의 UI 미리보기입니다.'
        w='실제 Azure 연결 전이라 정확한 오류 위치는 판독하지 않았습니다. 이 문구는 화면 테스트용입니다.'
        t='실제 분석을 연결하면 직전 시도와 단어·유창성·운율 지표를 비교해 다음 연습 포인트를 제시합니다.'
    else:
        g='현재는 Azure Speech 키가 없어 UI 미리보기 결과를 표시하고 있습니다.'
        w='이 점수는 실제 녹음의 발음을 분석한 결과가 아닙니다.'
        t='Azure Speech 환경변수를 설정하면 실제 Accuracy, Fluency, Completeness, Prosody를 기반으로 채점합니다.'
    return m,overall,g,w,t,['therapist','talked me through','through']

def choose_sentence(level='intermediate', topic='', exclude_ids=None):
    exclude_ids=set(exclude_ids or [])
    pool=[s for s in SENTENCES if s['level']==level and (not topic or s['topic']==topic) and s['id'] not in exclude_ids]
    if not pool:
        pool=[s for s in SENTENCES if s['level']==level and (not topic or s['topic']==topic)] or [s for s in SENTENCES if s['level']==level] or SENTENCES
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
            return self.j(200,{'azureReady':bool(AZURE_KEY and AZURE_REGION),'elevenLabsReady':bool(ELEVEN_KEY and (VOICE_IDS['female'] or VOICE_IDS['male'])),'voices':{'female':bool(VOICE_IDS['female']),'male':bool(VOICE_IDS['male'])},'ttsModel':ELEVEN_MODEL})
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
        if self.path!='/api/assess': self.send_error(404); return
        try:
            n=int(self.headers.get('Content-Length','0')); body=json.loads(self.rfile.read(n) or b'{}')
            if not AZURE_KEY or not AZURE_REGION:
                m,o,g,w,t,f=preview_feedback(body.get('previous'))
                return self.j(200,{'metrics':m,'overall':o,'good':g,'weak':w,'tip':t,'focusTerms':f,'warning':'현재는 Azure 미연결 미리보기 모드입니다. 점수와 피드백은 실제 분석 결과가 아닙니다.','preview':True})
            audio=base64.b64decode(body['audioBase64']); ref=body['referenceText']
            params={'ReferenceText':ref,'GradingSystem':'HundredMark','Granularity':'Phoneme','Dimension':'Comprehensive','EnableMiscue':'True','EnableProsodyAssessment':'True'}
            pah=base64.b64encode(json.dumps(params,separators=(',',':')).encode()).decode()
            host=f'https://{AZURE_RESOURCE}.cognitiveservices.azure.com' if AZURE_RESOURCE else f'https://{AZURE_REGION}.stt.speech.microsoft.com'
            url=host+'/speech/recognition/conversation/cognitiveservices/v1?language=en-US&format=detailed'
            req=urllib.request.Request(url,data=audio,method='POST',headers={'Ocp-Apim-Subscription-Key':AZURE_KEY,'Content-Type':'audio/wav; codecs=audio/pcm; samplerate=16000','Accept':'application/json','Pronunciation-Assessment':pah})
            with urllib.request.urlopen(req,timeout=30) as r: azure=json.loads(r.read())
            if azure.get('RecognitionStatus') not in (None,'Success'): return self.j(422,{'error':'음성을 충분히 인식하지 못했습니다. 문장 전체를 또렷하게 다시 읽어 주세요.','azure':azure})
            pa=((azure.get('NBest') or [{}])[0].get('PronunciationAssessment') or {}); comp=float(pa.get('CompletenessScore',0))
            if comp<55: return self.j(422,{'error':'기준 문장의 상당 부분이 누락되었거나 문장이 끝까지 읽히지 않았습니다. 이번 시도는 채점하지 않았습니다.','azure':azure})
            m,o,g,w,t,f=build_feedback(azure,body.get('previous')); warning=f'문장 완성도가 {comp:.0f}/100입니다. 일부 단어가 빠졌거나 문장 끝이 충분히 인식되지 않았을 수 있어요.' if comp<80 else None
            return self.j(200,{'metrics':m,'overall':o,'good':g,'weak':w,'tip':t,'focusTerms':f,'warning':warning,'azure':{'accuracy':pa.get('AccuracyScore'),'fluency':pa.get('FluencyScore'),'completeness':pa.get('CompletenessScore'),'prosody':pa.get('ProsodyScore')}})
        except urllib.error.HTTPError as e:
            detail=e.read().decode(errors='ignore'); return self.j(502,{'error':f'Azure Speech 요청 실패 ({e.code}) {detail[:240]}'})
        except Exception as e: return self.j(500,{'error':f'분석 처리 중 오류가 발생했습니다: {e}'})
    def j(self,status,obj):
        data=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)

if __name__=='__main__':
    os.chdir(ROOT); port=int(os.getenv('PORT','8000')); host=os.getenv('HOST','0.0.0.0')
    print(f'Shadowing Lab v1.3.2 → http://localhost:{port}')
    print('Azure:', 'READY' if AZURE_KEY and AZURE_REGION else 'PREVIEW MODE')
    print('ElevenLabs:', 'READY' if ELEVEN_KEY and (VOICE_IDS['female'] or VOICE_IDS['male']) else 'BROWSER FALLBACK')
    ThreadingHTTPServer((host,port),H).serve_forever()
