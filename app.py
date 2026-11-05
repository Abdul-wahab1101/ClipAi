import hashlib
import html
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import streamlit as st
from groq import Groq

# ---------------- SETTINGS: conservative beta limits ----------------
MAX_UPLOAD_MB = 150              # Keep below Streamlit Community Cloud's default per-file cap.
MAX_VIDEO_SECONDS = 1800         # 30 minutes per video in this beta.
MAX_AUDIO_MB = 24                # Keep below Groq's documented 25 MB free-tier audio limit.
MAX_TRANSCRIPT_CHARS_FOR_AI = 24000  # Keep model input manageable on the free tier.
MAX_CLIPS_PER_VIDEO = 5  # User can choose 2-5 clips; keep beta workload controlled.
OUTPUT_WIDTH = 720
OUTPUT_HEIGHT = 1280
OUTPUT_FPS = 24
MAX_JOBS_PER_USER_PER_DAY = 2
MAX_JOBS_TOTAL_PER_APP_PER_DAY = 5  # Keep this beta cap until public launch tests pass.

st.set_page_config(
    page_title="ClipAI | AI Video Clipper",
    page_icon="🎬",  # Browser-tab favicon for the Streamlit beta.
    layout="wide",
    menu_items={
        "About": "ClipAI helps creators turn authorized long videos into short vertical clips. Free beta; limits may change.",
    },
)


# ---------------- GOOGLE SIGN-IN + PERSISTENT SUPABASE QUOTAS ----------------
SUPABASE_URL = str(st.secrets.get("SUPABASE_URL", "")).rstrip("/")
SUPABASE_SECRET_KEY = str(st.secrets.get("SUPABASE_SECRET_KEY", ""))
CONTACT_EMAIL = str(st.secrets.get("CONTACT_EMAIL", "")).strip()


def supabase_rpc(function_name, payload):
    """Call a Supabase Postgres function from this server-side Streamlit app."""
    if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
        raise RuntimeError("Supabase is not configured. Check Streamlit Secrets.")

    url = f"{SUPABASE_URL}/rest/v1/rpc/{function_name}"
    headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    # New sb_secret keys are API keys, not JWTs, so they must not be sent as Bearer tokens.
    # The legacy service_role JWT remains supported as a compatibility option.
    if SUPABASE_SECRET_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {SUPABASE_SECRET_KEY}"

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(
            f"Usage database returned HTTP {exc.code}. Check the Supabase SQL setup and Secret key. {detail}"
        ) from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Could not reach the usage database. Please try again later.") from exc


def get_daily_usage(user_id, usage_date):
    result = supabase_rpc(
        "get_clipai_usage",
        {"p_user_id": user_id, "p_usage_date": usage_date},
    )
    row = result[0] if isinstance(result, list) and result else result
    if not isinstance(row, dict):
        raise RuntimeError("The usage database returned an unexpected response.")
    return {
        "user_jobs": int(row.get("user_jobs", 0)),
        "app_jobs": int(row.get("app_jobs", 0)),
    }


def reserve_daily_job(user_id, usage_date):
    result = supabase_rpc(
        "reserve_clipai_job",
        {
            "p_user_id": user_id,
            "p_usage_date": usage_date,
            "p_user_limit": MAX_JOBS_PER_USER_PER_DAY,
            "p_app_limit": MAX_JOBS_TOTAL_PER_APP_PER_DAY,
        },
    )
    row = result[0] if isinstance(result, list) and result else result
    if not isinstance(row, dict):
        raise RuntimeError("The usage database returned an unexpected reservation response.")
    return {
        "allowed": bool(row.get("allowed", False)),
        "user_jobs": int(row.get("user_jobs", 0)),
        "app_jobs": int(row.get("app_jobs", 0)),
        "reason": str(row.get("reason", "unknown")),
    }


def render_policy_page(page_name):
    """Render public policy pages without asking visitors to sign in first."""
    st.markdown(
        """
        <style>
        [data-testid="stAppViewContainer"] {background:#0b0d14;}
        [data-testid="stMainBlockContainer"] {max-width:900px;padding-top:2rem;padding-bottom:3rem;}
        h1,h2,h3 {letter-spacing:-.025em;}
        .policy-note {color:#b9c2d2;background:#151d2c;border:1px solid #30394a;padding:13px 16px;border-radius:12px;}
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("🎬 **ClipAI** · Beta service")
    if page_name == "Privacy Policy":
        st.title("Privacy Policy")
        st.caption("Effective date: 5 November 2026")
        st.markdown("""
### 1. Information ClipAI handles
When you sign in, Google provides an account identifier and basic profile information such as your name and email. ClipAI uses the Google subject identifier to create a one-way SHA-256 pseudonymous ID for quota records. The quota database stores that pseudonymous ID, the UTC usage date, and the number of jobs used; it does not store your raw Google subject, email, video, transcript, or generated clips.

When you use the service, ClipAI temporarily handles the video file you upload or the direct HTTPS media URL you submit, plus extracted audio and the transcript needed to create clips. The current beta does not provide a permanent media library. Temporary processing files are created while a job runs, and results are provided in the active app session for download.

### 2. How third-party services are involved
- **Google:** authenticates your account through OpenID Connect. Streamlit uses an authentication cookie so you can remain signed in; you can sign out from the app sidebar.
- **Groq:** receives extracted audio for transcription and transcript text for clip selection. Groq says inference inputs and outputs are not retained by default, but limited reliability or abuse-monitoring logs may retain data for up to 30 days unless Zero Data Retention is enabled. Review [Groq's data documentation](https://console.groq.com/docs/your-data).
- **Supabase:** stores the pseudonymous daily usage counters described above. Its free project may pause after a period of inactivity; database availability can therefore affect quota checks.
- **Streamlit Community Cloud:** hosts the app and handles app sessions and uploaded files while you use it.

Each provider processes information under its own terms and privacy documents. Do not upload confidential, highly sensitive, or private material in this beta.

### 3. Why information is used
Information is used only to authenticate users, operate the video-clipping workflow, enforce daily usage limits, protect the service from abuse, and troubleshoot errors.

### 4. Retention
Daily quota records are kept in the ClipAI database while needed to operate and protect the beta. Source videos, extracted audio, transcripts, and generated clips are not written to the quota tables. The app is not a long-term file-hosting or backup service.

### 5. Your responsibilities and choices
Only upload videos you own or are authorized to edit and submit to AI services. You can stop using ClipAI and sign out at any time. For a privacy or quota-record deletion request, contact the site operator using the contact details shown below.

### 6. Contact
""")
        if CONTACT_EMAIL:
            st.markdown(f"Privacy questions: **[{CONTACT_EMAIL}](mailto:{CONTACT_EMAIL})**")
        else:
            st.warning("The site owner must add CONTACT_EMAIL in Streamlit Secrets before public launch.")
        st.caption("This is a beta policy draft, not legal advice. The operator should review it for the laws and regions where ClipAI will be offered before public launch.")
    else:
        st.title("Terms of Service")
        st.caption("Effective date: 5 November 2026")
        st.markdown("""
### 1. Beta service
ClipAI is an experimental video-clipping service. Features, processing capacity, availability, and daily limits may change while the service is tested. Jobs can fail because of file formats, video quality, provider limits, or hosting constraints.

### 2. Your content and permission
You must own the video or have permission and all necessary rights to upload it, provide a media link, transcribe it, edit it, and create derivative clips. You are responsible for complying with copyright law and the terms of YouTube, TikTok, Instagram, and any other platform you use. ClipAI does not authorize downloading or copying videos from third-party platforms.

### 3. Acceptable use
Do not use ClipAI to infringe copyright or privacy rights, distribute unlawful content, bypass platform restrictions, attack the service, overload the service, or evade usage limits. Do not share your account or attempt to bypass per-user quotas with multiple accounts.

### 4. AI output
AI-selected moments, transcripts, captions, titles, and clip boundaries can be inaccurate. You must review each result before publishing. ClipAI does not guarantee views, virality, engagement, revenue, or fitness for a particular purpose.

### 5. Limits and availability
The beta currently accepts videos up to 30 minutes and 150 MB, and allows up to 2 processing jobs per signed-in account per UTC day and 5 jobs across the entire app per UTC day. A job is counted when processing is reserved, even if the job later fails. Limits may be adjusted to protect free-tier provider and hosting capacity. No paid service or unlimited processing is promised.

### 6. Third-party services
ClipAI relies on Google sign-in, Groq AI services, Supabase database services, and Streamlit hosting. Those services may be unavailable or subject to their own terms and limits.

### 7. Changes and suspension
The operator may change, pause, or discontinue this beta to protect the service, comply with law, or respond to provider restrictions. Continued use after an updated version is posted means you accept the updated terms.

### 8. Contact
""")
        if CONTACT_EMAIL:
            st.markdown(f"Terms questions: **[{CONTACT_EMAIL}](mailto:{CONTACT_EMAIL})**")
        else:
            st.warning("The site owner must add CONTACT_EMAIL in Streamlit Secrets before public launch.")
        st.caption("This is a beta terms draft, not legal advice. Have it reviewed for the laws and regions where ClipAI will be offered before public launch.")


# Lightweight app routes: keep policy pages accessible before sign-in and
# provide an in-app not-found page for unsupported ?page= values.
PAGE_OPTIONS = ["Create clips", "Privacy Policy", "Terms of Service"]
requested_page = st.query_params.get("page", "Create clips")

if requested_page not in PAGE_OPTIONS:
    st.title("404 — Page not found")
    st.write("This ClipAI page does not exist or may have moved.")
    st.link_button("Go to ClipAI home", "?page=Create%20clips")
    st.stop()

page_selection = st.sidebar.radio(
    "ClipAI menu",
    PAGE_OPTIONS,
    index=PAGE_OPTIONS.index(requested_page),
)
if page_selection != "Create clips":
    render_policy_page(page_selection)
    st.markdown(
        "---\n[🎬 Back to ClipAI](?page=Create%20clips) · "
        "[Privacy Policy](?page=Privacy%20Policy) · "
        "[Terms of Service](?page=Terms%20of%20Service)"
    )
    st.stop()

# Require both Google OIDC and the durable usage database before processing is enabled.
_auth_settings = st.secrets.get("auth", {})
_google_settings = _auth_settings.get("google", {}) if hasattr(_auth_settings, "get") else {}
_required_auth_values = (
    _auth_settings.get("redirect_uri", "") if hasattr(_auth_settings, "get") else "",
    _auth_settings.get("cookie_secret", "") if hasattr(_auth_settings, "get") else "",
    _google_settings.get("client_id", "") if hasattr(_google_settings, "get") else "",
    _google_settings.get("client_secret", "") if hasattr(_google_settings, "get") else "",
)
if not all(_required_auth_values) or not _google_settings:
    st.title("ClipAI account setup")
    st.warning("Google sign-in is not configured yet. The owner must finish the Google OAuth setup in Streamlit Secrets before users can process videos.")
    st.markdown("1. Create a Google OAuth **Web application** client.\n2. Add your Streamlit app's `/oauth2callback` URL as an authorized redirect URI.\n3. Add the `[auth]` and `[auth.google]` entries from the setup guide to Streamlit Secrets.")
    st.stop()

if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    st.title("ClipAI database setup")
    st.warning("The durable usage database is not configured yet. Add SUPABASE_URL and SUPABASE_SECRET_KEY to Streamlit Secrets.")
    st.stop()

try:
    signed_in = bool(st.user.is_logged_in)
except Exception:
    signed_in = False

if not signed_in:
    st.title("Welcome to ClipAI")
    st.markdown("Sign in with Google to create clips and keep your daily usage linked to your account.")
    st.button("Continue with Google", on_click=st.login, args=["google"], type="primary")
    st.caption("Your videos are not saved as a permanent library by this beta. Read the Privacy Policy and Terms of Service from the sidebar before continuing.")
    st.stop()

try:
    _google_subject = str(st.user.get("sub", "")).strip()
    _google_email = str(st.user.get("email", "")).strip()
    _display_name = str(st.user.get("name", "Creator")).strip() or "Creator"
except Exception:
    _google_subject, _google_email, _display_name = "", "", "Creator"

if not _google_subject:
    st.error("Google did not provide a stable account identifier. Please sign out and sign in again.")
    st.stop()

# Store only a pseudonymous hash of Google's stable subject identifier in Supabase.
CURRENT_USER_ID = hashlib.sha256(
    ("clipai-quota-v1:" + _google_subject).encode("utf-8")
).hexdigest()
CURRENT_UTC_DAY = datetime.now(timezone.utc).date().isoformat()

st.sidebar.success(f"Signed in as {_display_name}")
if _google_email:
    st.sidebar.caption(_google_email)
if st.sidebar.button("Log out"):
    st.logout()
st.sidebar.caption("Beta quota: 2 jobs per account per day; 5 jobs shared across ClipAI per day (UTC).")

try:
    CURRENT_USAGE = get_daily_usage(CURRENT_USER_ID, CURRENT_UTC_DAY)
except Exception as usage_error:
    st.error("ClipAI cannot verify your daily quota, so processing is temporarily disabled to protect the AI key.")
    st.caption(str(usage_error)[:500])
    st.stop()

def get_value(item, key, default=None):
    """Read Groq response fields from either dicts or SDK objects."""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def extract_youtube_video_id(value):
    """Return an 11-character YouTube video ID for common video URL formats."""
    try:
        parsed = urlparse(value.strip())
        host = (parsed.hostname or "").lower()
        allowed_hosts = {
            "youtube.com", "www.youtube.com", "m.youtube.com",
            "youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
        }
        if parsed.scheme not in ("https", "http") or host not in allowed_hosts:
            return None

        video_id = None
        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/")[0]
        elif parsed.path.startswith("/shorts/"):
            video_id = parsed.path.split("/", 2)[2].split("/")[0]
        elif parsed.path.startswith("/embed/"):
            video_id = parsed.path.split("/", 2)[2].split("/")[0]
        else:
            video_id = parse_qs(parsed.query).get("v", [None])[0]

        if video_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return video_id
    except (AttributeError, IndexError, ValueError):
        return None
    return None


class MemoryVideo:
    """Small UploadedFile-compatible wrapper for a fetched direct video URL."""
    def __init__(self, name, data):
        self.name = name
        self._data = data
        self.size = len(data)

    def getvalue(self):
        return self._data


def validate_public_https_url(value):
    """Reject non-HTTPS, local, private, and otherwise unsafe URL targets."""
    try:
        parsed = urlparse(value.strip())
        if parsed.scheme.lower() != "https":
            raise ValueError("Use a direct HTTPS video-file link.")
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("This link is not a valid public HTTPS URL.")
        if parsed.port not in (None, 443):
            raise ValueError("Only standard HTTPS links on port 443 are supported.")

        host = parsed.hostname.rstrip(".").lower()
        if host in {"localhost", "metadata.google.internal"} or host.endswith((".local", ".internal", ".localhost")):
            raise ValueError("Private or local network links are not supported.")
        if host in {
            "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
            "youtube-nocookie.com", "www.youtube-nocookie.com",
        }:
            raise ValueError(
                "YouTube watch links can be previewed but not fetched as media here. "
                "For your own channel video, download it from YouTube Studio and upload the file."
            )

        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_global:
                raise ValueError("Private or local network links are not supported.")
        except ValueError as exc:
            # If this is not a literal IP address, resolve the hostname and reject
            # it if any returned address is not globally routable.
            try:
                ipaddress.ip_address(host)
            except ValueError:
                try:
                    records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
                except OSError:
                    raise ValueError("Could not resolve this public video host.") from None
                addresses = {record[4][0].split("%", 1)[0] for record in records}
                if not addresses or any(not ipaddress.ip_address(addr).is_global for addr in addresses):
                    raise ValueError("This link does not point to a public internet host.") from None
            else:
                raise exc
        return parsed
    except (AttributeError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("Please enter a valid HTTPS video-file link.") from None


def download_direct_video(value, max_bytes):
    """Fetch a direct video file URL, refusing redirects and oversized responses.

    This beta helper is intended only for public, direct media-file URLs that the
    user owns or is authorized to process. Platform watch pages are not downloaded.
    """
    parsed = validate_public_https_url(value)
    extension = Path(parsed.path).suffix.lower()
    allowed_extensions = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
    if extension and extension not in allowed_extensions:
        raise ValueError("The URL must point directly to an MP4, MOV, WebM, MKV, or AVI file.")

    class RejectRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(RejectRedirect)
    request = urllib.request.Request(
        value.strip(),
        headers={"User-Agent": "ClipAI-beta/1.0", "Accept": "video/*,application/octet-stream;q=0.8"},
        method="GET",
    )
    try:
        response = opener.open(request, timeout=20)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ValueError("This host redirects the download. Use a direct, non-redirecting file URL.") from None
        raise ValueError(f"The video host returned HTTP {exc.code}. Check that the file link is public.") from None
    except Exception:
        raise ValueError("Could not reach this video link. Check that it is a direct public file URL.") from None

    with response:
        content_type = response.headers.get_content_type().lower()
        type_to_ext = {
            "video/mp4": ".mp4",
            "video/quicktime": ".mov",
            "video/webm": ".webm",
            "video/x-matroska": ".mkv",
            "video/x-msvideo": ".avi",
            "application/octet-stream": extension or ".mp4",
        }
        if content_type not in type_to_ext:
            raise ValueError("The URL did not return a supported video file.")

        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    raise ValueError(f"The linked video is larger than the {max_bytes // (1024 * 1024)} MB beta limit.")
            except ValueError as exc:
                if "larger than" in str(exc):
                    raise

        chunks = []
        total = 0
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"The linked video is larger than the {max_bytes // (1024 * 1024)} MB beta limit.")
            chunks.append(chunk)

    if not total:
        raise ValueError("The video file from this link is empty.")

    final_extension = extension or type_to_ext[content_type]
    filename = Path(parsed.path).name or f"linked_video{final_extension}"
    if Path(filename).suffix.lower() not in allowed_extensions:
        filename = f"linked_video{final_extension}"
    return filename, b"".join(chunks)


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
    :root {
      --clip-bg:#090d18; --clip-panel:#111827; --clip-panel-2:#172235;
      --clip-line:rgba(207,220,255,.13); --clip-muted:#a7b3c8;
      --clip-accent:#a78bfa; --clip-accent-2:#5eead4; --clip-text:#f6f7fb;
    }
    html { scroll-behavior:smooth; }
    [data-testid="stAppViewContainer"] {
      background:
        radial-gradient(ellipse at 12% 0%,rgba(109,84,220,.20),transparent 34%),
        radial-gradient(ellipse at 92% 18%,rgba(40,183,174,.12),transparent 30%),
        linear-gradient(180deg,#0a0e19 0%,#0b1020 48%,#090d18 100%);
      color:var(--clip-text);
    }
    [data-testid="stHeader"] { background:rgba(9,13,24,.72); }
    [data-testid="stMainBlockContainer"] { max-width:1120px; padding:1.2rem 2rem 3.5rem; }
    [data-testid="stSidebar"] { background:linear-gradient(180deg,#101729,#0d1422); border-right:1px solid var(--clip-line); }
    h1,h2,h3 { letter-spacing:-.04em; color:#f7f8fc; }
    p,li { color:#c4ccda; }
    .clipai-nav {
      display:flex; align-items:center; justify-content:space-between; gap:18px;
      padding:13px 2px 21px; border-bottom:1px solid var(--clip-line); margin-bottom:27px;
    }
    .clipai-brand { color:#fff !important; text-decoration:none !important; font-size:1.52rem;
      font-weight:850; letter-spacing:-.055em; }
    .clipai-brand span { background:linear-gradient(90deg,#b8a5ff,#67e8d4); -webkit-background-clip:text; background-clip:text; color:transparent; }
    .clipai-navlinks { display:flex; align-items:center; gap:25px; flex-wrap:wrap; }
    .clipai-navlinks a { color:#c6d0e1 !important; text-decoration:none !important; font-size:.88rem; font-weight:600; transition:color .2s ease; }
    .clipai-navlinks a:hover { color:#fff !important; }
    .clipai-pill { border:1px solid rgba(167,139,250,.40); color:#d5caff; background:rgba(167,139,250,.12);
      border-radius:999px; padding:7px 12px; font-size:.72rem; font-weight:800; letter-spacing:.07em; white-space:nowrap; }
    .clipai-hero {
      display:grid; grid-template-columns:1.28fr .82fr; gap:30px; align-items:center;
      padding:clamp(24px,4vw,42px); border:1px solid rgba(210,220,255,.15); border-radius:27px;
      background:radial-gradient(ellipse at 82% 0%,rgba(167,139,250,.24),transparent 43%),
                 radial-gradient(ellipse at 2% 100%,rgba(94,234,212,.12),transparent 42%),
                 linear-gradient(135deg,rgba(24,34,57,.98),rgba(13,19,33,.98));
      box-shadow:0 24px 70px rgba(0,0,0,.24),inset 0 1px 0 rgba(255,255,255,.045);
      margin-bottom:35px; position:relative; overflow:hidden;
    }
    .clipai-eyebrow { color:#c8baff; text-transform:uppercase; letter-spacing:.16em; font-size:.70rem; font-weight:850; }
    .clipai-hero h1 { color:#fff; font-size:clamp(2.35rem,5.1vw,4.05rem); line-height:1.02; margin:.72rem 0 .95rem; font-weight:850; }
    .clipai-hero h1 span { background:linear-gradient(95deg,#c5b8ff 8%,#73ead8 95%); -webkit-background-clip:text; background-clip:text; color:transparent; }
    .clipai-hero p { color:#b9c5d8; font-size:1.02rem; line-height:1.72; max-width:630px; margin-bottom:0; }
    .clipai-hero-card { border:1px solid rgba(220,228,255,.16); border-radius:20px; padding:22px;
      background:linear-gradient(150deg,rgba(8,13,25,.77),rgba(21,29,48,.72)); box-shadow:0 14px 36px rgba(0,0,0,.18); }
    .clipai-mini-label { color:#91a0ba; text-transform:uppercase; font-size:.68rem; letter-spacing:.14em; font-weight:800; }
    .clipai-mini-title { color:#fff; font-weight:800; font-size:1.22rem; margin:9px 0 7px; letter-spacing:-.025em; }
    .clipai-mini-copy { color:#b0bdd1; font-size:.84rem; line-height:1.62; }
    .clipai-statrow { display:flex; gap:9px; flex-wrap:wrap; margin-top:19px; }
    .clipai-stat { border:1px solid rgba(220,228,255,.13); background:rgba(255,255,255,.055); border-radius:11px;
      padding:9px 12px; color:#e0e7f5; font-size:.78rem; font-weight:650; }
    .clipai-section-kicker { color:#b7a6ff; font-size:.70rem; font-weight:850; letter-spacing:.16em; text-transform:uppercase; }
    .clipai-section-title { color:#fff; font-weight:850; font-size:1.8rem; letter-spacing:-.05em; margin:5px 0 5px; }
    .clipai-section-copy { color:var(--clip-muted); font-size:.92rem; line-height:1.55; margin-bottom:17px; }
    .clipai-feature-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:15px; margin:14px 0 31px; }
    .clipai-feature { background:linear-gradient(145deg,#141e30,#101827); border:1px solid var(--clip-line); border-radius:17px; padding:19px;
      box-shadow:0 10px 28px rgba(0,0,0,.12); transition:transform .18s ease,border-color .18s ease; }
    .clipai-feature:hover { transform:translateY(-3px); border-color:rgba(167,139,250,.42); }
    .clipai-feature-icon { font-size:1.45rem; margin-bottom:11px; }
    .clipai-feature-title { color:#f5f7fc; font-weight:800; margin-bottom:6px; }
    .clipai-feature-copy { color:#aebbd0; font-size:.83rem; line-height:1.58; }
    .clipai-limit { color:#d1e9e6; background:linear-gradient(100deg,rgba(94,234,212,.09),rgba(167,139,250,.08));
      border:1px solid rgba(94,234,212,.22); border-radius:13px; padding:13px 16px; font-size:.84rem; margin:15px 0; }
    .clipai-footer { border-top:1px solid var(--clip-line); padding-top:20px; margin-top:33px;
      color:#94a3b8; font-size:.79rem; display:flex; justify-content:space-between; gap:14px; flex-wrap:wrap; }
    .clipai-footer a { color:#c7bdff !important; text-decoration:none !important; font-weight:650; }
    .clipai-footer a:hover { color:#fff !important; }
    div[data-testid="stFileUploader"] section { background:rgba(20,29,47,.72); border:1px dashed rgba(167,139,250,.45); border-radius:17px; padding:18px; }
    div[data-testid="stFileUploader"] section:hover { border-color:#75e4d5; }
    div[data-testid="stTextInput"] input,div[data-testid="stTextArea"] textarea {
      background:#111a2a; color:#f4f6fb; border:1px solid rgba(190,205,235,.18); border-radius:11px;
    }
    div[data-testid="stTextInput"] input:focus,div[data-testid="stTextArea"] textarea:focus { border-color:#a78bfa; box-shadow:0 0 0 1px #a78bfa; }
    div[data-testid="stButton"] button,div[data-testid="stDownloadButton"] button {
      border-radius:12px; border:1px solid rgba(190,205,235,.18); font-weight:750; min-height:2.7rem; transition:all .18s ease;
    }
    div[data-testid="stButton"] button[kind="primary"] { color:#111326; background:linear-gradient(100deg,#b7a5ff,#71e5d5); border:0; box-shadow:0 8px 24px rgba(117,105,220,.24); }
    div[data-testid="stButton"] button:hover,div[data-testid="stDownloadButton"] button:hover { border-color:#9b8cff; transform:translateY(-1px); }
    div[data-testid="stAlert"] { border-radius:13px; }
    div[data-testid="stExpander"] { background:rgba(17,26,42,.72); border:1px solid var(--clip-line); border-radius:14px; }
    div[data-testid="stRadio"] label,div[data-testid="stCheckbox"] label { color:#d8e0ef; }
    div[data-testid="stVideo"] { border-radius:14px; overflow:hidden; }
    .stCaption { color:#91a0b8; }
    @media (max-width:760px) {
      [data-testid="stMainBlockContainer"] { padding:1rem 1rem 2.4rem; }
      .clipai-nav { align-items:flex-start; flex-direction:column; padding-bottom:16px; margin-bottom:20px; }
      .clipai-navlinks { gap:13px; }
      .clipai-hero { grid-template-columns:1fr; gap:20px; padding:23px 19px; border-radius:21px; }
      .clipai-hero h1 { font-size:2.35rem; }
      .clipai-feature-grid { grid-template-columns:1fr; }
      .clipai-statrow { gap:7px; }
      .clipai-stat { font-size:.74rem; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div id="top" class="clipai-nav">
      <a class="clipai-brand" href="?page=Create%20clips#top">🎬 Clip<span>AI</span></a>
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
        <p>ClipAI reviews your long video, finds distinct moments worth sharing, and turns them into vertical shorts with optional word-highlight captions.</p>
        <div class="clipai-statrow">
          <span class="clipai-stat">✂️ 2–5 distinct clips</span>
          <span class="clipai-stat">📱 Vertical 9:16 exports</span>
          <span class="clipai-stat">⏱️ Up to 30 minutes (beta)</span>
        </div>
      </div>
      <div class="clipai-hero-card">
        <div class="clipai-mini-label">Your creator workflow</div>
        <div class="clipai-mini-title">A smarter path from long to short</div>
        <div class="clipai-mini-copy">Bring a video up to 30 minutes, choose 2–5 moments, customize captions, and export vertical clips for social platforms.</div>
        <div class="clipai-statrow">
          <span class="clipai-stat">01 · Upload</span>
          <span class="clipai-stat">02 · Find moments</span>
          <span class="clipai-stat">03 · Export</span>
        </div>
        <div class="clipai-mini-copy" style="margin-top:14px">Direct HTTPS video-file links are supported in this beta. YouTube watch links remain preview-only. Videos can be up to 30 minutes (150 MB).</div>
      </div>
    </section>
    <div id="create" class="clipai-section-kicker">Create</div>
    <div class="clipai-section-title">Make your first clips</div>
    <div class="clipai-section-copy">Choose your output preferences, upload a video, and let ClipAI do the editing work.</div>
    """,
    unsafe_allow_html=True,
)

# ---------------- VIDEO LINK INPUTS ----------------
st.markdown(
    '<div class="clipai-section-kicker">Video link</div>'
    '<div class="clipai-section-title">Use a video link</div>'
    '<div class="clipai-section-copy">YouTube links can be previewed. For automatic processing without manual upload, use a direct HTTPS link to a video file you own or are authorized to edit.</div>',
    unsafe_allow_html=True,
)
youtube_url = st.text_input(
    "YouTube link (preview only)",
    placeholder="https://www.youtube.com/watch?v=...",
    help="YouTube watch, Shorts, embed, and youtu.be links can be previewed here.",
)
if youtube_url.strip():
    youtube_id = extract_youtube_video_id(youtube_url)
    if youtube_id:
        st.video(f"https://www.youtube.com/watch?v={youtube_id}", width=420)
        st.info(
            "YouTube preview only: ClipAI does not download YouTube media from watch URLs. "
            "For a video on your own channel, download the MP4 through YouTube Studio and upload it, "
            "or provide a direct media-file URL from storage you control."
        )
        st.markdown("[Open YouTube Studio](https://studio.youtube.com/)")
    else:
        st.error("This does not look like a supported YouTube video link.")

with st.expander("Use a direct video-file link (MP4 / MOV / WebM)", expanded=False):
    direct_video_url = st.text_input(
        "Direct HTTPS video-file URL",
        placeholder="https://your-storage.example.com/my-video.mp4",
        help="Must link directly to a public video file, not a webpage or a YouTube watch page.",
        key="clipai_direct_video_url",
    )
    rights_confirmed = st.checkbox(
        "I own this video or have permission to download and edit it.",
        key="clipai_direct_video_rights",
    )
    if st.button("Load video from direct link", key="clipai_load_direct_video"):
        if not direct_video_url.strip():
            st.error("Paste a direct video-file URL first.")
        elif not rights_confirmed:
            st.error("Please confirm that you own the video or have permission to process it.")
        else:
            try:
                with st.spinner("Fetching the authorized video file..."):
                    fetched_name, fetched_bytes = download_direct_video(
                        direct_video_url,
                        MAX_UPLOAD_MB * 1024 * 1024,
                    )
                st.session_state["clipai_direct_video"] = {
                    "name": fetched_name,
                    "data": fetched_bytes,
                }
                st.success(f"Direct video link loaded: {fetched_name} ({len(fetched_bytes) / (1024 * 1024):.1f} MB)")
            except Exception as exc:
                st.error(f"Could not load this direct video link: {str(exc)[:400]}")

    if st.session_state.get("clipai_direct_video"):
        st.caption("A direct video file is loaded and ready for ClipAI processing.")
        if st.button("Remove loaded direct video", key="clipai_clear_direct_video"):
            st.session_state.pop("clipai_direct_video", None)
            st.rerun()

st.divider()
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

st.caption("30-minute beta input · 720 × 1280 vertical exports at 24 FPS · 2–5 clips per job. Longer videos need chunked processing, which we will add after this stable beta test.")

st.markdown(
    """
    <div class="clipai-limit"><b>Beta limits:</b> up to 150 MB upload · up to 30-minute video · 2 jobs per account per day · 5 jobs shared across ClipAI per day. Audio is compressed before AI analysis to stay within the current transcription file limit.</div>
    """,
    unsafe_allow_html=True,
)

user_jobs = CURRENT_USAGE["user_jobs"]
used_today = CURRENT_USAGE["app_jobs"]

usage_col1, usage_col2 = st.columns(2)
with usage_col1:
    st.caption(f"Your account usage today: {user_jobs}/{MAX_JOBS_PER_USER_PER_DAY} jobs (UTC reset).")
with usage_col2:
    st.caption(f"Shared beta usage today: {used_today}/{MAX_JOBS_TOTAL_PER_APP_PER_DAY} jobs (UTC reset).")

st.divider()
st.markdown('<div class="clipai-section-kicker">Create clips</div><div class="clipai-section-title">Upload a file or use a loaded direct link</div>', unsafe_allow_html=True)

uploaded_file_widget = st.file_uploader(
    "📁 Upload your video",
    type=["mp4", "mov", "mkv", "avi", "webm"],
    max_upload_size=MAX_UPLOAD_MB,
    help=f"Maximum file size: {MAX_UPLOAD_MB} MB. Maximum video length: 30 minutes.",
)

uploaded_file = uploaded_file_widget
if uploaded_file is None and st.session_state.get("clipai_direct_video"):
    saved_video = st.session_state["clipai_direct_video"]
    uploaded_file = MemoryVideo(saved_video["name"], saved_video["data"])

if uploaded_file is not None:
    file_size_mb = uploaded_file.size / (1024 * 1024)
    source_label = "Direct video link loaded" if isinstance(uploaded_file, MemoryVideo) else "Video uploaded"
    st.success(f"{source_label}: {uploaded_file.name} ({file_size_mb:.1f} MB)")
    # Keep large source videos out of the initial page render; preview only on demand.
    with st.expander("Preview source video", expanded=False):
        st.video(uploaded_file.getvalue(), width=300)

    if file_size_mb > MAX_UPLOAD_MB:
        st.error(f"Please upload a file smaller than {MAX_UPLOAD_MB} MB.")
    elif user_jobs >= MAX_JOBS_PER_USER_PER_DAY:
        st.warning("You have used your account's daily beta limit. Please try after the daily reset (UTC).")
    elif used_today >= MAX_JOBS_TOTAL_PER_APP_PER_DAY:
        st.warning("ClipAI has reached today's shared beta limit. Please try after the daily reset.")
    else:
        if st.button("✨ Generate AI Clips", type="primary"):
            # Atomically reserve a durable per-account and app-wide quota slot.
            try:
                reservation = reserve_daily_job(CURRENT_USER_ID, CURRENT_UTC_DAY)
            except Exception as quota_error:
                st.error("ClipAI could not reserve a quota slot. No AI processing was started.")
                st.caption(str(quota_error)[:500])
                st.stop()

            if not reservation["allowed"]:
                if reservation["reason"] == "user_limit":
                    st.warning("You have used your account's daily limit. Please try after the UTC reset.")
                else:
                    st.warning("ClipAI has reached its shared beta limit for today. Please try after the UTC reset.")
            else:
                st.caption(
                    f"This attempt is counted. Your usage: {reservation['user_jobs']}/{MAX_JOBS_PER_USER_PER_DAY}; "
                    f"shared usage: {reservation['app_jobs']}/{MAX_JOBS_TOTAL_PER_APP_PER_DAY}."
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
                                raise ValueError("For this beta, please use a video of 30 minutes or less.")

                        with st.spinner("Extracting audio..."):
                            run_command(
                                [
                                    "ffmpeg", "-y", "-i", str(video_path),
                                    "-map", "0:a:0", "-vn", "-ac", "1",
                                    "-ar", "16000", "-b:a", "48k", str(audio_path),
                                ],
                                timeout=600,
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
                                    timeout=600.0,
                                )

                        transcript = get_value(transcription, "text", "")
                        segments = get_value(transcription, "segments", []) or []
                        word_timestamps = get_value(transcription, "words", []) or []
                        if not transcript or not segments:
                            raise ValueError("No clear speech was detected. Try a video with audible speech.")

                        transcript_lines = []
                        for segment in segments:
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

                        full_timestamped_transcript = "\n".join(transcript_lines)
                        timestamped_transcript = full_timestamped_transcript
                        transcript_sampled = False
                        if len(full_timestamped_transcript) > MAX_TRANSCRIPT_CHARS_FOR_AI:
                            # Evenly sample timestamped speech lines across the entire video,
                            # not just the opening minutes, to keep the model prompt manageable.
                            target_count = max(
                                1,
                                int(len(transcript_lines) * MAX_TRANSCRIPT_CHARS_FOR_AI / len(full_timestamped_transcript)),
                            )
                            while target_count > 1:
                                indexes = sorted({
                                    round(i * (len(transcript_lines) - 1) / (target_count - 1))
                                    for i in range(target_count)
                                })
                                candidate = "\n".join(transcript_lines[index] for index in indexes)
                                if len(candidate) <= MAX_TRANSCRIPT_CHARS_FOR_AI:
                                    timestamped_transcript = candidate
                                    break
                                target_count -= 1
                            transcript_sampled = True

                        if transcript_sampled:
                            st.info(
                                "This is a long video, so ClipAI sampled speech moments from across the full 30 minutes "
                                "to keep AI analysis within the free-tier request budget."
                            )

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
                                            f"Select up to {requested_clip_count} strongest, distinct moments from this timestamped transcript sample spanning the entire video. "
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
                                    run_command(encode_args, timeout=360)
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
                                        run_command(encode_args, timeout=360)
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

current_year = datetime.now(timezone.utc).year
contact_link = (
    f'<a href="mailto:{html.escape(CONTACT_EMAIL, quote=True)}">Email ClipAI</a>'
    if CONTACT_EMAIL else ""
)
st.markdown(
    f"""
    <div id="how-it-works" class="clipai-section-kicker">Simple workflow</div>
    <div class="clipai-section-title">How ClipAI works</div>
    <div class="clipai-feature-grid">
      <div class="clipai-feature"><div class="clipai-feature-icon">📤</div><div class="clipai-feature-title">1. Add your video</div><div class="clipai-feature-copy">Upload a video or load a direct video-file link you own or have permission to process.</div></div>
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
      <span>© {current_year} ClipAI · Free beta. Features and limits may change during testing.</span>
      <span>
        <a href="?page=Privacy%20Policy">Privacy Policy</a> ·
        <a href="?page=Terms%20of%20Service">Terms of Service</a> ·
        {contact_link} · <a href="#top">Back to top ↑</a>
      </span>
    </div>
    """,
    unsafe_allow_html=True,
)
