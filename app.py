from flask import Flask, render_template, request, jsonify, send_from_directory
import os
import uuid
import json
import random
import openai
import requests
import subprocess
from dotenv import load_dotenv

# Umgebungsladung
load_dotenv()
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
ELEVENLABS_API_KEY = os.getenv('ELEVENLABS_API_KEY')
PEXELS_API_KEY = os.getenv('PEXELS_API_KEY')

openai.api_key = OPENAI_API_KEY

app = Flask(__name__)
app.config['VIDEO_FOLDER'] = os.path.join(app.root_path, 'static', 'videos')
app.config['CLIPS_FOLDER'] = os.path.join(app.root_path, 'static', 'clips')
os.makedirs(app.config['VIDEO_FOLDER'], exist_ok=True)
os.makedirs(app.config['CLIPS_FOLDER'], exist_ok=True)

TOPICS_FILE = os.path.join(app.root_path, 'topics_history.json')
VOICES_FILE = os.path.join(app.root_path, 'voices.json')

def load_json(path):
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def choose_next_topic():
    history = load_json(TOPICS_FILE)
    if not history:
        return None
    topics = []
    weights = []
    for entry in history:
        topic = entry["topic"]
        score = entry.get("score", 0)
        views = entry.get("views", 0)
        if views < 10:
            continue
        weight = max(1, 10 - score)
        topics.append(topic)
        weights.append(weight)
    if not topics:
        topics = [e["topic"] for e in history]
        weights = [max(1, 10 - e.get("score", 0)) for e in history]
    return random.choices(topics, weights=weights, k=1)[0]

def update_topic_score_and_reset_views(chosen_topic, increment=1):
    history = load_json(TOPICS_FILE)
    for entry in history:
        if entry["topic"] == chosen_topic:
            entry["score"] = entry.get("score", 0) + increment
            save_json(TOPICS_FILE, history)
            return
    history.append({"topic": chosen_topic, "score": increment, "views": 0})
    save_json(TOPICS_FILE, history)

def pick_voice_by_topic(topic_text):
    voices = load_json(VOICES_FILE)
    if not voices:
        return None, None
    normalized = ''.join(ch.lower() if ch.isalnum() or ch.isspace() else ' ' for ch in topic_text)
    tokens = set(normalized.split())
    for voice in voices:
        for cat in voice.get("categories", []):
            if cat in tokens:
                return voice["voice_id"], voice["name"]
    for voice in voices:
        for cat in voice.get("categories", []):
            if any(cat in token for token in tokens):
                return voice["voice_id"], voice["name"]
    return voices[0]["voice_id"], voices[0]["name"]

@app.route("/")
def index():
    video_files = os.listdir(app.config['VIDEO_FOLDER'])
    topics_data = load_json(TOPICS_FILE)
    return render_template("index.html", videos=video_files, topics=topics_data)

@app.route("/generate", methods=["POST"])
def generate():
    topic = choose_next_topic()
    if not topic:
        return jsonify({"status": "error", "message": "Keine verfügbaren Themen."})

    # 1) Skript generieren
    script_prompt = f\"\"\"
Write a short, family-friendly, legally safe and copyrighted-compliant
video script (about 100 words) on the topic: \"{topic}\".
Start with a legal disclaimer: \"This video is for educational purposes only.
No professional advice is given. Consult experts if needed.\"
Structure the script as a list of five facts, each with a short explanation.
    \"\"\"
    try:
        ai_response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role": "user", "content": script_prompt}],
            temperature=0.7,
            max_tokens=300
        )
        script_text = ai_response.choices[0].message.content.strip()
    except Exception as e:
        return jsonify({"status": "error", "step": "OpenAI-Skript", "message": str(e)})

    # 2) Stimme auswählen
    voice_id, voice_name = pick_voice_by_topic(topic)
    if not voice_id:
        return jsonify({"status": "error", "step": "Stimmwahl", "message": "Keine passende Stimme gefunden."})

    # 3) TTS anfordern
    tts_url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    tts_headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }
    tts_payload = {
        "text": script_text,
        "model_id": "eleven_monolingual_v1",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.5}
    }
    try:
        tts_resp = requests.post(tts_url, headers=tts_headers, json=tts_payload)
        if tts_resp.status_code != 200:
            raise Exception(f"ElevenLabs-Status: {tts_resp.status_code}")
        audio_filename = f"{uuid.uuid4()}.mp3"
        audio_path = os.path.join(app.config['CLIPS_FOLDER'], audio_filename)
        with open(audio_path, "wb") as f:
            f.write(tts_resp.content)
    except Exception as e:
        return jsonify({"status": "error", "step": "TTS", "message": str(e)})

    # 4) Fakten extrahieren und Clips holen
    facts = [line for line in script_text.splitlines() if line.strip().startswith(("1.", "2.", "3.", "4.", "5."))]
    if not facts:
        return jsonify({"status": "error", "step": "Fakten", "message": "Keine Fakten im Skript gefunden."})

    clips_paths = []
    trimmed_paths = []
    for idx, fact in enumerate(facts, start=1):
        parts = fact.split()
        keyword = parts[1].strip().strip(".").lower() if len(parts) > 1 else topic.split()[0].lower()
        pexels_url = f"https://api.pexels.com/videos/search?query={keyword}&per_page=1"
        try:
            pex_resp = requests.get(pexels_url, headers={"Authorization": PEXELS_API_KEY})
            data = pex_resp.json()
            if data.get("videos"):
                video_file_url = data["videos"][0]["video_files"][0]["link"]
                clip_filename = f"{uuid.uuid4()}.mp4"
                clip_path = os.path.join(app.config['CLIPS_FOLDER'], clip_filename)
                with open(clip_path, "wb") as cf:
                    cf.write(requests.get(video_file_url).content)
            else:
                clip_path = os.path.join(app.root_path, 'static', 'sample.mp4')
            clips_paths.append(clip_path)
        except Exception as e:
            return jsonify({"status": "error", "step": f"Pexels Clip {idx}", "message": str(e)})

        # 5) Trimmen mit ffmpeg
        trimmed = os.path.join(app.config['CLIPS_FOLDER'], f"trim_{idx}.mp4")
        try:
            subprocess.run(['ffmpeg', '-y', '-i', clip_path, '-t', '5', '-c', 'copy', trimmed], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            trimmed_paths.append(trimmed)
        except subprocess.CalledProcessError as e:
            return jsonify({
                "status": "error",
                "step": f"ffmpeg-Trim Clip {idx}",
                "message": e.stderr.decode('utf-8', errors='ignore')
            })

    # 6) Liste für Konkatenerierung erstellen
    list_file = os.path.join(app.config['CLIPS_FOLDER'], 'list.txt')
    try:
        with open(list_file, 'w') as f:
            for p in trimmed_paths:
                f.write(f"file '{p}'\n")
    except Exception as e:
        return jsonify({"status": "error", "step": "Liste erstellen", "message": str(e)})

    concat_path = os.path.join(app.config['CLIPS_FOLDER'], 'concat.mp4')
    try:
        subprocess.run(['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', list_file, '-c', 'copy', concat_path], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        return jsonify({
            "status": "error",
            "step": "ffmpeg Concat",
            "message": e.stderr.decode('utf-8', errors='ignore')
        })

    # 7) Audio und Video kombinieren
    output_filename = f"{uuid.uuid4()}.mp4"
    output_path = os.path.join(app.config['VIDEO_FOLDER'], output_filename)
    try:
        subprocess.run(['ffmpeg', '-y', '-i', concat_path, '-i', audio_path, '-c:v', 'copy', '-c:a', 'aac', '-shortest', output_path], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        return jsonify({
            "status": "error",
            "step": "ffmpeg Merge Audio/Video",
            "message": e.stderr.decode('utf-8', errors='ignore')
        })

    # 8) Thema updaten
    update_topic_score_and_reset_views(topic, increment=1)

    return jsonify({
        "status": "success",
        "message": f"Video erstellt: {output_filename} mit Stimme: {voice_name}",
        "topic": topic
    })

@app.route('/videos/<filename>')
def get_video(filename):
    return send_from_directory(app.config['VIDEO_FOLDER'], filename)

if __name__ == "__main__":
    app.run(debug=True)
