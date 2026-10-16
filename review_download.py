#!/usr/bin/env python3
"""review_download.py -- eyeball-check the videos behind a screened collection.

The screening pipeline downloads each reel into a temp dir and deletes it
after analysis, so to verify label accuracy you re-download selected items
with this script and watch them locally (videos land in review_videos/).

Usage:
  python review_download.py --store feed_candidates_mine            # list all items + verdicts
  python review_download.py --store feed_candidates_mine \
      --download --status screened_candidate                        # download all that passed
  python review_download.py --store feed_candidates_mine \
      --download --code AbCd1234 --code EfGh5678                    # download specific ones

When a reel is downloaded, its model verdict (status/reason/category,
per-batch visual reasons, transcript snippet) is printed next to it so you
can compare what the model claimed with what the video actually shows.

Run this only while no collector/rescreen is using the browser profile
(Edge hands a second launch to the existing instance and exits silently).
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


def rows_for(store, status, codes):
    db = sqlite3.connect(Path(store) / 'feed_collection.sqlite')
    db.row_factory = sqlite3.Row
    q = 'SELECT * FROM feed_items'
    conds, params = [], []
    if status:
        conds.append('status=?'); params.append(status)
    if codes:
        conds.append('url IN (' + ','.join('?' * len(codes)) + ')')
        params += [f'https://www.instagram.com/reel/{c}/' for c in codes]
    if conds:
        q += ' WHERE ' + ' AND '.join(conds)
    rows = [dict(r) for r in db.execute(q + ' ORDER BY first_seen_utc')]
    db.close()
    return rows


def shortcode(url):
    return re.sub(r'.*/reels?/([\w-]+)/?', r'\1', url)


def verdict_text(row):
    """Human-readable model verdict for one item."""
    vis = json.loads(row.get('visual_evidence_json') or '[]')
    parts = [f"status={row['status']}  reason={row['reason']}  category={row['category']}"]
    parts.append(f"audio={row.get('audio_status')}  duration={row.get('duration_seconds')}s")
    for v in vis:
        fr = v.get('sample_batch_start_index', 0)
        n = v.get('sample_frame_count', 0)
        parts.append(
            f"  frames {fr}-{fr + n - 1}: {v.get('category')} "
            f"(pol={v.get('political')} sens={v.get('sensitive')} "
            f"lang={v.get('meaningful_text_language')} conf={v.get('confidence')}) "
            f"-- {v.get('reason')}")
    if row.get('transcript'):
        parts.append('  transcript: ' + row['transcript'][:300])
    return '\n'.join(parts)


def export_cookies():
    """Read the collector profile's Instagram cookies via Playwright.

    Launches the browser briefly but navigates nowhere, so Instagram sees
    no activity at all.
    """
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
        domain = c.get('domain', '')
        lines.append('\t'.join([
            domain, 'TRUE' if domain.startswith('.') else 'FALSE',
            c.get('path', '/'), 'TRUE' if c.get('secure') else 'FALSE',
            str(max(0, int(c.get('expires', 0)))), c.get('name', ''), c.get('value', ''),
        ]))
    return lines


def download(rows):
    out_dir = Path('review_videos')
    out_dir.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False) as f:
        f.write('\n'.join(export_cookies()) + '\n')
        cookie_path = f.name
    # Strip dead proxy env vars (this machine's 127.0.0.1:10887).
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')}
    try:
        for row in rows:
            print('=' * 72)
            print(f"URL: {row['url']}  @{row.get('username')}")
            print(verdict_text(row))
            cmd = ['yt-dlp', '--no-update', '--no-warnings', '--no-playlist',
                   '--cookies', cookie_path,
                   '-f', 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b',
                   '-o', str(out_dir / '%(id)s.%(ext)s'),
                   '--download-archive', str(out_dir / 'downloaded.txt'),
                   '--print', 'after_move:filepath', row['url']]
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
            if r.returncode == 0 and r.stdout.strip():
                print(f"DOWNLOADED -> {r.stdout.strip().splitlines()[-1]}")
            else:
                last = (r.stderr or '').strip().splitlines()[-1] if r.stderr else ''
                print(f"FAILED ({last or 'exit ' + str(r.returncode)})")
    finally:
        os.unlink(cookie_path)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--store', default='feed_candidates_mine', help='collection folder (default feed_candidates_mine)')
    ap.add_argument('--status', default=None, help='filter by status: screened_candidate|review|excluded')
    ap.add_argument('--download', action='store_true', help='download matching items to review_videos/')
    ap.add_argument('--code', action='append', default=[], help='shortcode to select (repeatable)')
    ap.add_argument('--approve', action='append', default=[], metavar='CODE',
                    help='after watching, mark this review item as approved (screened_candidate)')
    ap.add_argument('--reject', action='append', default=[], metavar='CODE',
                    help='after watching, mark this review item as excluded')
    ap.add_argument('--category', default=None,
                    help='category override when approving (e.g. travel_nature)')
    args = ap.parse_args()

    if args.approve or args.reject:
        db = sqlite3.connect(Path(args.store) / 'feed_collection.sqlite')
        db.row_factory = sqlite3.Row
        for code in args.approve:
            url = f'https://www.instagram.com/reel/{code}/'
            row = db.execute('SELECT status, reason, category FROM feed_items WHERE url=?', (url,)).fetchone()
            if not row:
                print(f'{code}: not found in store'); continue
            cat = args.category or row['category']
            db.execute('UPDATE feed_items SET status=?, reason=?, category=? WHERE url=?',
                       ('screened_candidate', f'human_approved_after_review (model: {row["reason"]})', cat, url))
            print(f'{code}: {row["status"]} -> screened_candidate  category={cat}')
        for code in args.reject:
            url = f'https://www.instagram.com/reel/{code}/'
            row = db.execute('SELECT status, reason, category FROM feed_items WHERE url=?', (url,)).fetchone()
            if not row:
                print(f'{code}: not found in store'); continue
            db.execute('UPDATE feed_items SET status=?, reason=? WHERE url=?',
                       ('excluded', f'human_excluded_after_review (model: {row["reason"]})', url))
            print(f'{code}: {row["status"]} -> excluded')
        db.commit(); db.close()
        import feed_collector as f
        f.Store(args.store).export()
        print('Verdicts saved; CSVs and analysis.html regenerated.')
        return

    rows = rows_for(args.store, args.status, args.code)
    if args.code and not rows:
        print('No items matched those shortcodes.')
        return
    if not args.download:
        print(f"{'CODE':<13} {'STATUS':<19} {'REASON':<34} {'CATEGORY':<17} {'DUR':>4}")
        for r in rows:
            print(f"{shortcode(r['url']):<13} {r['status']:<19} "
                  f"{str(r['reason'])[:33]:<34} {r['category']:<17} "
                  f"{str(r.get('duration_seconds') or ''):>4}")
        print(f"\n{len(rows)} items. Add --download to fetch the videos; "
              "then open review_videos/ and compare against the printed verdicts.")
        return
    if not rows:
        print('Nothing matched -- nothing to download.')
        return
    print(f'Downloading {len(rows)} item(s) to review_videos/ ...')
    download(rows)


if __name__ == '__main__':
    main()
