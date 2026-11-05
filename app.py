
import streamlit as st

st.set_page_config(
    page_title="ClipAI - AI Video Clipper",
    page_icon="🎬",
    layout="wide"
)

st.title("🎬 ClipAI")
st.subheader("Turn Long Videos Into Short Viral Clips")

st.write(
    "Upload your long video and create engaging short videos with AI."
)

st.info("Free AI video clipping platform — Coming to life!")

video = st.file_uploader(
    "Upload your video",
    type=["mp4", "mov", "avi", "mkv"]
)

if video:
    st.success("Video uploaded successfully!")
    st.video(video)

    st.write("Video name:", video.name)
    st.write("File size:", round(video.size / (1024 * 1024), 2), "MB")

    st.warning("AI clip generation will be added in the next step.")
