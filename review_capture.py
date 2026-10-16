#!/usr/bin/env python3
"""review_capture.py -- save lightweight frame evidence for review items.

The screening pipeline analyzes each reel in a temp dir and deletes it, so
after a run there is nothing local to eyeball. This script re-downloads each
item currently in the review queue and saves:
  * every frame the model flagged as uncertain (reusing the per-frame
    verdicts already stored in visual_json -- no model calls are made here)
  * up to --context evenly spaced context frames for scene orientation
as small 320px JPEGs under review_frames/<shortcode>/, together with a
meta.json (caption, transcript, audio status, per-frame model vote) that
review_gui.py renders as a keyboard-driven queue.

Run it only while no collector/rescreen is using the browser profile.
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from pathlib import Path

PROFILE = 'instagram-session'
INTERVAL = 2.0  # must match the frame interval used by the screening run


def clean(v):
    """A verdict is 'clean' when nothing would send the video to review."""
    return (v.get('political') == 'no' and v.get('sensitive') == 'no'
            and v.get('meaningful_text_language') in ('english', 'none')
            and v.get('us_audience_context') == 'suitable'
            and (v.get('confidence') or 0) >= 0.8)


def shortcode(url):
    return re.sub(r'.*/reels?/([\w-]+)/?', r'\1', url)


def export_cookies():
    """Instagram cookies from the collector profile, via a headless launch
    that navigates nowhere."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(Path(PROFILE).resolve()), headless=True, channel='msedge',
            args=['--disable-blink-features=AutomationControlled'],
            ignore_default_args=['--enable-automation'])
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        cookies = ctx.cookies('https://www.instagram.com/')
        ctx.close()
    lines = ['# Netscape HTTP Cookie File']
    for c in cookies:
        d = c.get('domain', '')
        lines.append('\t'.join([
            d, 'TRUE' if d.startswith('.') else 'FALSE', c.get('path', '/'),
            'TRUE' if c.get('secure') else 'FALSE',
            str(max(0, int(c.get('expires', 0)))), c.get('name', ''), c.get('value', ''),
        ]))
    return lines


def download_video(url, folder):
    with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False) as f:
        f.write('\n'.join(export_cookies()) + '\n')
        cookie_path = f.name
    # Strip dead proxy env vars (this machine's 127.0.0.1:10887).
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')}
    try:
        cmd = ['yt-dlp', '--no-update', '--no-warnings', '--no-playlist',
               '--cookies', cookie_path,
               '-f', 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b',
               '-o', str(Path(folder) / 'video.%(ext)s'),
               '--print', 'after_move:filepath', url]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not r.stdout.strip():
            raise RuntimeError((r.stderr or 'download failed').strip().splitlines()[-1])
        return Path(r.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(cookie_path)


def collect_frames(path, out_dir, interval, keep):
    """Decode the video and save only the frames whose index is in `keep`."""
    import av
    frames, idx, next_at = [], 0, 0.0
    with av.open(str(path)) as container:
        if not container.streams.video:
            raise RuntimeError('No video stream')
        for frame in container.decode(video=0):
            at = float(frame.time or 0)
            if at >= next_at:
                if idx in keep:
                    name = f'f{idx:03d}.jpg'
                    im = frame.to_image()
                    im.thumbnail((320, 320))
                    im.save(Path(out_dir) / name, quality=70)
                    frames.append({'idx': idx, 'file': name, 'time': round(at, 1)})
                idx += 1
                next_at = at + interval
    if not frames:
        raise RuntimeError('No frames decoded')
    return frames


def pick_indices(total, visuals, context):
    flagged = set()
    for v in visuals:
        s = int(v.get('sample_batch_start_index', 0))
        n = int(v.get('sample_frame_count', 0))
        if not clean(v):
            flagged.update(range(s, s + n))
    keep = set(flagged)
    if context > 0:
        step = max(1.0, (total - 1) / max(1, context - 1)) if total > 1 else 1.0
        for i in range(context):
            keep.add(min(total - 1, int(round(i * step))))
    return keep, flagged


def model_vote(visuals, idx):
    for v in visuals:
        s = int(v.get('sample_batch_start_index', 0))
        n = int(v.get('sample_frame_count', 0))
        if s <= idx < s + n:
            return v
    return None


def capture_row(row, args):
    code = shortcode(row['url'])
    out_dir = Path(args.frames) / code
    if (out_dir / 'meta.json').exists() and not args.force:
        print(f'{code}: already captured, skipping (--force to redo)')
        return
    visuals = json.loads(row['visual_json'] or '[]')
    print(f'{code}: downloading video...')
    with tempfile.TemporaryDirectory(prefix='review-cap-') as folder:
        try:
            path = download_video(row['url'], folder)
        except Exception as exc:
            print(f'{code}: DOWNLOAD FAILED ({exc}) -- skipping')
            return
        total = int((row['duration_seconds'] or 0) // INTERVAL) + 1
        keep, flagged = pick_indices(total, visuals, args.context)
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            saved = collect_frames(path, out_dir, INTERVAL, keep)
        except Exception as exc:
            print(f'{code}: DECODE FAILED ({exc}) -- skipping')
            return
    audio = json.loads(row['audio_json'] or '{}')
    meta = {
        'url': row['url'], 'username': row['username'], 'caption': row['caption'],
        'likes': row['like_count'], 'status': row['status'], 'reason': row['reason'],
        'category': row['category'], 'audio_status': audio.get('status'),
        'transcript': audio.get('transcript', ''),
        'duration_seconds': row['duration_seconds'],
        'frames': [dict(f, flagged=f['idx'] in flagged, model=model_vote(visuals, f['idx']))
                   for f in sorted(saved, key=lambda x: x['idx'])],
    }
    (out_dir / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                       encoding='utf-8')
    print(f'{code}: saved {len(saved)} frames ({len(flagged)} flagged) -> {out_dir}')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--store', default='feed_candidates_mine', help='collection folder')
    ap.add_argument('--frames', default='review_frames', help='evidence output dir')
    ap.add_argument('--context', type=int, default=12, help='context frames per video')
    ap.add_argument('--only', action='append', default=[], metavar='CODE',
                    help='shortcode to capture (repeatable)')
    ap.add_argument('--force', action='store_true', help='re-capture items that already have frames')
    args = ap.parse_args()

    db = sqlite3.connect(Path(args.store) / 'feed_collection.sqlite')
    db.row_factory = sqlite3.Row
    q = "SELECT * FROM feed_items WHERE status='review'"
    params = []
    if args.only:
        q += ' AND url IN (' + ','.join('?' * len(args.only)) + ')'
        params += [f'https://www.instagram.com/reel/{c}/' for c in args.only]
    rows = [dict(r) for r in db.execute(q + ' ORDER BY first_seen_utc', params)]
    db.close()
    print(f'{len(rows)} review item(s) to capture.')
    for row in rows:
        capture_row(row, args)
    print('Done. Start the review queue with: python review_gui.py')


if __name__ == '__main__':
    main()
