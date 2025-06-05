import os
import uuid
import json
import random
import openai
import requests
import subprocess
import traceback
from flask import Flask, render_template, request, jsonify, send_from_directory
from dotenv import load_dotenv

# 1. ENV‐Variablen laden
load_dotenv()
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
ELEVENLABS_API_KEY = os.getenv('ELEVENLABS_API_KEY')
PEXELS_API_KEY = os.getenv('PEXELS_API_KEY')

# 2. OpenAI‐Key setzen
openai.api_key = OPENAI_API_KEY

# 3. Flask‐App und Verzeichnisse konfigurieren
app = Flask(__name__)
app.config['VIDEO_FOLDER'] = os.path.join(app.root_path, 'static', 'videos')
app.config['CLIPS_FOLDER'] = os.path.join(app.root_path, 'static', 'clips')
os.makedirs(app.config['VIDEO_FOLDER'], exist_ok=True)
os.makedirs(app.config['CLIPS_FOLDER'], exist_ok=True)

TOPICS_FILE = os.path.join(app.root_path, 'topics_history.json')
VOICES_FILE = os.path.join(app.root_path, 'voices.json')

# 4. Hilfsfunktionen zum Laden/Speichern von JSON
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

# 5. Thema auswählen basierend auf Score/Views
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

# 6. Stimme auswählen anhand von Kategorien
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

# 7. Startseite: zeigt vorhandene Videos + Themen-Tabelle
@app.route("/")
def index():
    video_files = os.listdir(app.config['VIDEO_FOLDER'])
    topics_data = load_json(TOPICS_FILE)
    return render_template("index.html", videos=video_files, topics=topics_data)

# 8. Route zum Generieren eines neuen Videos
@app.route("/generate", methods=["POST"])
def generate():
    try:
        # 8.1 Thema auswählen
        topic = choose_next_topic()
        if not topic:
            return jsonify({"status": "error", "message": "Keine verfügbaren Themen."}), 400

        # 8.2 Skript per gpt-3.5-turbo erzeugen
        script_prompt = (
            f"Write a short, family-friendly, legally safe and copyrighted-compliant "
            f"video script (about 100 words) on the topic: \"{topic}\". "
            f"Start with a legal disclaimer: \"This video is for educational purposes only. "
            f"No professional advice is given. Consult experts if needed.\" "
            f"Structure the script as a list of five facts, each with a short explanation."
        )
        try:
            ai_response = openai.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": script_prompt}],
                temperature=0.7,
                max_tokens=300
            )
            script_text = ai_response.choices[0].message.content.strip()
        except openai.RateLimitError:
            return jsonify({
                "status": "error",
                "step": "OpenAI-Skript",
                "message": "OpenAI-Kontingent erschöpft. Bitte prüfe Plan und Abrechnung."
            }), 429
        except openai.InvalidRequestError as e:
            return jsonify({
                "status": "error",
                "step": "OpenAI-Skript",
                "message": f"Ungültige Anfrage: {e.user_message or str(e)}"
            }), 400
        except openai.OpenAIError as e:
            return jsonify({
                "status": "error",
                "step": "OpenAI-Skript",
                "message": f"OpenAI-Fehler: {e.user_message or str(e)}"
            }), 500

        # 8.3 Passende Stimme auswählen
        voice_id, voice_name = pick_voice_by_topic(topic)
        if not voice_id:
            return jsonify({
                "status": "error",
                "step": "Stimmwahl",
                "message": "Keine passende Stimme gefunden."
            }), 500

        # 8.4 TTS per ElevenLabs
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
        tts_resp = requests.post(tts_url, headers=tts_headers, json=tts_payload)
        if tts_resp.status_code != 200:
            raise Exception(f"ElevenLabs-Status: {tts_resp.status_code}")
        audio_filename = f"{uuid.uuid4()}.mp3"
        audio_path = os.path.join(app.config['CLIPS_FOLDER'], audio_filename)
        with open(audio_path, "wb") as f:
            f.write(tts_resp.content)

        # 8.5 Fakten extrahieren und Pexels‐Clips holen
        facts = [line for line in script_text.splitlines() if line.strip().startswith(("1.", "2.", "3.", "4.", "5."))]
        if not facts:
            return jsonify({
                "status": "error",
                "step": "Fakten",
                "message": "Keine Fakten im Skript gefunden."
            }), 400

        trimmed_paths = []
        for idx, fact in enumerate(facts, start=1):
            parts = fact.split()
            if len(parts) > 1:
                keyword = parts[1].strip().strip(".").lower()
            else:
                keyword = topic.split()[0].lower()

            pexels_url = f"https://api.pexels.com/videos/search?query={keyword}&per_page=1"
            pex_resp = requests.get(pexels_url, headers={"Authorization": PEXELS_API_KEY})
            data = pex_resp.json()
            if data.get("videos"):
                video_file_url = data["videos"][0]["video_files"][0]["link"]
                clip_filename = f"{uuid.uuid4()}.mp4"
                clip_path = os.path.join(app.config['CLIPS_FOLDER'], clip_filename)
                with open(clip_path, "wb") as cf:
                    cf.write(requests.get(video_file_url).content)

                # 8.5.1 Clip auf 5 Sekunden trimmen
                trimmed = os.path.join(app.config['CLIPS_FOLDER'], f"trim_{idx}.mp4")
                subprocess.run(
                    ['ffmpeg', '-y', '-i', clip_path, '-t', '5', '-c', 'copy', trimmed],
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                trimmed_paths.append(trimmed)
            else:
                # 8.5.2 Kein Clip gefunden, Dummy‐Fallback
                sample_clip = os.path.join(app.root_path, 'static', 'sample.mp4')
                trimmed_paths.append(sample_clip)

        # 8.6 Dateiliste für ffmpeg‐Concat erstellen
        list_file = os.path.join(app.config['CLIPS_FOLDER'], 'list.txt')
        with open(list_file, 'w') as f:
            for p in trimmed_paths:
                f.write(f"file '{p}'\n")

        concat_path = os.path.join(app.config['CLIPS_FOLDER'], 'concat.mp4')
        subprocess.run(
            ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', list_file, '-c', 'copy', concat_path],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        # 8.7 Audio und Video zusammenführen
        output_filename = f"{uuid.uuid4()}.mp4"
        output_path = os.path.join(app.config['VIDEO_FOLDER'], output_filename)
        subprocess.run(
            ['ffmpeg', '-y', '-i', concat_path, '-i', audio_path, '-c:v', 'copy', '-c:a', 'aac', '-shortest', output_path],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        # Debug‐Log zum Speichern
        print(f"DEBUG: Speichere Video hier: {output_path}")

        # 8.8 Thema‐Statistik aktualisieren
        update_topic_score_and_reset_views(topic, increment=1)

        return jsonify({
            "status": "success",
            "message": f"Video erstellt: {output_filename} mit Stimme: {voice_name}",
            "topic": topic
        }), 200

    except Exception as e:
        # Vollständigen Traceback in Render-Logs ausgeben
        tb = traceback.format_exc()
        print("=== EXCEPTION in /generate ===")
        print(tb)
        print("=== END EXCEPTION ===")

        # JSON‐Antwort mit Status 500 + letzte 5 Zeilen des Tracebacks
        return jsonify({
            "status": "error",
            "step": "Unbekannter Fehler in /generate",
            "message": str(e),
            "traceback": tb.splitlines()[-5:]
        }), 500

# 9. Neue Route, um alle Videos als JSON‐Liste zu liefern
@app.route("/videos_list", methods=["GET"])
def videos_list():
    video_dir = app.config["VIDEO_FOLDER"]
    try:
        files = [f for f in os.listdir(video_dir) if f.lower().endswith(".mp4")]
    except Exception:
        files = []
    return jsonify({"videos": files})

# 10. Route, um ein einzelnes Video auszuliefern
@app.route('/videos/<filename>')
def get_video(filename):
    return send_from_directory(app.config['VIDEO_FOLDER'], filename)

if __name__ == "__main__":
    app.run(debug=True)
