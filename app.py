import json
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
from groq import Groq

# ---------------- SETTINGS: conservative beta limits ----------------
MAX_UPLOAD_MB = 50
MAX_VIDEO_SECONDS = 300          # 5 minutes per video
MAX_AUDIO_MB = 24                # keep below Groq's documented 25 MB limit
MAX_CLIPS_PER_VIDEO = 5  # User can choose 2-5 clips; keep beta workload controlled.
OUTPUT_WIDTH = 720
OUTPUT_HEIGHT = 1280
OUTPUT_FPS = 24
MAX_JOBS_PER_SESSION_PER_DAY = 2
MAX_JOBS_TOTAL_PER_APP_PER_DAY = 5

st.set_page_config(
    page_title="ClipAI - AI Video Clipper",
    page_icon="🎬",
    layout="wide",
)


# ---------------- SHARED IN-MEMORY DAILY COUNTER ----------------
class UsageGuard:
    """A temporary app-wide counter shared by sessions in this process."""

    def __init__(self):
        self.day = datetime.now(timezone.utc).date().isoformat()
        self.jobs = 0
        self.lock = threading.Lock()

    def reserve_job(self):
        today = datetime.now(timezone.utc).date().isoformat()
        with self.lock:
            if today != self.day:
                self.day = today
                self.jobs = 0
            if self.jobs >= MAX_JOBS_TOTAL_PER_APP_PER_DAY:
                return False, self.jobs
            self.jobs += 1
            return True, self.jobs

    def get_usage(self):
        today = datetime.now(timezone.utc).date().isoformat()
        with self.lock:
            if today != self.day:
                self.day = today
                self.jobs = 0
            return self.jobs


@st.cache_resource
def get_usage_guard():
    return UsageGuard()


guard = get_usage_guard()


def get_value(item, key, default=None):
    """Read Groq response fields from either dicts or SDK objects."""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def run_command(command, timeout=300):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = result.stderr[-1200:] if result.stderr else "Unknown media error."
        raise RuntimeError(detail)
    return result


def get_video_duration(video_path):
    result = run_command(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        timeout=60,
    )
    return float(result.stdout.strip())


def srt_time(seconds):
    """Convert seconds to an SRT timestamp."""
    milliseconds = max(0, int(round(float(seconds) * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def write_clip_subtitles(segments, clip_start, clip_end, subtitle_path):
    """Create clip-relative SRT captions from Groq timestamped segments."""
    entries = []
    for segment in segments:
        start = get_value(segment, "start")
        end = get_value(segment, "end")
        text = str(get_value(segment, "text", "")).strip()
        if start is None or end is None or not text:
            continue
        try:
            start = float(start)
            end = float(end)
        except (TypeError, ValueError):
            continue

        overlap_start = max(start, clip_start)
        overlap_end = min(end, clip_end)
        if overlap_end <= overlap_start:
            continue

        # Keep subtitle text on one line; SRT/FFmpeg handles display wrapping.
        text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
        entries.append((overlap_start - clip_start, overlap_end - clip_start, text))

    with open(subtitle_path, "w", encoding="utf-8") as subtitle_file:
        for index, (start, end, text) in enumerate(entries, 1):
            subtitle_file.write(
                f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n\n"
            )

    return bool(entries)



def ass_time(seconds):
    """Convert seconds to ASS timestamp (h:mm:ss.cc)."""
    total_centiseconds = max(0, int(round(float(seconds) * 100)))
    hours, remainder = divmod(total_centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"


def escape_ass_text(value):
    """Escape text that has special meaning in ASS subtitle files."""
    return (
        str(value)
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def write_word_highlight_ass(words, clip_start, clip_end, subtitle_path):
    """Write compact, bottom-positioned captions with one active word highlighted.

    Each caption group contains up to four words. A timed event is emitted for
    each spoken word; only that word is yellow while the other words stay white.
    """
    timed_words = []
    for item in words or []:
        start = get_value(item, "start")
        end = get_value(item, "end")
        word = get_value(item, "word", get_value(item, "text", ""))
        word = str(word or "").strip()
        if start is None or end is None or not word:
            continue
        try:
            start = float(start)
            end = float(end)
        except (TypeError, ValueError):
            continue

        start = max(start, clip_start)
        end = min(end, clip_end)
        if end <= start:
            continue
        timed_words.append({
            "start": start - clip_start,
            "end": end - clip_start,
            "word": word,
        })

    if not timed_words:
        return False

    # Build short readable caption groups: up to four words or a sentence break.
    groups = []
    current = []
    for item in timed_words:
        current.append(item)
        punctuation_break = item["word"].endswith((".", "!", "?", ";", ":"))
        group_text_length = sum(len(part["word"]) + 1 for part in current)
        if len(current) >= 4 or punctuation_break or group_text_length >= 30:
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 720",
        "PlayResY: 1280",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # Smaller, bold, white captions; the active word is colored via inline ASS tags.
        "Style: Default,DejaVu Sans,28,&H00FFFFFF,&H0000D7FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,2,42,42,175,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    dialogue = []
    for group in groups:
        plain_words = [escape_ass_text(part["word"]) for part in group]
        for active_index, active in enumerate(group):
            rendered = []
            for index, word in enumerate(plain_words):
                if index == active_index:
                    rendered.append(r"{\c&H0000D7FF&}" + word + r"{\c&H00FFFFFF&}")
                else:
                    rendered.append(word)
            text = " ".join(rendered)
            start = active["start"]
            end = max(active["end"], start + 0.06)
            dialogue.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{text}"
            )

    subtitle_path.write_text("\n".join(header + dialogue) + "\n", encoding="utf-8")
    return bool(dialogue)


def validate_suggestions(data, duration, requested_clip_count):
    if not isinstance(data, dict):
        raise ValueError("AI returned an invalid response.")

    suggestions = data.get("clips", [])
    if not isinstance(suggestions, list):
        raise ValueError("AI clip suggestions were not a list.")

    valid = []
    for item in suggestions[:requested_clip_count]:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue

        if start < 0 or end > duration or end <= start:
            continue
        if not 10 <= (end - start) <= 65:
            continue

        valid.append(
            {
                "start": start,
                "end": end,
                "title": str(item.get("title", "AI Clip"))[:70],
                "reason": str(item.get("reason", "Interesting moment"))[:250],
            }
        )

    return valid


# ---------------- PROFESSIONAL CLIPAI INTERFACE ----------------
st.markdown(
    """
    <style>
    :root { --clip-bg:#0b0d14; --clip-panel:#121725; --clip-panel-2:#171e2d;
            --clip-line:rgba(255,255,255,.10); --clip-muted:#aab3c5;
            --clip-accent:#9b8cff; --clip-accent-2:#65d6c4; }
    [data-testid="stAppViewContainer"] { background:var(--clip-bg); }
    [data-testid="stHeader"] { background:rgba(11,13,20,.86); }
    [data-testid="stMainBlockContainer"] { max-width:1160px; padding-top:1.25rem; padding-bottom:3rem; }
    h1,h2,h3 { letter-spacing:-.035em; }
    .clipai-nav { display:flex; align-items:center; justify-content:space-between; gap:18px;
        padding:12px 0 22px; border-bottom:1px solid var(--clip-line); margin-bottom:30px; }
    .clipai-brand { color:#fff !important; text-decoration:none !important; font-size:1.34rem;
        font-weight:800; letter-spacing:-.04em; }
    .clipai-brand span { color:var(--clip-accent); }
    .clipai-navlinks { display:flex; gap:22px; flex-wrap:wrap; }
    .clipai-navlinks a { color:#c6cede !important; text-decoration:none !important; font-size:.88rem; }
    .clipai-navlinks a:hover { color:#fff !important; }
    .clipai-pill { border:1px solid rgba(155,140,255,.35); color:#c9c0ff; background:rgba(155,140,255,.10);
        border-radius:999px; padding:6px 11px; font-size:.75rem; white-space:nowrap; }
    .clipai-hero { display:grid; grid-template-columns:1.35fr .85fr; gap:25px; align-items:center;
        padding:30px; border:1px solid var(--clip-line); border-radius:22px;
        background:radial-gradient(ellipse at 85% 5%,rgba(155,140,255,.18),transparent 42%),
                   radial-gradient(ellipse at 5% 95%,rgba(101,214,196,.10),transparent 38%),#111624;
        margin-bottom:26px; }
    .clipai-eyebrow { color:#bdb2ff; text-transform:uppercase; letter-spacing:.13em; font-size:.72rem; font-weight:800; }
    .clipai-hero h1 { color:#fff; font-size:clamp(2.1rem,5vw,3.65rem); line-height:1.04; margin:.65rem 0 .8rem; }
    .clipai-hero h1 span { background:linear-gradient(90deg,#b7aaff,#71dfd0); -webkit-background-clip:text; background-clip:text; color:transparent; }
    .clipai-hero p { color:#b9c2d2; font-size:1.02rem; line-height:1.65; max-width:650px; margin-bottom:0; }
    .clipai-hero-card { border:1px solid rgba(255,255,255,.12); border-radius:17px; padding:19px;
        background:rgba(8,11,19,.55); }
    .clipai-mini-label { color:#95a0b5; text-transform:uppercase; font-size:.68rem; letter-spacing:.12em; }
    .clipai-mini-title { color:#fff; font-weight:750; font-size:1.18rem; margin:7px 0 5px; }
    .clipai-mini-copy { color:#aeb8c9; font-size:.82rem; line-height:1.5; }
    .clipai-statrow { display:flex; gap:9px; flex-wrap:wrap; margin-top:17px; }
    .clipai-stat { border:1px solid var(--clip-line); background:rgba(255,255,255,.045); border-radius:10px;
        padding:9px 11px; color:#d7dced; font-size:.78rem; }
    .clipai-section-kicker { color:#a99cff; font-size:.72rem; font-weight:800; letter-spacing:.12em; text-transform:uppercase; }
    .clipai-section-title { color:#fff; font-weight:800; font-size:1.65rem; letter-spacing:-.04em; margin:4px 0 5px; }
    .clipai-section-copy { color:var(--clip-muted); font-size:.91rem; margin-bottom:14px; }
    .clipai-feature-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:13px; margin:12px 0 26px; }
    .clipai-feature { background:var(--clip-panel); border:1px solid var(--clip-line); border-radius:15px; padding:17px; }
    .clipai-feature-icon { font-size:1.35rem; margin-bottom:9px; }
    .clipai-feature-title { color:#f4f5fa; font-weight:750; margin-bottom:5px; }
    .clipai-feature-copy { color:#aab3c5; font-size:.82rem; line-height:1.5; }
    .clipai-limit { color:#c9d5e8; background:rgba(101,214,196,.08); border:1px solid rgba(101,214,196,.2);
        border-radius:12px; padding:12px 15px; font-size:.83rem; margin:12px 0; }
    .clipai-footer { border-top:1px solid var(--clip-line); padding-top:18px; margin-top:28px;
        color:#919caf; font-size:.78rem; display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; }
    .clipai-footer a { color:#c7bdff !important; text-decoration:none !important; }
    div[data-testid="stFileUploader"] section { border-radius:14px; }
    div[data-testid="stButton"] button[kind="primary"] { border-radius:11px; font-weight:750; }
    @media (max-width:760px) {
      [data-testid="stMainBlockContainer"] { padding-left:1rem; padding-right:1rem; }
      .clipai-nav { align-items:flex-start; flex-direction:column; }
      .clipai-navlinks { gap:14px; }
      .clipai-hero { grid-template-columns:1fr; padding:22px; }
      .clipai-hero h1 { font-size:2.35rem; }
      .clipai-feature-grid { grid-template-columns:1fr; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div id="top" class="clipai-nav">
      <a class="clipai-brand" href="#top">🎬 Clip<span>AI</span></a>
      <div class="clipai-navlinks">
        <a href="#create">Create clips</a>
        <a href="#how-it-works">How it works</a>
        <a href="#features">Features</a>
      </div>
      <span class="clipai-pill">FREE BETA</span>
    </div>
    <section class="clipai-hero">
      <div>
        <div class="clipai-eyebrow">AI short-form video studio</div>
        <h1>One long video.<br><span>More moments worth sharing.</span></h1>
        <p>ClipAI finds engaging moments in your video and turns them into vertical shorts with optional word-highlight captions—ready for your next post.</p>
        <div class="clipai-statrow">
          <span class="clipai-stat">✂️ AI moment selection</span>
          <span class="clipai-stat">📱 Vertical 9:16 exports</span>
          <span class="clipai-stat">💬 Captions option</span>
        </div>
      </div>
      <div class="clipai-hero-card">
        <div class="clipai-mini-label">Your creator workflow</div>
        <div class="clipai-mini-title">From full-length to share-ready</div>
        <div class="clipai-mini-copy">Upload a video, choose how many clips to try, then preview and download each result.</div>
        <div class="clipai-statrow">
          <span class="clipai-stat">01 · Upload</span>
          <span class="clipai-stat">02 · Find moments</span>
          <span class="clipai-stat">03 · Export</span>
        </div>
        <div class="clipai-mini-copy" style="margin-top:14px">Video-link import is planned next for supported and authorized sources.</div>
      </div>
    </section>
    <div id="create" class="clipai-section-kicker">Create</div>
    <div class="clipai-section-title">Make your first clips</div>
    <div class="clipai-section-copy">Choose your output preferences, upload a video, and let ClipAI do the editing work.</div>
    """,
    unsafe_allow_html=True,
)

settings_a, settings_b, settings_c = st.columns([1, 1.35, 1])
with settings_a:
    burn_captions = st.checkbox(
        "Burn captions into the video",
        value=True,
        help="Turn this off for faster rendering. A separate SRT file will still be available.",
    )
with settings_b:
    layout_choice = st.radio(
        "Vertical video layout",
        ["Fill screen (may crop left/right edges)", "Fit full video (black bars)"],
        horizontal=False,
        help="Fill screen looks like a Reel. Fit full video keeps the entire original frame visible.",
    )
with settings_c:
    requested_clip_count = st.select_slider(
        "Clips to create",
        options=[2, 3, 4, 5],
        value=2,
        help="More clips can take longer. ClipAI may return fewer if there are not enough distinct moments.",
    )

st.caption("Fast beta export: 720 × 1280 at 24 FPS. We are optimizing quality and processing time before increasing limits.")

st.markdown(
    """
    <div class="clipai-limit"><b>Beta limits:</b> 50 MB upload · 5-minute video · 2 jobs per browser session per day · 5 jobs shared across the app per day.</div>
    """,
    unsafe_allow_html=True,
)

used_today = guard.get_usage()
current_day = datetime.now(timezone.utc).date().isoformat()
if st.session_state.get("clipai_session_day") != current_day:
    st.session_state["clipai_session_day"] = current_day
    st.session_state["clipai_session_jobs"] = 0
session_jobs = st.session_state.get("clipai_session_jobs", 0)

usage_col1, usage_col2 = st.columns(2)
with usage_col1:
    st.caption(f"Shared beta usage today: {used_today}/{MAX_JOBS_TOTAL_PER_APP_PER_DAY} jobs (UTC reset).")
with usage_col2:
    st.caption(f"This browser session: {session_jobs}/{MAX_JOBS_PER_SESSION_PER_DAY} jobs used today.")

st.divider()
st.markdown('<div class="clipai-section-kicker">Upload</div><div class="clipai-section-title">Choose your video</div>', unsafe_allow_html=True)

uploaded_file = st.file_uploader(
    "📁 Upload your video",
    type=["mp4", "mov", "mkv", "avi", "webm"],
    max_upload_size=MAX_UPLOAD_MB,
    help=f"Maximum file size: {MAX_UPLOAD_MB} MB. Maximum video length: 5 minutes.",
)

if uploaded_file is not None:
    file_size_mb = uploaded_file.size / (1024 * 1024)
    st.success(f"Video uploaded: {uploaded_file.name} ({file_size_mb:.1f} MB)")
    st.video(uploaded_file, width=320)

    if file_size_mb > MAX_UPLOAD_MB:
        st.error(f"Please upload a file smaller than {MAX_UPLOAD_MB} MB.")
    elif session_jobs >= MAX_JOBS_PER_SESSION_PER_DAY:
        st.warning("You have used this browser session's daily beta limit. Please try after the daily reset.")
    elif used_today >= MAX_JOBS_TOTAL_PER_APP_PER_DAY:
        st.warning("ClipAI has reached today's shared beta limit. Please try after the daily reset.")
    else:
        if st.button("✨ Generate AI Clips", type="primary"):
            # Reserve a shared app slot before any API request, to avoid repeated clicks.
            allowed, total_used = guard.reserve_job()
            if not allowed:
                st.warning("ClipAI has reached today's shared beta limit. Please try after the daily reset.")
            else:
                st.session_state["clipai_session_jobs"] = session_jobs + 1
                st.caption(
                    f"This attempt is counted. Shared beta usage: "
                    f"{total_used}/{MAX_JOBS_TOTAL_PER_APP_PER_DAY}."
                )

                try:
                    api_key = st.secrets["GROQ_API_KEY"]
                    client = Groq(api_key=api_key)

                    with tempfile.TemporaryDirectory() as temp_dir:
                        folder = Path(temp_dir)
                        suffix = Path(uploaded_file.name).suffix.lower() or ".mp4"
                        video_path = folder / f"original{suffix}"
                        audio_path = folder / "audio.mp3"
                        video_path.write_bytes(uploaded_file.getvalue())

                        with st.spinner("Checking video..."):
                            duration = get_video_duration(video_path)
                            if duration <= 0:
                                raise ValueError("The video duration could not be read.")
                            if duration > MAX_VIDEO_SECONDS:
                                raise ValueError("For the free beta, please use a video of 5 minutes or less.")

                        with st.spinner("Extracting audio..."):
                            run_command(
                                [
                                    "ffmpeg", "-y", "-i", str(video_path),
                                    "-map", "0:a:0", "-vn", "-ac", "1",
                                    "-ar", "16000", "-b:a", "48k", str(audio_path),
                                ],
                                timeout=180,
                            )

                        if not audio_path.exists():
                            raise RuntimeError("Audio extraction failed.")
                        if audio_path.stat().st_size > MAX_AUDIO_MB * 1024 * 1024:
                            raise ValueError("Extracted audio is too large. Try a shorter video.")

                        with st.spinner("AI is transcribing the speech..."):
                            with open(audio_path, "rb") as audio_file:
                                transcription = client.audio.transcriptions.create(
                                    file=audio_file,
                                    model="whisper-large-v3-turbo",
                                    response_format="verbose_json",
                                    timestamp_granularities=["word", "segment"],
                                    temperature=0.0,
                                )

                        transcript = get_value(transcription, "text", "")
                        segments = get_value(transcription, "segments", []) or []
                        word_timestamps = get_value(transcription, "words", []) or []
                        if not transcript or not segments:
                            raise ValueError("No clear speech was detected. Try a video with audible speech.")

                        transcript_lines = []
                        for segment in segments[:500]:
                            start = get_value(segment, "start")
                            end = get_value(segment, "end")
                            words = get_value(segment, "text", "")
                            if start is None or end is None:
                                continue
                            try:
                                start = float(start)
                                end = float(end)
                                words = str(words).strip()
                                if words and 0 <= start < end <= duration:
                                    transcript_lines.append(f"[{start:.2f}-{end:.2f}] {words}")
                            except (TypeError, ValueError):
                                continue

                        if not transcript_lines:
                            raise ValueError("AI did not return usable speech timestamps.")

                        timestamped_transcript = "\n".join(transcript_lines)

                        with st.spinner("AI is selecting the best moments..."):
                            response = client.chat.completions.create(
                                model="openai/gpt-oss-20b",
                                messages=[
                                    {
                                        "role": "system",
                                        "content": (
                                            "You are an expert short-form video editor. "
                                            f"Find at most {requested_clip_count} self-contained, engaging moments. "
                                            "Return the strongest distinct moments first, and avoid overlapping or repeated ideas. "
                                            "Each clip must be 20-60 seconds. Use only the "
                                            "provided timestamps, stay inside the video duration, "
                                            "avoid repeated moments, and return valid JSON only "
                                            "with a clips array. Each item needs start, end, title, reason."
                                        ),
                                    },
                                    {
                                        "role": "user",
                                        "content": (
                                            f"Video duration: {duration:.2f} seconds.\n"
                                            f"Select up to {requested_clip_count} strongest, distinct moments from this transcript. "
                                            "If there are not enough good moments, return fewer rather than weak clips. "
                                            "Respond as JSON matching this exact shape: "
                                            '{"clips":[{"start":10.0,"end":35.0,"title":"Short title","reason":"Why it is engaging"}]}.\n'
                                            + timestamped_transcript
                                        ),
                                    },
                                ],
                                response_format={
                                    "type": "json_schema",
                                    "json_schema": {
                                        "name": "clip_suggestions",
                                        "strict": True,
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "clips": {
                                                    "type": "array",
                                                    "items": {
                                                        "type": "object",
                                                        "properties": {
                                                            "start": {"type": "number"},
                                                            "end": {"type": "number"},
                                                            "title": {"type": "string"},
                                                            "reason": {"type": "string"},
                                                        },
                                                        "required": ["start", "end", "title", "reason"],
                                                        "additionalProperties": False,
                                                    },
                                                }
                                            },
                                            "required": ["clips"],
                                            "additionalProperties": False,
                                        },
                                    },
                                },
                                temperature=0.2,
                                max_completion_tokens=2000,
                            )

                        ai_text = response.choices[0].message.content
                        if not ai_text:
                            raise ValueError("AI returned an empty clip-selection response.")
                        ai_data = json.loads(ai_text)
                        clips = validate_suggestions(ai_data, duration, requested_clip_count)
                        if not clips:
                            raise ValueError("AI could not find suitable 20-60 second clips in this video.")

                        generated = []
                        for number, clip in enumerate(clips, 1):
                            output_path = folder / f"clip_{number}_vertical.mp4"
                            subtitle_path = folder / f"clip_{number}.srt"
                            ass_path = folder / f"clip_{number}_word_highlight.ass"
                            clip_length = clip["end"] - clip["start"]
                            has_subtitles = write_clip_subtitles(
                                segments,
                                clip["start"],
                                clip["end"],
                                subtitle_path,
                            )
                            has_word_captions = write_word_highlight_ass(
                                word_timestamps,
                                clip["start"],
                                clip["end"],
                                ass_path,
                            )

                            # Fast 9:16 export. One scale/crop (or scale/pad) pass is
                            # much lighter than the previous blurred-background composition.
                            if layout_choice == "Fill screen (may crop left/right edges)":
                                video_filter = (
                                    f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,"
                                    f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},fps={OUTPUT_FPS}"
                                )
                            else:
                                video_filter = (
                                    f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,"
                                    f"pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
                                    f"fps={OUTPUT_FPS}"
                                )

                            caption_path = ass_path if has_word_captions else subtitle_path
                            if burn_captions and (has_word_captions or has_subtitles):
                                # ASS word captions use 28px text, a safe lower-third
                                # position, and a moving yellow highlight on the spoken word.
                                if has_word_captions:
                                    caption_file = ass_path.as_posix().replace(":", r"\:")
                                    video_filter += f",subtitles='{caption_file}'"
                                else:
                                    style = (
                                        "FontName=DejaVu Sans,FontSize=28,"
                                        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                                        "BorderStyle=1,Outline=2,Shadow=1,"
                                        "Alignment=2,MarginV=175"
                                    )
                                    caption_file = subtitle_path.as_posix().replace(":", r"\:")
                                    video_filter += (
                                        f",subtitles='{caption_file}':force_style='{style}'"
                                    )

                            with st.spinner(f"Creating fast vertical clip {number} of {len(clips)}..."):
                                encode_args = [
                                    "ffmpeg", "-y",
                                    "-ss", str(clip["start"]),
                                    "-i", str(video_path),
                                    "-t", str(clip_length),
                                    "-vf", video_filter,
                                    "-map", "0:v:0", "-map", "0:a:0?",
                                    "-c:v", "libx264", "-preset", "ultrafast",
                                    "-crf", "25", "-threads", "2",
                                    "-c:a", "aac", "-b:a", "96k",
                                    "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                                    str(output_path),
                                ]
                                try:
                                    run_command(encode_args, timeout=240)
                                except RuntimeError:
                                    # If subtitle rendering is unavailable, retry without
                                    # burned captions; the separate SRT remains downloadable.
                                    if burn_captions and (has_word_captions or has_subtitles):
                                        output_path.unlink(missing_ok=True)
                                        st.warning(
                                            "Could not burn captions into this MP4 on the current server. "
                                            "The vertical clip will still be created, and the SRT subtitle file will be available."
                                        )
                                        clean_filter = video_filter.split(",subtitles=", 1)[0]
                                        encode_args[encode_args.index(video_filter)] = clean_filter
                                        run_command(encode_args, timeout=240)
                                    else:
                                        raise

                            if output_path.exists() and output_path.stat().st_size > 0:
                                generated.append(
                                    {
                                        "title": clip["title"],
                                        "reason": clip["reason"],
                                        "start": clip["start"],
                                        "end": clip["end"],
                                        "data": output_path.read_bytes(),
                                        "srt": subtitle_path.read_bytes() if subtitle_path.exists() and subtitle_path.stat().st_size > 0 else None,
                                    }
                                )

                        if not generated:
                            raise RuntimeError("Video cutting did not produce a downloadable clip.")

                    st.balloons()
                    st.success(f"Done! {len(generated)} clips created successfully.")
                    # Two-column result grid keeps portrait previews compact instead
                    # of stretching each generated video across the entire page.
                    result_columns = st.columns(2)
                    for number, clip in enumerate(generated, 1):
                        with result_columns[(number - 1) % 2]:
                            st.divider()
                            st.subheader(f"🎬 Clip {number}: {clip['title']}")
                            st.caption(f"{clip['start']:.1f}s to {clip['end']:.1f}s")
                            with st.expander("Preview clip", expanded=True):
                                st.video(clip["data"], width=240)
                                st.caption(clip["reason"])
                            st.download_button(
                                f"⬇️ Download Clip {number}",
                                data=clip["data"],
                                file_name=f"ClipAI_{number}.mp4",
                                mime="video/mp4",
                                key=f"download_clip_{number}",
                                use_container_width=True,
                            )
                            if clip["srt"]:
                                st.download_button(
                                    f"💬 Download subtitles {number} (.srt)",
                                    data=clip["srt"],
                                    file_name=f"ClipAI_{number}_subtitles.srt",
                                    mime="application/x-subrip",
                                    key=f"download_srt_{number}",
                                    use_container_width=True,
                                )

                    with st.expander("View the transcript"):
                        st.text_area("Transcript", transcript, height=220)

                except Exception as error:
                    status_code = getattr(error, "status_code", None)
                    error_text = str(error)
                    if status_code == 429 or "429" in error_text:
                        st.warning(
                            "Groq's free rate limit has been reached. No paid upgrade is needed; "
                            "wait for the provider's reset and try later."
                        )
                    elif status_code in (401, 403):
                        st.error("Groq API key issue. Check Streamlit Settings → Secrets.")
                    else:
                        st.error(f"ClipAI could not finish this job: {error_text[:700]}")

st.markdown(
    """
    <div id="how-it-works" class="clipai-section-kicker">Simple workflow</div>
    <div class="clipai-section-title">How ClipAI works</div>
    <div class="clipai-feature-grid">
      <div class="clipai-feature"><div class="clipai-feature-icon">📤</div><div class="clipai-feature-title">1. Add your video</div><div class="clipai-feature-copy">Start with a video file you own or have permission to process.</div></div>
      <div class="clipai-feature"><div class="clipai-feature-icon">🧠</div><div class="clipai-feature-title">2. Find strong moments</div><div class="clipai-feature-copy">AI reviews the spoken content and suggests distinct moments worth sharing.</div></div>
      <div class="clipai-feature"><div class="clipai-feature-icon">📲</div><div class="clipai-feature-title">3. Export your shorts</div><div class="clipai-feature-copy">Preview compact vertical clips, then download the MP4 and subtitle file.</div></div>
    </div>
    <div id="features" class="clipai-section-kicker">Built for creators</div>
    <div class="clipai-feature-grid">
      <div class="clipai-feature"><div class="clipai-feature-icon">🎯</div><div class="clipai-feature-title">Distinct clip ideas</div><div class="clipai-feature-copy">Prioritizes useful, surprising, entertaining, or memorable moments.</div></div>
      <div class="clipai-feature"><div class="clipai-feature-icon">💬</div><div class="clipai-feature-title">Captions when you need them</div><div class="clipai-feature-copy">Choose burned-in captions or download a separate subtitle file.</div></div>
      <div class="clipai-feature"><div class="clipai-feature-icon">⚡</div><div class="clipai-feature-title">Lightweight beta exports</div><div class="clipai-feature-copy">720 × 1280 exports help us test a faster workflow on limited free hosting.</div></div>
    </div>
    <div class="clipai-footer">
      <span>© 2026 ClipAI · Free beta. Features and limits may change during testing.</span>
      <span><a href="#top">Back to top ↑</a> · Please upload content you own or are authorized to use.</span>
    </div>
    """,
    unsafe_allow_html=True,
)
