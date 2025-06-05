from flask import Flask, render_template, request, jsonify, send_from_directory
import os
import uuid
import json
import random
import openai
import requests
from dotenv import load_dotenv
from shutil import copyfile
from moviepy.editor import VideoFileClip, concatenate_videoclips

# Load environment variables
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
    chosen = random.choices(topics, weights=weights, k=1)[0]
    return chosen

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
    default = voices[0]
    return default["voice_id"], default["name"]

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
    script_prompt = f"""
    Write a short, family-friendly, legally safe and copyrighted-compliant 
    video script (about 100 words) on the topic: "{topic}". 
    Start with a legal disclaimer: "This video is for educational purposes only. 
    No professional advice is given. Consult experts if needed." 
    Structure the script as a list of five facts, each with a short explanation.
    """
    try:
        ai_response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role": "user", "content": script_prompt}],
            temperature=0.7,
            max_tokens=300
        )
        script_text = ai_response.choices[0].message.content.strip()
    except Exception as e:
        return jsonify({"status": "error", "message": f"OpenAI-Fehler: {e}"})
    voice_id, voice_name = pick_voice_by_topic(topic)
    if not voice_id:
        return jsonify({"status": "error", "message": "Keine passende Stimme gefunden."})
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
        return jsonify({"status": "error", "message": "ElevenLabs TTS fehlgeschlagen."})
    audio_filename = f"{uuid.uuid4()}.mp3"
    audio_path = os.path.join(app.config['CLIPS_FOLDER'], audio_filename)
    with open(audio_path, "wb") as f:
        f.write(tts_resp.content)
    facts = [
        line for line in script_text.splitlines()
        if line.strip().startswith(("1.", "2.", "3.", "4.", "5."))
    ]
    clips_paths = []
    for fact in facts:
        parts = fact.split()
        if len(parts) > 1:
            keyword = parts[1].strip().strip(".").lower()
        else:
            keyword = topic.split()[0].lower()
        pexels_url = f"https://api.pexels.com/videos/search?query={keyword}&per_page=1"
        pex_headers = {"Authorization": PEXELS_API_KEY}
        pex_resp = requests.get(pexels_url, headers=pex_headers)
        data = pex_resp.json()
        if data.get("videos"):
            video_file_url = data["videos"][0]["video_files"][0]["link"]
            clip_filename = f"{uuid.uuid4()}.mp4"
            clip_path = os.path.join(app.config['CLIPS_FOLDER'], clip_filename)
            clip_data = requests.get(video_file_url).content
            with open(clip_path, "wb") as cf:
                cf.write(clip_data)
            clips_paths.append(clip_path)
        else:
            clips_paths.append(os.path.join(app.root_path, 'static', 'sample.mp4'))
    try:
        video_clips = []
        for c in clips_paths:
            clip = VideoFileClip(c)
            duration = min(clip.duration, 5)
            video_clips.append(clip.subclip(0, duration))
        final_clip = concatenate_videoclips(video_clips, method="compose")
        final_audio = VideoFileClip(audio_path).audio
        final_clip = final_clip.set_audio(final_audio)
        output_filename = f"{uuid.uuid4()}.mp4"
        output_path = os.path.join(app.config['VIDEO_FOLDER'], output_filename)
        final_clip.write_videofile(output_path, codec="libx264", audio_codec="aac", verbose=False, logger=None)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Fehler bei der Videoerstellung: {e}"})
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