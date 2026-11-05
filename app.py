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
MAX_CLIPS_PER_VIDEO = 2  # Keep beta workload small; raise to 4-5 after load testing.
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


def validate_suggestions(data, duration):
    if not isinstance(data, dict):
        raise ValueError("AI returned an invalid response.")

    suggestions = data.get("clips", [])
    if not isinstance(suggestions, list):
        raise ValueError("AI clip suggestions were not a list.")

    valid = []
    for item in suggestions[:MAX_CLIPS_PER_VIDEO]:
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


# ---------------- PAGE CONTENT ----------------
st.title("🎬 ClipAI")
st.subheader("Turn long videos into short clips with AI")
st.write("Upload a short video. ClipAI finds interesting moments and creates vertical 9:16 MP4 clips.")

burn_captions = st.checkbox("Burn captions into the video", value=True, help="Turn this off for faster rendering. An SRT subtitle file will still be available.")
layout_choice = st.radio(
    "Vertical video layout",
    ["Fill screen (may crop left/right edges)", "Fit full video (black bars)"],
    horizontal=True,
    help="Fill screen looks more like a Reel. Fit full video keeps the entire original frame visible."
)
st.caption("Fast beta export: 720 × 1280 at 24 FPS. Higher-resolution export can be added after performance testing.")

st.info(
    "Beta limits: up to 50 MB upload, 5-minute video, "
    "2 jobs per browser session per day, and 5 jobs total per app per day."
)

used_today = guard.get_usage()
st.caption(
    f"App-wide beta usage today: {used_today}/{MAX_JOBS_TOTAL_PER_APP_PER_DAY} jobs "
    "(UTC reset)."
)

# Session-level daily counter
current_day = datetime.now(timezone.utc).date().isoformat()
if st.session_state.get("clipai_session_day") != current_day:
    st.session_state["clipai_session_day"] = current_day
    st.session_state["clipai_session_jobs"] = 0

session_jobs = st.session_state.get("clipai_session_jobs", 0)
st.caption(
    f"This browser session: {session_jobs}/{MAX_JOBS_PER_SESSION_PER_DAY} jobs used today."
)

st.divider()

uploaded_file = st.file_uploader(
    "📁 Upload your video",
    type=["mp4", "mov", "mkv", "avi", "webm"],
    max_upload_size=MAX_UPLOAD_MB,
    help=f"Maximum file size: {MAX_UPLOAD_MB} MB. Maximum video length: 5 minutes.",
)

if uploaded_file is not None:
    file_size_mb = uploaded_file.size / (1024 * 1024)
    st.success(f"Video uploaded: {uploaded_file.name} ({file_size_mb:.1f} MB)")
    st.video(uploaded_file)

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
                                    timestamp_granularities=["segment"],
                                    temperature=0.0,
                                )

                        transcript = get_value(transcription, "text", "")
                        segments = get_value(transcription, "segments", []) or []
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
                                            "Find at most 2 self-contained, engaging moments. "
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
                                            "Select the strongest moments from this transcript:\n"
                                            + timestamped_transcript
                                        ),
                                    },
                                ],
                                response_format={"type": "json_object"},
                                temperature=0.2,
                                max_completion_tokens=1200,
                            )

                        ai_text = response.choices[0].message.content
                        if not ai_text:
                            raise ValueError("AI returned an empty clip-selection response.")
                        ai_data = json.loads(ai_text)
                        clips = validate_suggestions(ai_data, duration)
                        if not clips:
                            raise ValueError("AI could not find suitable 20-60 second clips in this video.")

                        generated = []
                        for number, clip in enumerate(clips, 1):
                            output_path = folder / f"clip_{number}_vertical.mp4"
                            subtitle_path = folder / f"clip_{number}.srt"
                            clip_length = clip["end"] - clip["start"]
                            has_subtitles = write_clip_subtitles(
                                segments,
                                clip["start"],
                                clip["end"],
                                subtitle_path,
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

                            if burn_captions and has_subtitles:
                                style = (
                                    "FontName=DejaVu Sans,FontSize=30,"
                                    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                                    "BorderStyle=1,Outline=2,Shadow=1,"
                                    "Alignment=2,MarginV=85"
                                )
                                video_filter += (
                                    f",subtitles={subtitle_path.as_posix()}:force_style='{style}'"
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
                                    if burn_captions and has_subtitles:
                                        output_path.unlink(missing_ok=True)
                                        st.warning(
                                            "Could not burn captions into this MP4 on the current server. "
                                            "The vertical clip will still be created, and you can download its SRT file."
                                        )
                                        encode_args[encode_args.index(video_filter)] = (
                                            video_filter.rsplit(",subtitles=", 1)[0]
                                        )
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
                    for number, clip in enumerate(generated, 1):
                        st.divider()
                        st.subheader(f"🎬 Clip {number}: {clip['title']}")
                        st.write(clip["reason"])
                        st.caption(f"{clip['start']:.1f}s to {clip['end']:.1f}s")
                        st.video(clip["data"])
                        st.download_button(
                            f"⬇️ Download Clip {number}",
                            data=clip["data"],
                            file_name=f"ClipAI_{number}.mp4",
                            mime="video/mp4",
                            key=f"download_clip_{number}",
                        )
                        if clip["srt"]:
                            st.download_button(
                                f"💬 Download subtitles {number} (.srt)",
                                data=clip["srt"],
                                file_name=f"ClipAI_{number}_subtitles.srt",
                                mime="application/x-subrip",
                                key=f"download_srt_{number}",
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

st.divider()
st.caption("© 2026 ClipAI | Free beta. Limits may change while we test.")
