
import streamlit as st
from groq import Groq
import subprocess
import tempfile
import shutil
import json
from pathlib import Path
import re

st.set_page_config(
    page_title="ClipAI - AI Video Clipper",
    page_icon="🎬",
    layout="wide"
)

st.title("🎬 ClipAI")
st.subheader("Turn Long Videos Into Short Videos With AI")
st.write("Upload a video. AI will find interesting moments and create clips.")

st.divider()

uploaded_file = st.file_uploader(
    "📁 Upload your video",
    type=["mp4", "mov", "mkv", "avi", "webm"]
)

if uploaded_file:
    st.success("Video uploaded successfully!")
    st.video(uploaded_file)

    st.write(
        "File size:",
        round(uploaded_file.size / (1024 * 1024), 2),
        "MB"
    )

    if st.button("✨ Generate AI Clips", type="primary"):

        if shutil.which("ffmpeg") is None:
            st.error("FFmpeg is not installed. Check packages.txt and redeploy.")

        else:
            try:
                api_key = st.secrets["GROQ_API_KEY"]
                client = Groq(api_key=api_key)

                with tempfile.TemporaryDirectory() as temp_dir:

                    folder = Path(temp_dir)
                    video_path = folder / "original_video.mp4"
                    audio_path = folder / "audio.mp3"

                    video_path.write_bytes(uploaded_file.getvalue())

                    with st.spinner("Preparing video audio..."):
                        duration_result = subprocess.run(
                            [
                                "ffprobe", "-v", "error",
                                "-show_entries", "format=duration",
                                "-of", "default=noprint_wrappers=1:nokey=1",
                                str(video_path)
                            ],
                            capture_output=True,
                            text=True,
                            timeout=60
                        )

                        if duration_result.returncode != 0:
                            raise Exception("Could not read video duration.")

                        video_duration = float(duration_result.stdout.strip())

                        if video_duration <= 0:
                            raise Exception("This video has no valid duration.")

                        audio_result = subprocess.run(
                            [
                                "ffmpeg", "-y",
                                "-i", str(video_path),
                                "-map", "0:a:0",
                                "-vn", "-ac", "1",
                                "-ar", "16000",
                                "-b:a", "48k",
                                str(audio_path)
                            ],
                            capture_output=True,
                            text=True,
                            timeout=300
                        )

                        if audio_result.returncode != 0:
                            raise Exception(
                                "Could not extract audio. Make sure your video has sound."
                            )

                    if audio_path.stat().st_size > 24 * 1024 * 1024:
                        raise Exception(
                            "Audio is too large for this first version. "
                            "Please try a shorter video."
                        )

                    with st.spinner("AI is understanding the video..."):
                        with open(audio_path, "rb") as audio_file:
                            transcript = client.audio.transcriptions.create(
                                file=audio_file,
                                model="whisper-large-v3-turbo",
                                response_format="verbose_json",
                                timestamp_granularities=["segment"],
                                temperature=0.0
                            )

                    segments = transcript.segments

                    if not segments:
                        raise Exception("AI could not find any speech in this video.")

                    transcript_lines = []

                    for seg in segments[:500]:
                        start = float(seg.start)
                        end = float(seg.end)
                        text = seg.text.strip()

                        if text:
                            transcript_lines.append(
                                f"[{start:.2f} - {end:.2f}] {text}"
                            )

                    transcript_text = "\n".join(transcript_lines)

                    if len(segments) > 500:
                        st.warning(
                            "This first version analyzes up to 500 speech segments."
                        )

                    st.success("Speech analysis completed!")

                    with st.spinner("AI is finding the best moments..."):
                        response = client.chat.completions.create(
                            model="openai/gpt-oss-20b",
                            messages=[
                                {
                                    "role": "system",
                                    "content": (
                                        "You are an expert short-form video editor. "
                                        "Find engaging, self-contained moments from "
                                        "the supplied timestamped transcript. "
                                        "Return only valid JSON with a clips array. "
                                        "Each clip must have start, end, title and reason. "
                                        "Choose at most 3 clips. Each clip should be "
                                        "between 20 and 60 seconds. Use only times "
                                        "supported by the transcript. Do not invent "
                                        "timestamps. Prefer strong hooks, useful ideas, "
                                        "surprising statements or emotional moments."
                                    )
                                },
                                {
                                    "role": "user",
                                    "content": (
                                        f"Video duration: {video_duration:.2f} seconds.\n"
                                        "Find up to 3 best short clips from this transcript:\n"
                                        + transcript_text
                                    )
                                }
                            ],
                            response_format={"type": "json_object"},
                            temperature=0.2,
                            max_completion_tokens=1200
                        )

                    answer = response.choices[0].message.content
                    clip_suggestions = json.loads(answer).get("clips", [])

                    if not clip_suggestions:
                        raise Exception("AI could not find suitable clips.")

                    generated_clips = []

                    for item in clip_suggestions[:3]:
                        start = max(0.0, float(item["start"]))
                        end = min(video_duration, float(item["end"]))
                        clip_length = end - start

                        if clip_length < 10 or clip_length > 65:
                            continue

                        title = str(item.get("title", "AI Clip"))[:70]
                        reason = str(item.get("reason", "Interesting moment"))

                        output_path = folder / f"clip_{len(generated_clips) + 1}.mp4"

                        cut_result = subprocess.run(
                            [
                                "ffmpeg", "-y",
                                "-ss", str(start),
                                "-i", str(video_path),
                                "-t", str(clip_length),
                                "-c:v", "libx264",
                                "-preset", "ultrafast",
                                "-crf", "23",
                                "-c:a", "aac",
                                "-movflags", "+faststart",
                                str(output_path)
                            ],
                            capture_output=True,
                            text=True,
                            timeout=180
                        )

                        if cut_result.returncode == 0 and output_path.exists():
                            generated_clips.append({
                                "title": title,
                                "reason": reason,
                                "start": start,
                                "end": end,
                                "data": output_path.read_bytes()
                            })

                    if not generated_clips:
                        raise Exception("AI selected moments but video cutting failed.")

                st.balloons()
                st.success(
                    f"Successfully created {len(generated_clips)} AI clips!"
                )

                for index, clip in enumerate(generated_clips, 1):
                    st.divider()
                    st.subheader(f"🎬 Clip {index}: {clip['title']}")
                    st.write(clip["reason"])
                    st.caption(
                        f"Time: {clip['start']:.1f}s – {clip['end']:.1f}s"
                    )
                    st.video(clip["data"])
                    st.download_button(
                        label=f"⬇️ Download Clip {index}",
                        data=clip["data"],
                        file_name=f"ClipAI_{index}.mp4",
                        mime="video/mp4",
                        key=f"download_{index}"
                    )

            except Exception as error:
                st.error(f"ClipAI error: {error}")

st.divider()
st.caption("ClipAI © 2026 | Create Shorts From Your Videos")
