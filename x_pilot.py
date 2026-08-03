import os, re, json, time, requests
from difflib import SequenceMatcher
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

NOTION_TOKEN=os.getenv('NOTION_TOKEN','').strip()
DATABASE_ID=os.getenv('DATABASE_ID','').strip()
CONTENT_PULSE_DATABASE_ID=os.getenv('CONTENT_PULSE_DATABASE_ID','').strip()
X_HANDLE=os.getenv('X_HANDLE','Mylovanov').strip().lstrip('@')
X_COOKIES_JSON=os.getenv('X_COOKIES_JSON','').strip()
SAVE_LIMIT=int(os.getenv('SAVE_LIMIT','10').strip() or '0')
MAX_SCROLLS=int(os.getenv('MAX_SCROLLS','260').strip() or '260')
START_DATE=os.getenv('START_DATE','2026-07-27').strip()
END_DATE=os.getenv('END_DATE','2026-08-01').strip()
NOTION_VERSION='2022-06-28'
CODE_VERSION='v4_strict_dates_first_tweet_body_match'

def clean_id(v):
    v=(v or '').strip(); m=re.findall(r'[0-9a-fA-F]{32}', v.replace('-',''))
    return m[-1] if m else v.replace('-','')
DATABASE_ID=clean_id(DATABASE_ID); CONTENT_PULSE_DATABASE_ID=clean_id(CONTENT_PULSE_DATABASE_ID)
START_DT=datetime.fromisoformat(START_DATE).replace(tzinfo=timezone.utc)
END_DT=datetime.fromisoformat(END_DATE).replace(tzinfo=timezone.utc)+timedelta(days=1)

def h(): return {'Authorization':f'Bearer {NOTION_TOKEN}','Notion-Version':NOTION_VERSION,'Content-Type':'application/json'}
def norm(s):
    s=(s or '').lower(); s=re.sub(r'https?://\S+',' ',s); s=re.sub(r'[^\w\sа-яіїєґєіїʼ\'-]',' ',s,flags=re.I)
    return re.sub(r'\s+',' ',s).strip()[:1200]
def sim(a,b):
    a,b=norm(a),norm(b); return SequenceMatcher(None,a,b).ratio() if a and b else 0

def num(s):
    s=s.strip().replace(' ','').replace(',','.'); mult=1
    if s[-1:].upper() in ['K','К']: mult,s=1000,s[:-1]
    elif s[-1:].upper() in ['M','М']: mult,s=1000000,s[:-1]
    elif s[-1:].upper() in ['B','Б']: mult,s=1000000000,s[:-1]
    try: return int(float(s)*mult)
    except: return None

def views(t):
    if not t: return None
    t=t.replace('\u202f',' ').replace('\xa0',' ')
    for p in [r'([0-9]+(?:[.,][0-9]+)?\s*[KMBКМБ]?)\s+(?:Views|views|перегляд|перегляди|переглядів)',r'(?:Views|views|перегляди|переглядів)\s*[:·]?\s*([0-9]+(?:[.,][0-9]+)?\s*[KMBКМБ]?)']:
        m=re.search(p,t)
        if m: return num(m.group(1))
    return None

def prop_payload(props,name,value):
    if name not in props or value is None: return None
    typ=props[name]['type']
    if typ=='title': return {'title':[{'text':{'content':str(value)[:2000]}}]}
    if typ=='rich_text': return {'rich_text':[{'text':{'content':str(value)[:2000]}}]}
    if typ=='number': return {'number':int(value)}
    if typ=='url': return {'url':str(value)}
    if typ=='date': return {'date':{'start':str(value)}}
    if typ=='select': return {'select':{'name':str(value)}}
    if typ=='status': return {'status':{'name':str(value)}}
    return None

def db_props(db):
    r=requests.get(f'https://api.notion.com/v1/databases/{db}',headers=h(),timeout=30)
    if r.status_code!=200: print('db load error',r.status_code,r.text); return {}
    props=r.json().get('properties',{}); print('loaded database properties:',', '.join(props.keys())); return props

def plain(prop):
    typ=prop.get('type') if prop else None
    if typ=='title': return ''.join(x.get('plain_text','') for x in prop.get('title',[]))
    if typ=='rich_text': return ''.join(x.get('plain_text','') for x in prop.get('rich_text',[]))
    return ''

def block_text(page_id):
    out=[]; url=f'https://api.notion.com/v1/blocks/{page_id}/children?page_size=100'
    for _ in range(3):
        r=requests.get(url,headers=h(),timeout=30)
        if r.status_code!=200: break
        data=r.json()
        for b in data.get('results',[]):
            typ=b.get('type'); obj=b.get(typ,{})
            if 'rich_text' in obj: out.append(''.join(x.get('plain_text','') for x in obj.get('rich_text',[])))
        if not data.get('has_more'): break
        url=f'https://api.notion.com/v1/blocks/{page_id}/children?page_size=100&start_cursor={data.get("next_cursor")}'
    return '\n'.join(out)

def content_items():
    if not CONTENT_PULSE_DATABASE_ID: print('no CONTENT_PULSE_DATABASE_ID'); return []
    r=requests.post(f'https://api.notion.com/v1/databases/{CONTENT_PULSE_DATABASE_ID}/query',headers=h(),json={'page_size':100},timeout=30)
    if r.status_code!=200: print('content pulse query error',r.status_code,r.text); return []
    items=[]
    for p in r.json().get('results',[]):
        props_text='\n'.join(plain(x) for x in p.get('properties',{}).values())
        body=block_text(p['id'])
        text=(props_text+'\n'+body).strip()
        if text: items.append({'id':p['id'],'text':text})
    print('loaded content pulse candidates with body:',len(items)); return items

def match_cp(post_text,cands):
    best=(0,None)
    for c in cands:
        sc=sim(post_text,c['text'])
        if sc>best[0]: best=(sc,c)
    if best[0]>=0.42:
        print(f'content pulse match score={best[0]:.2f}'); return best[1]['id']
    print(f'no content pulse match, best_score={best[0]:.2f}'); return None

def save(item,props,cands):
    m={'Post':f"X post {item['id']}",'Channel':'X','Published at':item['published_at'],'Metric type':'Views','Metric':item['views'],'Post URL':item['url'],'Run status':'Views captured'}
    properties={}
    for k,v in m.items():
        pp=prop_payload(props,k,v)
        if pp: properties[k]=pp
    mid=match_cp(item['text'],cands)
    if mid and props.get('Content Pulse Item',{}).get('type')=='relation': properties['Content Pulse Item']={'relation':[{'id':mid}]}
    r=requests.post('https://api.notion.com/v1/pages',headers=h(),json={'parent':{'database_id':DATABASE_ID},'properties':properties},timeout=30)
    if r.status_code not in (200,201): print('notion error',r.status_code,r.text); return False
    print(f"saved to Notion: {item['id']} {item['published_at']} views={item['views']}"); return True

def cookies(ctx):
    if not X_COOKIES_JSON: print('no cookies'); return
    cs=json.loads(X_COOKIES_JSON); cs=cs.get('cookies',[]) if isinstance(cs,dict) else cs
    for c in cs:
        c.setdefault('domain','.x.com'); c.setdefault('path','/')
        if c.get('sameSite') not in [None,'Strict','Lax','None']: c.pop('sameSite',None)
    ctx.add_cookies(cs); print('loaded cookies:',len(cs))

def extract(article):
    try:
        text=article.inner_text(timeout=3000); low=text.lower()
        if any(x in low for x in ['replying to','у відповідь','в ответ']): return None
        links=article.locator("a[href*='/status/']").evaluate_all('els => els.map(a => a.href)')
        own=[]
        for href in links:
            m=re.search(rf'/({re.escape(X_HANDLE)})/status/(\d+)',href,re.I)
            if m: own.append(m.group(2))
        if not own: return None
        # first tweet in visible article/thread block only
        post_id=own[0]
        times=article.locator('time').evaluate_all("els => els.map(t => t.getAttribute('datetime'))")
        if not times: return None
        published_at=times[0]
        dt=datetime.fromisoformat(published_at.replace('Z','+00:00'))
        if not (START_DT <= dt < END_DT): return None
        aria=article.evaluate("""el => Array.from(el.querySelectorAll('[aria-label]')).map(x => x.getAttribute('aria-label')).filter(Boolean).join(' | ')""")
        v=views(text) or views(aria)
        if v is None or v>20000000: return None
        return {'id':post_id,'published_at':published_at,'dt':dt,'views':v,'url':f'https://x.com/{X_HANDLE}/status/{post_id}','text':text}
    except Exception as e:
        print('extract error',repr(e)); return None

def main():
    print('CODE_VERSION:',CODE_VERSION)
    print(f'STRICT_DATE_RANGE: {START_DATE} through {END_DATE}; Aug 2 excluded')
    print('save limit:',SAVE_LIMIT)
    seen={}
    with sync_playwright() as p:
        br=p.chromium.launch(headless=True); ctx=br.new_context(viewport={'width':1400,'height':1800}); cookies(ctx)
        page=ctx.new_page(); page.goto(f'https://x.com/{X_HANDLE}',wait_until='domcontentloaded',timeout=60000); time.sleep(5)
        page.screenshot(path='x_debug_home.png',full_page=True)
        no_new=0
        for s in range(MAX_SCROLLS):
            arts=page.locator('article'); count=arts.count(); new=0
            for i in range(count):
                it=extract(arts.nth(i))
                if it and it['id'] not in seen:
                    seen[it['id']]=it; new+=1; print(f"qualified found: {it['id']} {it['published_at']} views={it['views']}")
            print(f'scroll {s}: article nodes={count}, qualified_raw={len(seen)}, new_this_scroll={new}')
            no_new=no_new+1 if new==0 else 0
            if no_new>=25: break
            page.mouse.wheel(0,3500); time.sleep(2)
        page.screenshot(path='x_debug_after_scroll.png',full_page=True); br.close()
    q=sorted(seen.values(),key=lambda x:x['dt'],reverse=True)
    print(f'FINAL qualified posts in strict date range ({START_DATE}..{END_DATE}): {len(q)}')
    for x in q[:30]: print(f"QUALIFIED_CHECK: {x['id']} {x['published_at']} views={x['views']}")
    if SAVE_LIMIT>0: q=q[:SAVE_LIMIT]; print('limited qualified for test:',len(q))
    props=db_props(DATABASE_ID); cands=content_items()
    saved=sum(1 for it in q if save(it,props,cands))
    print(f'saved={saved}, attempted={len(q)}')
    print('verdict: saved to Notion' if saved else 'verdict: posts found, but none saved to Notion')
if __name__=='__main__': main()
