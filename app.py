import os
import sys
import shutil
import zipfile
import subprocess
import tempfile
import time
import json
import traceback
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, render_template, Response, stream_with_context

from yt_dlp import YoutubeDL
from PIL import Image
from urllib.parse import quote
from threading import Lock

app = Flask(__name__, template_folder='.', static_folder='.', static_url_path='')

MAX_WORKERS = 4
progress_store = {}
progress_lock = Lock()

# 完了したタスクのファイル情報を保存する辞書
finished_tasks = {}
finished_tasks_lock = Lock()

# 【自動お掃除機能（ガベージコレクション）】
def cleanup_old_files():
    """1時間以上経過した一時フォルダを自動削除するスレッド"""
    while True:
        try:
            now = time.time()
            temp_root = tempfile.gettempdir()
            
            # プレフィックスが yt- または px- のフォルダを探す
            for dirname in os.listdir(temp_root):
                if dirname.startswith("yt-") or dirname.startswith("px-"):
                    dir_path = os.path.join(temp_root, dirname)
                    if os.path.isdir(dir_path):
                        # フォルダの最終更新時刻を取得
                        mtime = os.path.getmtime(dir_path)
                        # 1時間 (3600秒) 以上前のものなら強制削除
                        if now - mtime > 3600:
                            shutil.rmtree(dir_path, ignore_errors=True)
                            
            # 辞書の古いデータも掃除
            keys_to_delete =[]
            with finished_tasks_lock:
                for tid, info in finished_tasks.items():
                    if not os.path.exists(info["temp_dir"]):
                        keys_to_delete.append(tid)
                for tid in keys_to_delete:
                    finished_tasks.pop(tid, None)
                    
        except Exception as e:
            print(f"Cleanup error: {e}")
            
        time.sleep(1800)  # 30分ごとにパトロール

# サーバー起動時にバックグラウンドでお掃除スレッドを開始
gc_thread = threading.Thread(target=cleanup_old_files, daemon=True)
gc_thread.start()


def stream_and_cleanup(file_path, temp_dir):
    """ファイルをストリーム送信した後に一時ディレクトリごと削除する"""
    try:
        if os.path.exists(file_path):
            with open(file_path, "rb") as f:
                while chunk := f.read(8192):
                    yield chunk
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def process_image_to_webp(file_info):
    """Pixiv画像をWebPに変換"""
    file_path, root = file_info
    try:
        if not file_path.lower().endswith(('.jpg', '.jpeg', '.png')): return
        full_path = os.path.join(root, file_path)
        webp_path = os.path.splitext(full_path)[0] + ".webp"
        
        with Image.open(full_path) as img:
            img.save(webp_path, "webp", quality=80)
        os.remove(full_path)
    except Exception:
        pass


def get_ffmpeg_path():
    """ffmpegのパスを取得（Renderのローカルパスも考慮）"""
    project_root = os.path.dirname(os.path.abspath(__file__))
    local_ffmpeg = os.path.join(project_root, "ffmpeg_bin", "ffmpeg")
    if os.path.exists(local_ffmpeg):
        return local_ffmpeg
    return shutil.which("ffmpeg") or "ffmpeg"


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/progress/<task_id>')
def progress(task_id):
    def generate():
        last_data = None
        while True:
            with progress_lock:
                data = progress_store.get(task_id)

            if data:
                if data != last_data:
                    yield f"data: {json.dumps(data)}\n\n"
                    last_data = data.copy()

                # 完了またはエラーで監視終了
                if data["p"] >= 100 or data["p"] < 0 or "失敗" in data["m"]:
                    break
            else:
                yield f'data: {json.dumps({"p": 0, "m": "準備中..."})}\n\n'

            time.sleep(0.8)

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@app.route('/api/get_file/<task_id>')
def get_file(task_id):
    """完了したファイルをブラウザに送るエンドポイント"""
    with finished_tasks_lock:
        task_info = finished_tasks.get(task_id)
    
    if not task_info:
        return "File not found or expired", 404

    file_path = task_info['file_path']
    temp_dir = task_info['temp_dir']
    content_type = task_info['content_type']
    filename = os.path.basename(file_path)
    ext = os.path.splitext(filename)[1].lower()

    # 取得後はメモリリークを防ぐため辞書から削除
    with finished_tasks_lock:
        finished_tasks.pop(task_id, None)
    with progress_lock:
        progress_store.pop(task_id, None)

    # 日本語ファイル名の文字化け・エラー対策（RFC 6266準拠）
    encoded_filename = quote(filename)

    return Response(
        stream_with_context(stream_and_cleanup(file_path, temp_dir)),
        headers={
            "Content-Disposition": f"attachment; filename=\"download{ext}\"; filename*=UTF-8''{encoded_filename}",
            "Content-Type": content_type
        }
    )


def run_yt_task(task_id, url, fmt, cookies, temp_dir):
    """YouTubeダウンロードのバックグラウンド処理"""
    try:
        postprocess_started = False
        def yt_progress_hook(d):
            nonlocal postprocess_started
            with progress_lock:
                try:
                    status = d.get('status')
                    if status == 'downloading' and not postprocess_started:
                        percent_str = d.get('_percent_str', '0%').replace('%', '').strip()
                        try:
                            percent = float(percent_str)
                        except:
                            percent = 0
                        progress_store[task_id] = {"p": percent * 0.9, "m": f"ダウンロード中... {percent_str}%"}
                    elif status == 'finished':
                        postprocess_started = True
                        progress_store[task_id] = {"p": 95, "m": "変換・後処理中..."}
                    elif status == 'error':
                        progress_store[task_id] = {"p": -1, "m": "失敗"}
                except Exception:
                    pass

        ffmpeg_path = get_ffmpeg_path()

        ydl_opts = {
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'nocheckcertificate': True,
            'quiet': True,
            'no_warnings': True,
            'ffmpeg_location': ffmpeg_path,
            'writethumbnail': True,
            'progress_hooks':[yt_progress_hook],
            
            # --- IP・国別ブロック対策 ---
            'source_address': '0.0.0.0',  # IPv4強制
            'geo_bypass': True,           # 地域制限回避
            'geo_bypass_country': 'JP',   # 日本に偽装
            # ---------------------------

            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'ios'],
                }
            },
            'headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept-Language': 'ja-JP,ja;q=0.9',
            }
        }

        if cookies:
            cookie_file_path = os.path.join(temp_dir, "cookies_yt.txt")
            with open(cookie_file_path, "w", encoding="utf-8") as f:
                f.write(cookies)
            ydl_opts['cookiefile'] = cookie_file_path

        if fmt == 'mp3':
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors':[{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
            })
            content_type = "audio/mpeg"
        elif fmt == 'opus':
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'opus'}],
            })
            content_type = "audio/opus"
        elif fmt == 'webm':
            ydl_opts.update({
                'format': 'bv*+ba/best',
                'merge_output_format': 'webm',
                'postprocessors':[]
            })
            content_type = "video/webm"
        else:
            ydl_opts.update({
                'format': 'bestvideo[height<=1080]+bestaudio/best',
                'merge_output_format': 'mp4'
            })
            content_type = "video/mp4"

        with YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        files =[f for f in os.listdir(temp_dir) if not f.endswith(('.temp', '.part', '.txt', '.json', '.webp', '.jpg', '.png'))]
        if not files:
            raise Exception("ダウンロードされたファイルが見つかりません。")

        target_file = max(files, key=lambda f: os.path.getsize(os.path.join(temp_dir, f)))
        full_path = os.path.join(temp_dir, target_file)

        ext = os.path.splitext(target_file)[1].lower()
        if ext == ".webm": content_type = "video/webm"
        elif ext == ".mp4": content_type = "video/mp4"
        elif ext == ".mp3": content_type = "audio/mpeg"
        elif ext == ".opus": content_type = "audio/opus"

        # 完了タスクとして登録
        with finished_tasks_lock:
            finished_tasks[task_id] = {
                "file_path": full_path,
                "temp_dir": temp_dir,
                "content_type": content_type
            }

        with progress_lock:
            progress_store[task_id] = {"p": 100, "m": "完了！"}

    except Exception as e:
        traceback.print_exc()
        with progress_lock:
            progress_store[task_id] = {"p": -1, "m": f"失敗: {str(e)}"}
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_px_task(task_id, url, cookies, temp_dir):
    """Pixivダウンロードのバックグラウンド処理"""
    try:
        images_dir = os.path.join(temp_dir, "images")
        cookies_file = os.path.join(temp_dir, "cookies_px.txt")
        os.makedirs(images_dir, exist_ok=True)

        with open(cookies_file, "w", encoding="utf-8") as f:
            f.write(cookies)

        refresh_token = None
        lines = cookies.splitlines() if cookies else []
        if lines and lines[0].startswith("refresh-token:"):
            refresh_token = lines[0].split(":", 1)[1].strip()
        cookies_body = "\n".join(lines[1:])
        
        if refresh_token:
            with open(cookies_file, "w", encoding="utf-8") as f:
                f.write(cookies_body)

        cmd =[
            sys.executable, "-m", "gallery_dl",
            "--ignore-config",
            "--cookies", cookies_file,
            "-o", f"extractor.pixiv.refresh-token={refresh_token}" if refresh_token else "extractor.pixiv.api=false",
            "--directory", images_dir,
            "--filename", "{title}_{id}_{num}.{extension}",
            url
        ]

        with progress_lock:
            progress_store[task_id] = {"p": 10, "m": "Pixivに接続中..."}
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1
        )

        output_lines =[]
        img_count = 0

        for line in iter(process.stdout.readline, ''):
            output_lines.append(line.rstrip())
            if "Saved" in line or "Finished" in line:
                img_count += 1
                with progress_lock:
                    progress_store[task_id] = {"p": 50, "m": f"画像を収集中... ({img_count}枚)"}
        
        process.wait()

        if process.returncode != 0:
            full_output = "\n".join(output_lines[-50:])
            raise Exception(f"gallery-dl 失敗 (code {process.returncode})\n{full_output}")

        files_to_process =[]
        for root, _, files in os.walk(images_dir):
            for f in files:
                files_to_process.append((f, root))
        
        if not files_to_process:
            raise Exception("画像が見つかりませんでした。")

        with progress_lock:
            progress_store[task_id] = {"p": 80, "m": "画像を圧縮中..."}
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            executor.map(process_image_to_webp, files_to_process)

        with progress_lock:
            progress_store[task_id] = {"p": 95, "m": "ZIPを作成中..."}
            
        zip_path = os.path.join(temp_dir, "pixiv.zip")
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(images_dir):
                for f in files:
                    full = os.path.join(root, f)
                    arcname = os.path.relpath(full, images_dir)
                    zf.write(full, arcname)

        # 完了タスクとして登録
        with finished_tasks_lock:
            finished_tasks[task_id] = {
                "file_path": zip_path,
                "temp_dir": temp_dir,
                "content_type": "application/zip"
            }

        with progress_lock:
            progress_store[task_id] = {"p": 100, "m": "完了！"}

    except Exception as e:
        traceback.print_exc()
        with progress_lock:
            progress_store[task_id] = {"p": -1, "m": f"失敗: {str(e)}"}
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.route('/api/download_youtube', methods=['POST'], strict_slashes=False)
def download_youtube():
    data = request.json
    if not data: return jsonify({"error": "リクエストが不正です"}), 400

    url = data.get('url')
    fmt = data.get('format', 'mp4')
    cookies = data.get('cookies')
    task_id = data.get('task_id')

    if not task_id or not re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", task_id):
        return jsonify({"error": "不正なtask_id"}), 400

    if cookies and len(cookies) > 30000:
        return jsonify({"error": "Cookieが大きすぎます"}), 400

    temp_dir = tempfile.mkdtemp(prefix=f"yt-{task_id}-")
    
    with progress_lock:
        progress_store[task_id] = {"p": 1, "m": "開始中..."}

    # スレッドでバックグラウンド実行（レスポンスはすぐに返す）
    thread = threading.Thread(target=run_yt_task, args=(task_id, url, fmt, cookies, temp_dir))
    thread.start()

    return jsonify({"status": "started", "task_id": task_id}), 202


@app.route('/api/download_pixiv', methods=['POST'], strict_slashes=False)
def download_pixiv():
    data = request.json
    url = data.get('url')
    cookies = data.get('cookies')
    task_id = data.get('task_id')

    if not task_id or not re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", task_id):
        return jsonify({"error": "不正なtask_id"}), 400

    if not url or not cookies:
        return jsonify({"error": "URLまたはCookieが不足しています"}), 400

    if len(cookies) > 30000:
        return jsonify({"error": "Cookieが大きすぎます"}), 400

    temp_dir = tempfile.mkdtemp(prefix=f"px-{task_id}-")

    with progress_lock:
        progress_store[task_id] = {"p": 1, "m": "準備中..."}

    # スレッドでバックグラウンド実行（レスポンスはすぐに返す）
    thread = threading.Thread(target=run_px_task, args=(task_id, url, cookies, temp_dir))
    thread.start()

    return jsonify({"status": "started", "task_id": task_id}), 202


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port, threaded=True)
