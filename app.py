
import streamlit as st
from groq import Groq

st.set_page_config(
    page_title="ClipAI - AI Video Clipper",
    page_icon="🎬",
    layout="wide"
)

st.title("🎬 ClipAI")
st.subheader("Turn Long Videos Into Short Clips")
st.write("Upload your video and let AI understand its content.")

st.divider()

uploaded_file = st.file_uploader(
    "Upload video or audio",
    type=["mp4", "mp3", "m4a", "wav", "webm", "ogg", "flac"]
)

if uploaded_file:
    file_size = uploaded_file.size / (1024 * 1024)

    st.success(f"Uploaded: {uploaded_file.name}")
    st.write(f"File size: {file_size:.2f} MB")

    if uploaded_file.name.lower().endswith((".mp4", ".webm")):
        st.video(uploaded_file)
    else:
        st.audio(uploaded_file)

    if st.button("✨ Analyze with AI", type="primary"):

        if file_size > 25:
            st.error("For now, please upload a file under 25 MB.")

        else:
            try:
                api_key = st.secrets["GROQ_API_KEY"]
                client = Groq(api_key=api_key)

                with st.spinner("AI is analyzing your video..."):

                    result = client.audio.transcriptions.create(
                        file=(
                            uploaded_file.name,
                            uploaded_file.getvalue()
                        ),
                        model="whisper-large-v3-turbo",
                        response_format="verbose_json",
                        temperature=0.0
                    )

                st.success("AI transcription completed!")

                transcript = result.text

                st.subheader("📝 Video Transcript")
                st.text_area(
                    "Your video's spoken content",
                    value=transcript,
                    height=300
                )

                st.download_button(
                    "⬇️ Download Transcript",
                    data=transcript,
                    file_name="clipai_transcript.txt",
                    mime="text/plain"
                )

            except Exception as e:
                st.error(f"Something went wrong: {e}")

st.divider()
st.caption("ClipAI © 2026 | AI-powered video tools")
