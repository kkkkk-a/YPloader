#!/usr/bin/env bash
set -o errexit

# Pythonパッケージのインストール
pip install -r requirements.txt

# ローカルなbinディレクトリを作成してffmpegを配置（権限エラー対策）
if [ ! -f ffmpeg_bin/ffmpeg ]; then
    echo "Installing ffmpeg..."
    mkdir -p tmp_ffmpeg
    mkdir -p ffmpeg_bin
    cd tmp_ffmpeg
    wget -q https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
    tar xf ffmpeg-release-amd64-static.tar.xz
    cp ffmpeg-*-static/ffmpeg ../ffmpeg_bin/
    cp ffmpeg-*-static/ffprobe ../ffmpeg_bin/
    chmod +x ../ffmpeg_bin/ffmpeg
    chmod +x ../ffmpeg_bin/ffprobe
    cd ..
    rm -rf tmp_ffmpeg
    echo "ffmpeg installed in local bin"
fi