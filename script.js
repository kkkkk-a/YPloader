document.addEventListener('DOMContentLoaded', () => {
    const tabs = document.querySelectorAll('.tab');
    const contents = document.querySelectorAll('.content');
    const msgEl = document.getElementById("msg");
    const ytBtn = document.getElementById('btn-yt-run');
    const pxBtn = document.getElementById('btn-px-run');

    const setMsg = (text, type) => {
        msgEl.innerText = text;
        msgEl.className = type === 'ok' ? 'msg-ok' : (type === 'err' ? 'msg-err' : 'msg-info');
    };

    const toggleBtn = (disable) => {
        ytBtn.disabled = disable;
        pxBtn.disabled = disable;
    };

    tabs.forEach(tab => tab.addEventListener('click', () => {
        tabs.forEach(t => t.classList.remove('active'));
        contents.forEach(c => c.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById(`content-${tab.dataset.target}`).classList.add('active');
    }));

    const setupCookieZone = (zoneId, inputId, statusId, setCookie) => {
        const zone = document.getElementById(zoneId);
        const input = document.getElementById(inputId);
        const status = document.getElementById(statusId);

        const updateStatus = (hasCookie) => {
            status.innerText = hasCookie ? "設定済み ✅" : "未設定 ⚠️";
            status.className = hasCookie ? "status-ok" : "status-ng";
        };

        const loadFile = file => {
            if (!file?.name.endsWith(".txt")) return setMsg(".txtファイルのみ対応", "err");

            const reader = new FileReader();
            reader.onload = e => {
                setCookie(e.target.result);
                setMsg(`Cookie読み込み完了: ${file.name}`, "ok");
                updateStatus(true);
            };

            reader.onerror = () => {
                setMsg("ファイル読み込み失敗", "err");
                updateStatus(false);
            };
            reader.readAsText(file);
        };

        zone.addEventListener('dragenter', e => { e.preventDefault(); zone.classList.add('dragover'); });
        zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
        zone.addEventListener('dragleave', e => {
            if (!e.relatedTarget || !zone.contains(e.relatedTarget)) {
                zone.classList.remove('dragover');
            }
        });
        zone.addEventListener('drop', e => {
            e.preventDefault();
            zone.classList.remove('dragover');
            loadFile(e.dataTransfer.files[0]);
        });

        input.addEventListener('change', e => loadFile(e.target.files[0]));

        updateStatus(false);
    };

    setupCookieZone(
        'yt-drop-zone',
        'yt-cookie-upload',
        'yt-c-status',
        (value) => { ytCookies = value; }
    );

    setupCookieZone(
        'px-drop-zone',
        'px-cookie-upload',
        'px-c-status',
        (value) => { pxCookies = value; }
    );


    let currentEventSource = null;
    let ytCookies = null;
    let pxCookies = null;

    const runDownload = async (endpoint, payload) => {
        const taskId = Math.random().toString(36).slice(2);
        payload.task_id = taskId;

        toggleBtn(true);
        setMsg("処理リクエスト中...", "info");

        const bar = document.getElementById("progress-bar-inner");
        if (bar) bar.style.width = `0%`;

        try {
            // タスク開始のリクエスト（裏で開始させてすぐレスポンスをもらう）
            const res = await fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (!res.ok) {
                const errData = await res.json().catch(() => ({}));
                throw new Error(errData.error || `HTTP ${res.status}`);
            }

            // SSE接続を生成して進捗を監視
            currentEventSource = new EventSource(`/api/progress/${taskId}`);

            currentEventSource.onmessage = e => {
                const data = JSON.parse(e.data);

                // エラー終了時
                if (data.p < 0 || data.m.includes("失敗")) {
                    setMsg(data.m, "err");
                    if (currentEventSource) {
                        currentEventSource.close();
                        currentEventSource = null;
                    }
                    toggleBtn(false);
                    return;
                }

                setMsg(`【${Math.round(data.p)}%】 ${data.m}`, "info");
                if (bar) bar.style.width = `${data.p}%`;

                // 完了時
                if (data.p >= 100) {
                    if (currentEventSource) {
                        currentEventSource.close();
                        currentEventSource = null;
                    }
                    setMsg("保存準備完了！ダウンロードを開始します...", "ok");
                    
                    // ブラウザの機能でファイルをGETしてダウンロード
                    window.location.href = `/api/get_file/${taskId}`;
                    
                    setTimeout(() => toggleBtn(false), 2000);
                }
            };

            currentEventSource.onerror = () => {
                if (currentEventSource) {
                    currentEventSource.close();
                    currentEventSource = null;
                }
                setMsg("接続エラーが発生しました", "err");
                toggleBtn(false);
            };

        } catch (e) {
            setMsg(`失敗: ${e.message}`, "err");
            toggleBtn(false);
        }
    };

    ytBtn.addEventListener('click', () => {
        const url = document.getElementById('yt-url').value.trim();
        const fmt = document.getElementById('yt-format').value;

        if (!url) return setMsg("URLを入力", "err");

        const payload = { url, format: fmt };
        if (ytCookies) payload.cookies = ytCookies;

        runDownload('/api/download_youtube', payload);
    });

    pxBtn.addEventListener('click', () => {
        const url = document.getElementById('px-url').value.trim();
        const cookies = pxCookies;
        if (!url) return setMsg("URLを入力", "err");
        if (!cookies) return setMsg("Cookie必須", "err");
        
        runDownload('/api/download_pixiv', { url, cookies });
    });
});