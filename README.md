# Reels feed collector

## What's new in this branch (changes from the original `main`)

This branch builds on the original collector and adds Mac support, screening-speed controls, and a keyboard-driven human-review toolchain. All new collector flags are optional; defaults reproduce the original behavior exactly.

**Fixes**
- `requirements.txt` now pins `av>=12,<16`. av 19 removed the `metadata_errors` parameter that faster-whisper 1.x passes to `av.open`, so every screening crashed with `TypeError` on fresh installs (reproduced on macOS, Python 3.13).
- `--rescreen` now retries only items whose last attempt never produced a real result (`not_screened` or `screening_failed:*`), so an interrupted rescreen resumes without redoing finished work.

**New collector flags**
- `--channel msedge|chrome|chromium` — drive a real installed browser without automation markers (default msedge). Reduces Instagram login-verification loops; no bundled-browser download needed on machines that already have Edge or Chrome.
- `--frame-interval SECONDS` — spacing between sampled frames (default 2.0, as before). Higher = faster screening, coarser coverage.
- `--vision-batch N` — frames per vision-model call (default 1, as before). `0` sends the whole video in one call; the Ollama context window expands automatically to fit the batch.
- `--speech-model NAME` — e.g. `base` for faster CPU transcription; language gates are unchanged.

With `--frame-interval 5 --vision-batch 0 --speech-model base` on an Apple Silicon Mac, screening runs at roughly 1-2 minutes per 90-second item instead of 10-15.

**New human-review toolchain (all local, no network uploads)**
- `review_capture.py` — re-downloads review items once, re-samples frames at the original interval, and saves the frames the model flagged as uncertain plus up to 12 context frames as 320 px JPEGs under `review_frames/<shortcode>/`. Reuses the stored per-frame verdicts, so it makes no model calls; the downloaded video is deleted after extraction.
- `review_gui.py` — local keyboard queue at http://127.0.0.1:8765: `g` approve, `w` reject, `r` choose a category (keys 1-12), `u` undo, arrow keys focus frames. Decisions write back to the SQLite store with an audit trail and regenerate every CSV plus analysis.html.
- `review_download.py` — list items by status, download selected ones fully into `review_videos/`, and record verdicts from the command line (`--approve` / `--reject` / `--category`).

**Repository hygiene**
- Added `.gitignore` (browser session, venv, local datasets, frame evidence, downloaded videos).
- The previously committed `instagram-session/` folder is removed from this branch's tip. **The login data still exists in the original main history — treat that account's session as compromised and log in again.**

**Known issues left unchanged for discussion**
- `decide()` accepts `no_speech_detected` audio, while the policy below says no-speech audio is held for review (`test_language_and_content_gates` documents the mismatch).
- The original code sends one frame per vision call, not the six-frame batches described below.
- `test_end_to_end_screen_success_and_failure` is stale (2 of 6 offline tests fail on a fresh checkout).

**Mac notes**
- If pip/brew/yt-dlp fail with connection errors to `127.0.0.1:10887`, dead proxy environment variables are the cause: unset `http_proxy` / `https_proxy` / `ALL_PROXY` (or prefix commands with `env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u all_proxy`).
- Run one collector/rescreen at a time: Edge hands a second launch with the same profile to the existing instance and the new run fails silently.

---

Fresh standalone package for collecting general-interest Reels, screening English audio/text and political/sensitive content, and exporting Google Sheets-compatible CSVs and statistics.

Extract this ZIP, open the english_reels_feed folder, and open PowerShell in that folder. No earlier scripts or updater are required. If desired, copy your existing instagram-session folder here to reuse your login; otherwise use the login command below.

## One-time local model setup

Actual audio and image screening requires additional models. This version uses local Ollama for vision and faster-whisper for audio; there are no Meta developer credentials or paid AI API keys. Model files take several gigabytes. CPU screening can be slow and some computers may not have enough memory. The default small speech model favors accessibility over accuracy; larger models may improve detection at a higher resource cost.

1. Install the Windows app from https://ollama.com/download/windows and keep it running. Open a new PowerShell terminal after installation if `ollama` is not recognized.
2. Install Python dependencies in your existing Python environment:

```powershell
py -m pip install -r requirements.txt
py -m playwright install chromium
ollama pull qwen2.5vl:3b
```

If pip reports missing compatible wheels for your Python 3.14 installation, use Python 3.12 from https://www.python.org/downloads/ and run the same commands with `py -3.12` instead of `py`. Use the same interpreter for installation and all runs.

3. Check setup:

```powershell
py feed_collector.py --check
```

4. Sign in once (skip if you copied a valid instagram-session folder here):

```powershell
py feed_collector.py --login
```

Sign in manually, return to the terminal and press Enter.

## Small trial, then scale

```powershell
py feed_collector.py --target 5
```

The first screened run downloads the speech model. The browser opens your normal Reels page and scrolls automatically. It records links from rendered feed links and Reel metadata that Instagram delivers during normal navigation, including prefetched items. Prefetched items may not have appeared on screen. It retrieves accessible candidate video files temporarily for analysis, then deletes them and its temporary Instagram cookie file. It never likes, follows, comments, messages, posts or bypasses login challenges.

Open the report:

```powershell
Start-Process .\feed_candidates\analysis.html
```

When the first trial works:

```powershell
py feed_collector.py --target 1000
```

Here --target counts NEW DISCOVERED Reels this run, not 1,000 passing candidates. Exclusions and uncertainties reduce the yield. Rerun to collect more; duplicates are skipped and previous results remain. This avoids promising a guaranteed passing count. Press Ctrl+C for a graceful stop and export.

## What the screener does

- Audio is processed in 20-second chunks without forcing English. A confidently non-English speech chunk excludes the item. Uncertain language goes to review.
- The vision model receives frames sampled about every two seconds (configurable with `--frame-interval`), plus the caption and full detected transcript, one frame per call by default (configurable with `--vision-batch`; `0` sends the whole video in one call). It checks meaningful text language, political/sensitive content, general US English-speaking audience context, and dominant category.
- Any predicted politics, specified sensitive content or meaningful non-English/mixed screen/caption text excludes the item.
- All visual batches must be confident and clear to enter candidates.csv. Failed downloads, missing evidence, model errors and uncertainty go to needs_review.csv.
- A silent file with no audio stream may pass visual checks. Audio with no detected speech (music, unintelligible speech or silence) is held for review: the model cannot prove that foreign lyrics/speech are absent.
- Model output cannot prove a video's country, cultural representativeness, or suitability for every individual aged 18-35. The prompt targets broad entertainment comprehensible to this audience. It does not infer sensitive personal traits from people appearing in videos.

Political exclusion includes elections, parties, politicians, political advocacy, policy controversy, and war/geopolitical conflict. Sensitive exclusions include sexual content/solicitation, graphic injury, violence/threats, self-harm, abuse, hate/harassment, dangerous challenges, illicit-drug use and distressing medical content. See POLICY at the top of feed_collector.py to adjust definitions to your lab's protocol. Ordinary romance, fashion, sports, food, animals, dance and comedy are eligible unless another exclusion applies.

## Outputs: feed_candidates/

- candidates.csv: automated screen passes, ALL still pending human verification.
- needs_review.csv: unresolved or incomplete screening.
- excluded.csv: exclusion reasons.
- all_discovered.csv: complete audit list.
- category_stats.csv: category counts and percentages among screen passes (one dominant category per candidate, percentages sum to approximately 100%).
- runs.csv: per-run configuration, policy, start/end times, new counts, duplicates and timing.
- summary.json and analysis.html: statistics, reasons, category mix, average candidate duration and cumulative stimulus duration.
- feed_collection.sqlite: resumable results. Keep it.

Google Sheets: File > Import > Upload > select a CSV. Output is UTF-8 with header rows and spreadsheet-formula escaping. A CSV import is supported; no automatic upload to your Google account occurs.

Run duration includes browser navigation, intentional waits, video fetching and model screening; excludes separate login/setup/model loading and final export. Screening duration is also recorded separately. Timing of hard-killed runs can be incomplete. The average video duration uses the last decoded video timestamp, so it may underestimate duration by approximately one frame.

## Keep data and revise screening

This feed dataset lives alongside your earlier hashtag/account results; it does not delete or silently mix them. To rescreen existing feed items after changing policy/model:

```powershell
py feed_collector.py --rescreen --target 1000
```

This updates the first 1,000 stored feed items in place. It cannot rescreen your earlier multicategory database automatically; those remain preserved separately. Every run saves its configuration/policy; each item stores its first-discovery run, latest screening run ID, and screen version. Link last_screen_run_id to runs.csv for the model/policy used. Preserve an export before rescreening if you need immutable historical classifications.

To regenerate reports without browsing:

```powershell
py feed_collector.py --analyze-only
```

For a browser-only discovery diagnostic without models:

```powershell
py feed_collector.py --collect-only --target 5
```

This puts every collected item into review, NEVER the screened-candidates file. Later run --rescreen. Existing URLs are skipped during normal collection; use --rescreen to screen items saved in collect-only mode.

## US audience versus geography

The tool does not set a country or use a proxy. English UI locale is requested; this does not enforce US recommendations. The starting mix depends on the signed-in account, connection and viewing history, and scrolling/watching can influence later recommendations. Screening aims for English and general US-audience comprehensibility, not verified US creator nationality or filming location. A single account's feed is not representative of all US adults aged 18-35. If the study requires verified geographic provenance, use a separately documented sampling frame.

## Limits and validation

Brief excluded events can occur between sampled frames. Speech models can miss short language switches or hallucinate speech; vision models can misclassify content and text. Automated pass is a shortlist, not final research eligibility. Download failures can happen even when a video plays in the browser; the item will be retained for review. No workaround for access restrictions is included.

Offline tests exercise mixed-language rejection logic, political/sensitive exclusions, missing/uncertain-evidence routing, schema validation, feed record parsing, SQLite deduplication, run/report generation, and the screening pipeline with mocked models/downloads. Models and live Instagram have not been integration-tested here. Validate sensitivity/false exclusions on a manually reviewed pilot before collecting the study set.

References: https://github.com/SYSTRAN/faster-whisper ; https://github.com/yt-dlp/yt-dlp ; https://docs.ollama.com/api ; https://ollama.com/library/qwen2.5vl


## Run offline checks

```powershell
py -m unittest discover -v
```

These checks do not use live Instagram or run the actual speech/vision models.
