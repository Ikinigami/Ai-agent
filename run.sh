#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
pip install -q -r requirements.txt
command -v ffmpeg >/dev/null || sudo apt-get install -y -qq ffmpeg
touch .gitignore
grep -qx ".env" .gitignore || printf ".env\n__pycache__/\n" >> .gitignore
if [ -z "$GEMINI_API_KEY" ] && ! grep -q GEMINI_API_KEY .env 2>/dev/null; then
  read -rp "Paste your Gemini API key (aistudio.google.com/apikey): " k
  echo "GEMINI_API_KEY=$k" > .env
fi
python app.py
