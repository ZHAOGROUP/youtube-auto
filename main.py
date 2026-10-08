import os, json, math, glob, subprocess, asyncio, datetime, random
import requests
import edge_tts
from PIL import Image, ImageDraw, ImageFont, ImageOps
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ====== यहाँ अपना चैनल नाम लिखो ======
CHANNEL_NAME = "ImagineDoodle"
# =====================================

W, H, FPS = 720, 1280, 24
VOICE = "hi-IN-MadhurNeural"          # महिला आवाज़ के लिए: hi-IN-SwaraNeural
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
YELLOW = (255, 214, 10)
BGS = [(0, 0, 0)]  # पूरा काला बैकग्राउंड
POSES = {  # (बायाँ हाथ, दायाँ हाथ) कोण: 0 = नीचे, 90 = सीधा, 160 = ऊपर
    "neutral": (20, 20), "point": (20, 95), "think": (20, 140),
    "surprised": (150, 150), "wave": (20, 160),
}
WORK = "work"
TITLE_Y = random.choice([90, 110, 130])   # हर वीडियो में टेक्स्ट की जगह थोड़ी बदलती है
os.makedirs(WORK, exist_ok=True)


# ---------- फ़ॉन्ट ----------
def find_font(patterns, fallback=None):
    for p in patterns:
        hits = sorted(glob.glob(p, recursive=True))
        if hits:
            return hits[0]
    return fallback


HINDI_FONT = find_font(["/usr/share/fonts/**/NotoSansDevanagari-Bold.ttf",
                        "/usr/share/fonts/**/NotoSansDevanagari*Bold*.ttf",
                        "/usr/share/fonts/**/NotoSansDevanagari*.ttf"])
LATIN_FONT = find_font(["/usr/share/fonts/**/DejaVuSans-Bold.ttf"], HINDI_FONT)


def load(path, size):
    try:
        return ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM)
    except Exception:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            return ImageFont.load_default()


F_BIG = load(HINDI_FONT, 76)
F_CAP = load(HINDI_FONT, 38)
F_SMALL = load(HINDI_FONT, 28)
F_HEAD = load(LATIN_FONT, 72)


# ---------- YouTube ----------
def yt_client():
    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube.readonly"],
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def get_insights(yt):
    """चैनल की पुरानी वीडियो से: टॉप-3 (पब्लिक, व्यूज़ के हिसाब से) और हाल के सारे टाइटल."""
    ch = yt.channels().list(part="contentDetails", mine=True).execute()
    pl = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    items = yt.playlistItems().list(part="contentDetails", playlistId=pl, maxResults=50).execute().get("items", [])
    ids = [i["contentDetails"]["videoId"] for i in items]
    if not ids:
        return [], []
    vids = yt.videos().list(part="snippet,statistics,status", id=",".join(ids)).execute()["items"]
    recent = [v["snippet"]["title"] for v in vids]
    pub = [(int(v["statistics"].get("viewCount", 0)), v["snippet"]["title"])
           for v in vids if v["status"]["privacyStatus"] == "public"]
    pub.sort(reverse=True)
    top = [f"{t} ({v} views)" for v, t in pub[:3]]
    return top, recent


# ---------- स्क्रिप्ट (Gemini) ----------
def make_script(kind, top, recent):
    kind_text = {
        "fact": "FACT video: one surprising, TRUE fact explained simply, with 2-3 supporting details.",
        "whatif": ("WHAT-IF video: a wild imagination question in the style of 'क्या होगा अगर ...' "
                   "(e.g. clouds fell instead of rain, or sudden huge money). Explain the funny/scary "
                   "consequences using real science and logic."),
        "myth": "MYTH-vs-TRUTH video: a very common belief that is actually wrong. Say the myth, then explain the real truth.",
        "list": "TOP-3 video: three surprising true things about ONE topic, each with a short simple explanation.",
        "story": "MINI-STORY video: a short, true, interesting story or discovery from history or science, told like a friend telling a story.",
    }[kind]
    words = random.choice([70, 90, 110, 130])
    prompt = f"""You write scripts for a Hindi YouTube Shorts channel "{CHANNEL_NAME}" where a yellow 2D stickman explains things.
Video type: {kind_text}
Rules:
- Language: Hindi (Devanagari), simple spoken style.
- 5 to 9 scenes, total narration about {words} words.
- Voice: a warm, cheerful best friend talking to one viewer. Use words like 'दोस्तों', 'सोचो ज़रा', short sentences, real emotions (surprise, fun, a little fear). Never use robotic phrases like 'आइए जानते हैं' or 'निष्कर्ष'.
- Title must be honest (no misleading clickbait). Make the structure different from earlier videos.
- Scene 1 is a strong hook (a question or shocking line). Last scene asks viewers to comment and follow.
- Use only facts you are sure about. Do not invent numbers. No copyrighted names or characters.
- Do NOT repeat these earlier titles: {recent[:30]}
- Audience insight, our best-performing videos so far: {top if top else 'none yet'}. Learn what topic type and hook style works, but do not copy.
Return ONLY JSON:
{{"title": "Hindi title, max 70 chars", "description": "2-3 lines Hindi", "tags": ["..."],
 "scenes": [{{"narration": "Hindi spoken line(s)", "text": "max 5 Hindi words shown big on screen",
 "pose": "neutral|point|think|surprised|wave", "visual": "none|falling|rising"}}]}}"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    r = requests.post(url, params={"key": os.environ["GEMINI_API_KEY"]}, timeout=120, json={
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0},
    })
    r.raise_for_status()
    data = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
    for s in data["scenes"]:
        if s.get("pose") not in POSES:
            s["pose"] = "neutral"
        if s.get("visual") not in ("falling", "rising"):
            s["visual"] = "none"
    return data


def fact_check(data):
    """दूसरा पास: जिस दावे पर पक्का भरोसा न हो, उसे हटाओ."""
    prompt = ("Check every factual claim in this Hindi video script. Remove or rewrite any claim you are not "
              "fully sure is true. Keep the same JSON schema and the same number of scenes where possible. "
              "Return ONLY the JSON.\n" + json.dumps(data, ensure_ascii=False))
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
        r = requests.post(url, params={"key": os.environ["GEMINI_API_KEY"]}, timeout=120, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}})
        r.raise_for_status()
        checked = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        for sc in checked["scenes"]:
            if sc.get("pose") not in POSES:
                sc["pose"] = "neutral"
            if sc.get("visual") not in ("falling", "rising"):
                sc["visual"] = "none"
        if len(checked["scenes"]) >= 4:
            return checked
    except Exception as e:
        print("Fact-check skipped:", e)
    return data


# ---------- आवाज़ ----------
def duration(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "csv=p=0", path])
    return float(out.decode().strip())


def make_audio(scenes):
    wavs, durs = [], []
    voice = random.choice(["hi-IN-MadhurNeural"] * 3 + ["hi-IN-SwaraNeural"])
    rate = random.choice(["+0%", "+5%", "+10%"])
    for i, s in enumerate(scenes):
        mp3, wav = f"{WORK}/s{i}.mp3", f"{WORK}/s{i}.wav"
        asyncio.run(edge_tts.Communicate(s["narration"], voice, rate=rate).save(mp3))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp3, "-af", "apad=pad_dur=0.4",
                        "-ar", "44100", "-ac", "2", wav], check=True)
        wavs.append(wav)
        durs.append(duration(wav))
    with open(f"{WORK}/list.txt", "w") as f:
        for w in wavs:
            f.write(f"file '{os.path.basename(w)}'\n")
    audio = f"{WORK}/audio.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", f"{WORK}/list.txt", "-c", "copy", audio], check=True)
    return audio, durs


# ---------- ड्रॉइंग ----------
def wrap(d, text, font, maxw):
    lines, cur = [], ""
    for w in text.split():
        test = (cur + " " + w).strip()
        if d.textlength(test, font=font) <= maxw:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def letter(ch, font, color=(20, 20, 20)):
    layer = Image.new("RGBA", (220, 220), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((20, 20), ch, font=font, fill=color)
    return layer.crop(layer.getbbox())


I_IMG = letter("I", F_HEAD, YELLOW)
D_IMG = ImageOps.mirror(letter("D", F_HEAD, YELLOW))   # उल्टा D


def draw_man(img, d, cx, cy, t, pose):
    cy = cy + math.sin(t * 6) * 6
    r = 75
    line = dict(fill=YELLOW, width=9)
    neck = (cx, cy + r)
    hip = (cx, cy + r + 170)
    d.line([neck, hip], **line)
    d.line([hip, (cx - 55, hip[1] + 150)], **line)
    d.line([hip, (cx + 55, hip[1] + 150)], **line)
    sh = (cx, cy + r + 40)
    left, right = POSES[pose]
    wig = math.sin(t * 8) * 12
    for sign, ang in ((-1, left + wig), (1, right - wig)):
        a = math.radians(ang)
        end = (sh[0] + sign * math.sin(a) * 120, sh[1] + math.cos(a) * 120)
        d.line([sh, end], **line)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 0, 0), outline=YELLOW, width=9)
    gap = 8
    tw = I_IMG.width + gap + D_IMG.width
    x = int(cx - tw / 2)
    y = int(cy - I_IMG.height / 2)
    img.paste(I_IMG, (x, y), I_IMG)
    img.paste(D_IMG, (x + I_IMG.width + gap, y), D_IMG)


def draw_visual(d, kind, t, seed):
    if kind == "none":
        return
    rnd = random.Random(seed)
    for _ in range(10):
        x = rnd.randint(40, W - 40)
        off = rnd.random() * H
        sp = rnd.randint(150, 300)
        size = rnd.randint(14, 30)
        if kind == "falling":
            y = (off + t * sp) % H
            col = (170, 190, 220)
        else:
            y = H - ((off + t * sp) % H)
            col = YELLOW
        d.ellipse([x - size, y - size, x + size, y + size], fill=col)


def frame(scene, t, global_t, total, idx):
    img = Image.new("RGB", (W, H), BGS[idx % len(BGS)])
    d = ImageDraw.Draw(img)
    draw_visual(d, scene["visual"], t, idx)
    y = TITLE_Y
    for ln in wrap(d, scene["text"], F_BIG, W - 80):
        w = d.textlength(ln, font=F_BIG)
        d.text(((W - w) / 2, y), ln, font=F_BIG, fill=YELLOW)
        y += 100
    draw_man(img, d, W // 2, 520, t, scene["pose"])
    lines = wrap(d, scene["narration"], F_CAP, W - 80)
    top = 960
    d.rounded_rectangle([20, top - 15, W - 20, top + len(lines) * 50 + 15], 18, fill=(28, 28, 28))
    for ln in lines:
        w = d.textlength(ln, font=F_CAP)
        d.text(((W - w) / 2, top), ln, font=F_CAP, fill=(255, 255, 255))
        top += 50
    w = d.textlength(CHANNEL_NAME, font=F_SMALL)
    d.text(((W - w) / 2, H - 70), CHANNEL_NAME, font=F_SMALL, fill=YELLOW)
    d.rectangle([0, H - 12, int(W * global_t / total), H], fill=YELLOW)
    return img


def render(scenes, durs, audio, out):
    total = sum(durs)
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", audio,
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", out],
        stdin=subprocess.PIPE)
    elapsed = 0.0
    for i, (s, dur) in enumerate(zip(scenes, durs)):
        for f in range(int(dur * FPS)):
            t = f / FPS
            proc.stdin.write(frame(s, t, elapsed + t, total, i).tobytes())
        elapsed += dur
    proc.stdin.close()
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg render failed")


# ---------- अपलोड ----------
def upload(yt, path, thumb, data):
    title = data["title"][:88] + " #Shorts"
    body = {
        "snippet": {"title": title, "description": data["description"] + "\n\n#Shorts",
                    "tags": data.get("tags", [])[:15], "categoryId": "27"},
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(path, mimetype="video/mp4", resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    print("Uploaded (private):", f"https://youtu.be/{vid}")
    try:
        yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumb)).execute()
    except Exception as e:
        print("Thumbnail skipped:", e)


def main():
    yt = yt_client()
    try:
        top, recent = get_insights(yt)
    except Exception as e:
        print("Insights skipped:", e)
        top, recent = [], []
    kind = random.choice(["fact", "whatif", "myth", "list", "story"])
    print("Type:", kind, "| Top videos:", top)
    data = make_script(kind, top, recent)
    data = fact_check(data)
    print("Title:", data["title"])
    audio, durs = make_audio(data["scenes"])
    video = f"{WORK}/video.mp4"
    render(data["scenes"], durs, audio, video)
    thumb = f"{WORK}/thumb.jpg"
    frame(data["scenes"][0], 0.5, 0.5, sum(durs), 0).save(thumb, quality=90)
    upload(yt, video, thumb, data)


if __name__ == "__main__":
    main()
