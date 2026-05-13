from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import requests
import re
import json
import xml.etree.ElementTree as ET
from datetime import datetime
import urllib.parse

app = Flask(__name__)
CORS(app)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def extract_video_id(url):
    url = url.strip()
    patterns = [
        r'[?&]v=([a-zA-Z0-9_-]{11})',
        r'youtu\.be/([a-zA-Z0-9_-]{11})',
        r'shorts/([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$'
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

def get_youtube_data(video_id):
    url = f"https://www.youtube.com/watch?v={video_id}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    html = resp.text

    data = {
        "video_id": video_id,
        "url": url,
        "title": "",
        "description": "",
        "tags": [],
        "upload_date": "",
        "upload_time": "",
        "upload_datetime_raw": "",
        "channel": "",
        "views": "",
        "likes": "",
        "duration": "",
        "category": "",
        "thumbnail": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
    }

    # Extract ytInitialData JSON
    match = re.search(r'var ytInitialData = ({.+?});</script>', html, re.DOTALL)
    yt_data = {}
    if match:
        try:
            yt_data = json.loads(match.group(1))
        except:
            pass

    # Extract ytInitialPlayerResponse
    match2 = re.search(r'var ytInitialPlayerResponse = ({.+?});</script>', html, re.DOTALL)
    player_data = {}
    if match2:
        try:
            player_data = json.loads(match2.group(1))
        except:
            pass

    # --- TITLE ---
    try:
        data["title"] = player_data["videoDetails"]["title"]
    except:
        try:
            title_match = re.search(r'"title":"([^"]+)"', html)
            if title_match:
                data["title"] = title_match.group(1)
        except:
            pass

    # --- DESCRIPTION ---
    try:
        data["description"] = player_data["videoDetails"]["shortDescription"]
    except:
        pass

    # --- TAGS ---
    try:
        data["tags"] = player_data["videoDetails"]["keywords"]
    except:
        # fallback: meta tags
        tags_match = re.findall(r'<meta property="og:video:tag" content="([^"]+)"', html)
        if tags_match:
            data["tags"] = tags_match
        else:
            # try another pattern in ytInitialData
            kw_match = re.search(r'"keywords":\[([^\]]+)\]', html)
            if kw_match:
                try:
                    data["tags"] = json.loads('[' + kw_match.group(1) + ']')
                except:
                    pass

    # --- CHANNEL ---
    try:
        data["channel"] = player_data["videoDetails"]["author"]
    except:
        pass

    # --- VIEWS ---
    try:
        data["views"] = player_data["videoDetails"]["viewCount"]
    except:
        pass

    # --- UPLOAD DATE & TIME ---
    # From microformat (most accurate)
    try:
        pub = player_data["microformat"]["playerMicroformatRenderer"]["publishDate"]
        data["upload_datetime_raw"] = pub
        dt = datetime.strptime(pub, "%Y-%m-%d")
        data["upload_date"] = dt.strftime("%d %B %Y")
    except:
        pass

    # Exact upload time from dateText in ytInitialData
    try:
        video_primary = yt_data["contents"]["twoColumnWatchNextResults"]["results"]["results"]["contents"]
        for item in video_primary:
            if "videoPrimaryInfoRenderer" in item:
                date_text = item["videoPrimaryInfoRenderer"]["dateText"]["simpleText"]
                data["upload_time"] = date_text
                break
    except:
        pass

    # Try to get exact datetime from html
    try:
        dt_match = re.search(r'"uploadDate":"([^"]+)"', html)
        if dt_match:
            raw = dt_match.group(1)
            data["upload_datetime_raw"] = raw
            # Parse ISO format
            dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            data["upload_date"] = dt.strftime("%d %B %Y")
            data["upload_time"] = dt.strftime("%I:%M %p UTC")
    except:
        pass

    # --- DURATION ---
    try:
        secs = int(player_data["videoDetails"]["lengthSeconds"])
        h = secs // 3600
        m = (secs % 3600) // 60
        s = secs % 60
        data["duration"] = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except:
        pass

    # --- LIKES ---
    try:
        for item in yt_data["contents"]["twoColumnWatchNextResults"]["results"]["results"]["contents"]:
            if "videoPrimaryInfoRenderer" in item:
                likes_text = item["videoPrimaryInfoRenderer"]["videoActions"]["menuRenderer"]["topLevelButtons"][0]["segmentedLikeDislikeButtonViewModel"]["likeButtonViewModel"]["likeButtonViewModel"]["toggleButtonViewModel"]["toggleButtonViewModel"]["defaultButtonViewModel"]["buttonViewModel"]["title"]
                data["likes"] = likes_text
                break
    except:
        pass

    return data


def get_transcript(video_id):
    """Get video transcript without API key"""
    try:
        # First get the page to find timedtext URL
        url = f"https://www.youtube.com/watch?v={video_id}"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        html = resp.text

        # Find captions URL
        caps_match = re.search(r'"captions":({.+?"captionTracks":\[.+?\])', html, re.DOTALL)
        if not caps_match:
            return None, "Is video mein captions/transcript available nahi hai"

        caps_data = json.loads(caps_match.group(1) + '}')
        tracks = caps_data.get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])

        if not tracks:
            return None, "Transcript nahi mila"

        # Prefer English, then first available
        base_url = None
        for track in tracks:
            lang = track.get("languageCode", "")
            if lang in ["en", "en-US", "en-GB", "hi"]:
                base_url = track["baseUrl"]
                break
        if not base_url:
            base_url = tracks[0]["baseUrl"]

        # Fetch transcript XML
        t_resp = requests.get(base_url, headers=HEADERS, timeout=15)
        root = ET.fromstring(t_resp.text)

        transcript_parts = []
        full_text = []
        for text_el in root.findall(".//text"):
            t = text_el.text or ""
            # Clean HTML entities
            t = t.replace("&#39;", "'").replace("&amp;", "&").replace("&quot;", '"').replace("<br/>", " ")
            t = re.sub(r'<[^>]+>', '', t)
            start = float(text_el.get("start", 0))
            dur = float(text_el.get("dur", 0))
            transcript_parts.append({
                "start": round(start, 1),
                "text": t.strip()
            })
            full_text.append(t.strip())

        return {
            "segments": transcript_parts[:100],  # first 100 segments for display
            "full_text": " ".join(full_text),
            "word_count": len(" ".join(full_text).split()),
            "segment_count": len(transcript_parts)
        }, None

    except Exception as e:
        return None, f"Transcript error: {str(e)}"


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/analyze", methods=["POST"])
def analyze():
    body = request.get_json()
    url = body.get("url", "").strip()
    include_transcript = body.get("transcript", False)

    if not url:
        return jsonify({"error": "URL daalo"}), 400

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Valid YouTube URL nahi hai"}), 400

    try:
        data = get_youtube_data(video_id)
        result = {"seo": data, "transcript": None, "transcript_error": None}

        if include_transcript:
            transcript, err = get_transcript(video_id)
            result["transcript"] = transcript
            result["transcript_error"] = err

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"Error: {str(e)}"}), 500

@app.route("/api/transcript", methods=["POST"])
def transcript_only():
    body = request.get_json()
    url = body.get("url", "").strip()
    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Valid YouTube URL nahi hai"}), 400
    transcript, err = get_transcript(video_id)
    if err:
        return jsonify({"error": err}), 400
    return jsonify(transcript)

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
