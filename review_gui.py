#!/usr/bin/env python3
"""review_gui.py -- keyboard-driven human review of the screening queue.

Serves a local page listing every review item that has frame evidence
(prepared by review_capture.py). Keyboard:
  g      approve the item (keep the model's category)
  w      reject the item (excluded)
  r      choose a category (keys 1-12 or click) and approve with it
  u      undo the last decision
  <- / -> move frame focus
Decisions are written back into <store>/feed_collection.sqlite (the model's
original verdict is preserved inside the new reason for audit) and the CSVs
plus analysis.html are regenerated after every decision.

Items without captured frames still appear, so you can open the reel link
and decide from the video itself.
"""

import argparse
import json
import re
import sqlite3
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import feed_collector as f

STATE = {'store': 'feed_candidates_mine', 'frames': 'review_frames', 'undo': []}

PAGE = '''<!doctype html>
<html><head><meta charset="utf-8"><title>Reel review queue</title>
<style>
body{font:15px/1.5 system-ui;margin:0;background:#f4f6fa;color:#1c2330}
header{padding:14px 22px;background:#fff;border-bottom:1px solid #e2e8f0;display:flex;gap:18px;align-items:baseline;flex-wrap:wrap}
header .code{font:700 19px ui-monospace,monospace}
header .muted{color:#5b6b80}
#meta{padding:14px 22px;background:#fff;border-bottom:1px solid #e2e8f0}
#meta .reason{color:#a33}
#meta .transcript{max-height:120px;overflow:auto;background:#f0f3f8;padding:8px 12px;border-radius:8px;white-space:pre-wrap;margin-top:8px;font-size:13px}
#frames{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;padding:18px 22px}
.card{background:#fff;border:1px solid #dde4ee;border-radius:10px;overflow:hidden;cursor:pointer}
.card img{width:100%;display:block;aspect-ratio:1/1;object-fit:cover}
.card .info{padding:6px 10px;font-size:12px;color:#5b6b80}
.card.sel{outline:3px solid #2f6fed;outline-offset:2px}
.card.flagged{border-color:#e06c4f;box-shadow:inset 0 0 0 1px #e06c4f}
.tag{display:inline-block;background:#e06c4f;color:#fff;border-radius:4px;padding:0 5px;margin-right:4px;font-size:11px}
#help{position:fixed;bottom:0;left:0;right:0;background:#101826;color:#cfd8e6;padding:10px 22px;font-size:13px}
kbd{background:#243040;border-radius:4px;padding:1px 7px;margin:0 2px}
#modal{position:fixed;inset:0;background:rgba(15,23,42,.55);display:flex;align-items:center;justify-content:center}
#modal.hidden{display:none}
#modalbox{background:#fff;border-radius:12px;padding:18px 22px;max-width:640px}
#modalbox .cats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
#modalbox button{padding:10px;font-size:14px;border:1px solid #cfd8e6;border-radius:8px;background:#f7f9fc;cursor:pointer}
#modalbox button:hover{background:#e8f0fe}
.done{padding:60px;text-align:center;font-size:18px}
</style></head>
<body>
<header id="top"><span id="prog" class="muted"></span><span id="code" class="code"></span><span id="usr" class="muted"></span><span id="likes" class="muted"></span><span id="dur" class="muted"></span><span id="audio" class="muted"></span><a id="vlink" target="_blank" class="muted">open reel ↗</a></header>
<div id="meta"><div id="caption"></div><div id="reason"></div><div id="transcript" class="transcript"></div></div>
<div id="frames"></div>
<div id="help">Keys: <kbd>g</kbd> approve &middot; <kbd>w</kbd> reject &middot; <kbd>r</kbd> choose category (<kbd>1</kbd>-<kbd>12</kbd>) &middot; <kbd>u</kbd> undo &middot; <kbd>&larr;</kbd>/<kbd>&rarr;</kbd> focus frame</div>
<div id="modal" class="hidden"><div id="modalbox"><h3>Choose a category</h3><div class="cats" id="cats"></div></div></div>
<script>
const CATS=['comedy','animals','food','sports_fitness','travel_nature','everyday_life','art_diy','science_technology','beauty_fashion','music_dance','gaming','other'];
let CUR=null,SEL=0;
const $=id=>document.getElementById(id);
async function api(path,body){const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});return r.json();}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function flagNames(m){const out=[];if(!m)return out;
 if(m.political!=='no')out.push('political?');
 if(m.sensitive!=='no')out.push('sensitive?');
 if(!(m.meaningful_text_language==='english'||m.meaningful_text_language==='none'))out.push('text lang?');
 if(m.us_audience_context!=='suitable')out.push('context?');
 if((m.confidence||0)<0.8)out.push('conf '+m.confidence);
 return out;}
async function load(code){
 const d=await api('/api/item?code='+encodeURIComponent(code));
 CUR=d;SEL=0;
 $('prog').textContent='item '+(d.index+1)+' / '+d.total+' · ';
 $('code').textContent=code;
 $('usr').textContent='@'+(d.item.username||'?');
 $('likes').textContent=((d.item.likes==null?'?':d.item.likes)+' likes');
 $('dur').textContent=((d.item.duration_seconds==null?'?':d.item.duration_seconds)+'s');
 $('audio').textContent='audio: '+(d.item.audio_status||'?');
 $('vlink').href=d.item.url||'#';
 $('caption').textContent=d.item.caption||'(no caption)';
 $('reason').innerHTML='<b>model:</b> '+esc(d.item.status)+' / '+esc(d.item.reason)+' / category <b>'+esc(d.item.category)+'</b>';
 $('transcript').textContent=d.item.transcript?'transcript: '+d.item.transcript:'(no transcript)';
 const fr=$('frames');fr.innerHTML='';
 (d.item.frames||[]).forEach((fm,i)=>{
  const card=document.createElement('div');card.className='card'+(fm.flagged?' flagged':'');
  card.innerHTML='<img src="/frames/'+encodeURIComponent(code)+'/'+encodeURIComponent(fm.file)+'">'
   +'<div class="info">t='+fm.time+'s · model: '+(fm.model?esc(fm.model.category)+' '+fm.model.confidence:'-')+'</div>';
  const tags=flagNames(fm.model);
  if(tags.length){const t=document.createElement('div');t.className='info';
   tags.forEach(x=>{const s=document.createElement('span');s.className='tag';s.textContent=x;t.appendChild(s);});
   card.appendChild(t);}
  card.onclick=()=>{SEL=i;select();};
  fr.appendChild(card);});
 if(!(d.item.frames||[]).length){
  fr.innerHTML='<div class="info">No frame evidence captured yet — run review_capture.py, or open <a href="'+esc(d.item.url)+'" target="_blank">the reel</a> and decide from the video.</div>';}
 select();}
function select(){const cards=document.querySelectorAll('.card');cards.forEach((c,i)=>c.classList.toggle('sel',i===SEL));const c=cards[SEL];if(c)c.scrollIntoView({block:'nearest'});}
async function decide(action,category){const r=await api('/api/decide',{code:CUR.code,action,category});
 if(r.next)load(r.next);else document.body.innerHTML='<div class="done">Queue complete ✓</div>';}
function openModal(){$('modal').classList.remove('hidden');const c=$('cats');c.innerHTML='';
 CATS.forEach((name,i)=>{const b=document.createElement('button');b.textContent=(i+1)+' '+name;
  b.onclick=()=>{closeModal();decide('category',name);};c.appendChild(b);});}
function closeModal(){$('modal').classList.add('hidden');}
document.addEventListener('keydown',e=>{
 if(!$('modal').classList.contains('hidden')){
  if(e.key>='1'&&e.key<='9'&&Number(e.key)<=CATS.length){const n=Number(e.key);if(n>=1&&n<=CATS.length){closeModal();decide('category',CATS[n-1]);}}
  else if(e.key==='Escape')closeModal();
  return;}
 if(!CUR)return;
 if(e.key==='g')decide('approve');
 else if(e.key==='w')decide('reject');
 else if(e.key==='r')openModal();
 else if(e.key==='u'){api('/api/undo').then(r=>{if(r.code)load(r.code);});}
 else if(e.key==='ArrowRight'&&CUR.item.frames.length){SEL=Math.min(SEL+1,CUR.item.frames.length-1);select();}
 else if(e.key==='ArrowLeft'){SEL=Math.max(SEL-1,0);select();}
});
api('/api/queue').then(q=>{
 if(q.items.length)load(q.items[0].code);
 else document.body.innerHTML='<div class="done">No review items.<br>If the queue should not be empty, run: python review_capture.py</div>';});
</script></body></html>'''


def shortcode(url):
    return re.sub(r'.*/reels?/([\w-]+)/?', r'\1', url)


def queue():
    db = sqlite3.connect(Path(STATE['store']) / 'feed_collection.sqlite')
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute(
        "SELECT url FROM feed_items WHERE status='review' ORDER BY first_seen_utc")]
    db.close()
    return [shortcode(r['url']) for r in rows]


def item_data(code):
    path = Path(STATE['frames']) / code / 'meta.json'
    if path.exists():
        item = json.loads(path.read_text(encoding='utf-8'))
    else:
        item = {'url': f'https://www.instagram.com/reel/{code}/', 'username': '',
                'caption': '', 'likes': None, 'status': 'review',
                'reason': 'no_frame_evidence', 'category': 'other',
                'audio_status': None, 'transcript': '', 'duration_seconds': None,
                'frames': []}
    items = queue()
    return {'code': code, 'item': item,
            'index': items.index(code) if code in items else 0, 'total': len(items)}


def refresh_reports():
    s = f.Store(STATE['store'])
    s.export()
    s.db.close()


def apply(code, action, category):
    db = sqlite3.connect(Path(STATE['store']) / 'feed_collection.sqlite')
    db.row_factory = sqlite3.Row
    url = f'https://www.instagram.com/reel/{code}/'
    row = db.execute('SELECT status, reason, category FROM feed_items WHERE url=?',
                     (url,)).fetchone()
    if row is None:
        db.close()
        return False
    old = (row['status'], row['reason'], row['category'])
    if action == 'reject':
        new = ('excluded', f'human_excluded_after_review (model: {old[1]})', old[2])
    elif action == 'category':
        new = ('screened_candidate',
               f'human_approved_with_category (model: {old[1]})', category or old[2])
    else:
        new = ('screened_candidate', f'human_approved_after_review (model: {old[1]})',
               old[2])
    db.execute('UPDATE feed_items SET status=?, reason=?, category=? WHERE url=?',
               (*new, url))
    db.commit()
    db.close()
    STATE['undo'].append((code, old))
    refresh_reports()
    return True


def undo():
    if not STATE['undo']:
        return None
    code, old = STATE['undo'].pop()
    db = sqlite3.connect(Path(STATE['store']) / 'feed_collection.sqlite')
    db.execute('UPDATE feed_items SET status=?, reason=?, category=? WHERE url=?',
               (*old, f'https://www.instagram.com/reel/{code}/'))
    db.commit()
    db.close()
    refresh_reports()
    return code


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body, ctype):
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/':
            self.send_bytes(PAGE.encode(), 'text/html; charset=utf-8')
            return
        if path == '/api/queue':
            self.send_json({'items': [{'code': c} for c in queue()]})
            return
        if path == '/api/item':
            code = parse_qs(urlparse(self.path).query).get('code', [''])[0]
            self.send_json(item_data(code))
            return
        if path.startswith('/frames/'):
            root = Path(STATE['frames']).resolve()
            target = (root / path[len('/frames/'):]).resolve()
            if not str(target).startswith(str(root) + '/') or not target.is_file():
                self.send_error(404)
                return
            self.send_bytes(target.read_bytes(), 'image/jpeg')
            return
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        data = json.loads(self.rfile.read(length) or b'{}')
        if self.path == '/api/decide':
            apply(data.get('code'), data.get('action'), data.get('category'))
            rest = queue()
            self.send_json({'next': rest[0] if rest else None})
            return
        if self.path == '/api/undo':
            self.send_json({'code': undo()})
            return
        self.send_error(404)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--store', default='feed_candidates_mine', help='collection folder')
    ap.add_argument('--frames', default='review_frames', help='evidence folder from review_capture.py')
    ap.add_argument('--port', type=int, default=8765, help='local server port')
    ap.add_argument('--no-browser', action='store_true', help='do not open the browser')
    args = ap.parse_args()
    STATE['store'] = args.store
    STATE['frames'] = args.frames

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Review queue: http://127.0.0.1:{args.port}   (store={args.store})')
    print(f'Review items queued: {len(queue())}')
    print('Keys: g approve · w reject · r category · u undo · Ctrl-C to quit')
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, [f'http://127.0.0.1:{args.port}']).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')


if __name__ == '__main__':
    main()
