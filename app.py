
import streamlit as st
from groq import Groq
import subprocess
import tempfile
import shutil
import json
from pathlib import Path

# ---------------- PAGE SETTINGS ----------------

st.set_page_config(
    page_title="ClipAI - AI Video Clipper",
    page_icon="🎬",
    layout="wide"
)

st.title("🎬 ClipAI")
st.subheader("Turn Long Videos Into Short Viral Clips")
st.write("Upload your video and let AI find its best moments.")

st.divider()

# ---------------- HELPER FUNCTIONS ----------------

def get_value(item, key, default=None):
    """Read a field from either a dictionary or an object."""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def run_command(command, timeout=300):
    """Run FFmpeg or FFprobe safely."""
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout
    )

    if result.returncode != 0:
        error = result.stderr[-1500:] if result.stderr else "Unknown media error."
        raise RuntimeError(error)

    return result


def get_video_duration(video_path):
    result = run_command([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path)
    ], timeout=60)

    return float(result.stdout.strip())


def clean_clip_data(data, duration):
    """Validate AI-generated clip timestamps."""
    if not isinstance(data, dict):
        raise ValueError("AI returned an invalid response.")

    suggestions = data.get("clips", [])

    if not isinstance(suggestions, list):
        raise ValueError("AI clip suggestions are invalid.")

    valid_clips = []

    for item in suggestions[:3]:
        try:
            start = float(item["start"])
            end = float(item["end"])

            if start < 0 or end > duration or end <= start:
                continue

            length = end - start

            if length < 10 or length > 65:
                continue

            valid_clips.append({
                "start": start,
                "end": end,
                "title": str(item.get("title", "AI Clip"))[:70],
                "reason": str(item.get("reason", "Interesting moment"))[:250]
            })

        except (KeyError, TypeError, ValueError):
            continue

    return valid_clips


# ---------------- VIDEO UPLOAD ----------------

uploaded_file = st.file_uploader(
    "📁 Upload your video",
    type=["mp4", "mov", "mkv", "avi", "webm"]
)

if uploaded_file:

    file_size = uploaded_file.size / (1024 * 1024)

    st.success("Video uploaded successfully!")

    st.video(uploaded_file)

    st.caption(f"File size: {file_size:.2f} MB")

    if file_size > 150:
        st.error("Please use a video smaller than 150 MB.")

    else:

        if st.button("✨ Generate AI Clips", type="primary"):

            # Check FFmpeg installation

            if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
                st.error(
                    "FFmpeg is missing. Check packages.txt "
                    "and redeploy your website."
                )

            else:

                try:
                    # Read API key securely

                    api_key = st.secrets["GROQ_API_KEY"]
                    client = Groq(api_key=api_key)

                    with tempfile.TemporaryDirectory() as temp:

                        folder = Path(temp)

                        suffix = Path(uploaded_file.name).suffix.lower()
                        video_path = folder / f"input{suffix or '.mp4'}"
                        audio_path = folder / "audio.mp3"

                        video_path.write_bytes(
                            uploaded_file.getvalue()
                        )

                        # -------- STEP 1: VIDEO ANALYSIS --------

                        with st.spinner("Analyzing your video..."):

                            duration = get_video_duration(video_path)

                            if duration <= 0:
                                raise ValueError("Invalid video duration.")

                            if duration > 600:
                                raise ValueError(
                                    "First version supports videos up to "
                                    "10 minutes. Try a shorter video."
                                )

                            # -------- STEP 2: AUDIO EXTRACTION --------

                            with st.spinner("Extracting video audio..."):

                                run_command([
                                    "ffmpeg",
                                    "-y",
                                    "-i", str(video_path),
                                    "-map", "0:a:0",
                                    "-vn",
                                    "-ac", "1",
                                    "-ar", "16000",
                                    "-b:a", "48k",
                                    str(audio_path)
                                ], timeout=300)

                            if not audio_path.exists():
                                raise RuntimeError("Audio extraction failed.")

                            audio_size = audio_path.stat().st_size

                            if audio_size > 24 * 1024 * 1024:
                                raise ValueError(
                                    "Audio exceeds the current 24 MB "
                                    "processing limit. Try a shorter video."
                                )

                            # -------- STEP 3: GROQ TRANSCRIPTION --------

                            with st.spinner(
                                "🤖 AI is understanding your video..."
                            ):

                                with open(audio_path, "rb") as audio_file:

                                    transcription = (
                                        client.audio.transcriptions.create(
                                            file=audio_file,
                                            model="whisper-large-v3-turbo",
                                            response_format="verbose_json",
                                            timestamp_granularities=["segment"],
                                            temperature=0.0
                                        )
                                    )

                            transcript = get_value(
                                transcription, "text", ""
                            )

                            segments = get_value(
                                transcription, "segments", []
                            )

                            if not transcript or not segments:
                                raise ValueError(
                                    "No clear speech was detected. "
                                    "Try a video with audible speech."
                                )

                            # -------- STEP 4: READ TIMESTAMPS --------

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
                                        transcript_lines.append(
                                            f"[{start:.2f}-{end:.2f}] {words}"
                                        )

                                except (TypeError, ValueError):
                                    continue

                            if not transcript_lines:
                                raise ValueError(
                                    "AI could not find valid timestamps."
                                )

                            timestamped_transcript = "\n".join(
                                transcript_lines
                            )

                            # -------- STEP 5: SELECT BEST MOMENTS --------

                            with st.spinner(
                                "✨ Finding the most interesting moments..."
                            ):

                                response = client.chat.completions.create(

                                    model="openai/gpt-oss-20b",

                                    messages=[
                                        {
                                            "role": "system",
                                            "content": """
You are an expert short-video editor.

Analyze the timestamped video transcript.

Find up to 3 interesting, self-contained moments.

Prioritize:
- Strong opening hooks
- Interesting facts
- Useful information
- Surprising moments
- Emotional or entertaining statements

Rules:
1. Each clip should be 20 to 60 seconds.
2. Use timestamps from the transcript.
3. Never invent timestamps.
4. Keep start and end within the video duration.
5. Do not select repetitive moments.

Return valid JSON only:

{
  "clips": [
    {
      "start": 10.0,
      "end": 45.0,
      "title": "Interesting Moment",
      "reason": "Why this moment is worth watching"
    }
  ]
}
"""
                                        },
                                        {
                                            "role": "user",
                                            "content": (
                                                f"Video duration: {duration:.2f} seconds.\n\n"
                                                + timestamped_transcript
                                            )
                                        }
                                    ],

                                    response_format={
                                        "type": "json_object"
                                    },

                                    temperature=0.2,
                                    max_completion_tokens=2000
                                )

                            ai_text = response.choices[0].message.content

                            if not ai_text:
                                raise ValueError("AI returned an empty response.")

                            ai_data = json.loads(ai_text)

                            clips = clean_clip_data(
                                ai_data,
                                duration
                            )

                            if not clips:
                                raise ValueError(
                                    "AI could not find suitable clips. "
                                    "Try another video with clearer speech."
                                )

                            # -------- STEP 6: CUT VIDEO CLIPS --------

                            generated_clips = []

                            for number, clip in enumerate(clips, 1):

                                output_path = (
                                    folder / f"clip_{number}.mp4"
                                )

                                clip_length = (
                                    clip["end"] - clip["start"]
                                )

                                with st.spinner(
                                    f"Creating clip {number} of {len(clips)}..."
                                ):

                                    run_command([
                                        "ffmpeg",
                                        "-y",
                                        "-ss", str(clip["start"]),
                                        "-i", str(video_path),
                                        "-t", str(clip_length),
                                        "-map", "0:v:0",
                                        "-map", "0:a:0?",
                                        "-c:v", "libx264",
                                        "-preset", "ultrafast",
                                        "-crf", "23",
                                        "-c:a", "aac",
                                        "-b:a", "128k",
                                        "-movflags", "+faststart",
                                        "-pix_fmt", "yuv420p",
                                        str(output_path)
                                    ], timeout=180)

                                if output_path.exists() and output_path.stat().st_size > 0:

                                    generated_clips.append({
                                        "title": clip["title"],
                                        "reason": clip["reason"],
                                        "start": clip["start"],
                                        "end": clip["end"],
                                        "data": output_path.read_bytes()
                                    })

                            if not generated_clips:
                                raise RuntimeError(
                                    "AI selected moments, but video cutting failed."
                                )

                    # -------- STEP 7: SHOW RESULTS --------

                    st.balloons()

                    st.success(
                        f"Success! {len(generated_clips)} clips created."
                    )

                    for number, clip in enumerate(generated_clips, 1):

                        st.divider()

                        st.subheader(
                            f"🎬 Clip {number}: {clip['title']}"
                        )

                        st.write(clip["reason"])

                        st.caption(
                            f"{clip['start']:.1f}s – {clip['end']:.1f}s"
                        )

                        st.video(clip["data"])

                        st.download_button(
                            label=f"⬇️ Download Clip {number}",
                            data=clip["data"],
                            file_name=f"ClipAI_{number}.mp4",
                            mime="video/mp4",
                            key=f"download_clip_{number}"
                        )

                    with st.expander("View original AI transcript"):
                        st.text_area(
                            "Transcript",
                            transcript,
                            height=250
                        )

                except KeyError as error:
                    st.error(
                        "GROQ_API_KEY is missing. Check Streamlit "
                        "Settings → Secrets."
                    )

                except Exception as error:
                    st.error(f"ClipAI processing failed: {error}")


# ---------------- FOOTER ----------------

st.divider()

st.caption("© 2026 ClipAI | AI-powered video clipping")
