import os
import re
import json
import sys
import argparse

from pathlib import Path
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
CONTENT_DIR = BASE_DIR / "data" / "content"
PUBLIC_DIR = BASE_DIR / "public"

TOKEN_FILE = Path(
    os.getenv("YOUTUBE_TOKEN_FILE", str(BASE_DIR / "instanews24_token.json"))
)
if not TOKEN_FILE.is_absolute():
    TOKEN_FILE = BASE_DIR / TOKEN_FILE

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]

PRIVACY_STATUS = os.getenv("YOUTUBE_PRIVACY_STATUS", "public")
CATEGORY_ID = os.getenv("YOUTUBE_CATEGORY_ID", "25")  # News & Politics
MAX_UPLOADS_PER_RUN = int(os.getenv("MAX_UPLOADS_PER_RUN", "1"))
EXPECTED_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "").strip()
CHUNK_SIZE = 8 * 1024 * 1024


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path, data):
    path = Path(path)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    os.replace(temp_path, path)


def is_true(value):
    return str(value or "").strip().lower() in {"true", "1", "yes"}


def clean_text(value):
    if isinstance(value, list):
        value = " ".join(str(item) for item in value if item)
    return re.sub(r"\s+", " ", str(value or "")).strip()


def clean_tags(*values):
    tags = {"InstaNews24", "Shorts", "News"}

    for value in values:
        if not value:
            continue

        items = value if isinstance(value, list) else [value]

        for item in items:
            for word in re.findall(r"[\w#-]+", str(item)):
                word = word.strip("#- ")
                if word:
                    tags.add(word[:100])

    return list(tags)[:30]


# ============================================================
# YOUTUBE AUTHENTICATION
# ============================================================

def load_youtube():
    if not TOKEN_FILE.is_file():
        raise FileNotFoundError(
            f"YouTube OAuth token not found: {TOKEN_FILE}\n"
            "Set YOUTUBE_TOKEN_FILE or place youtube_token.json beside this script."
        )

    credentials = Credentials.from_authorized_user_file(
        str(TOKEN_FILE), SCOPES
    )

    if not credentials.valid:
        if credentials.expired and credentials.refresh_token:
            print("[YOUTUBE] Refreshing OAuth token...")
            credentials.refresh(Request())
            TOKEN_FILE.write_text(credentials.to_json(), encoding="utf-8")
            print("[YOUTUBE] OAuth token refreshed.")
        else:
            raise RuntimeError(
                "YouTube credentials are invalid or expired. "
                "Run your YouTube OAuth authorization script again."
            )

    youtube = build(
        "youtube", "v3",
        credentials=credentials,
        cache_discovery=False
    )

    print("[YOUTUBE] OAuth authentication successful.")
    return youtube


def verify_youtube_channel(youtube):
    response = youtube.channels().list(
        part="id,snippet",
        mine=True
    ).execute()

    channels = response.get("items", [])
    if not channels:
        raise RuntimeError("No YouTube channel found for this OAuth account.")

    for channel in channels:
        channel_id = channel["id"]
        channel_title = channel.get("snippet", {}).get("title", "Unknown")
        print(f"[YOUTUBE] Authenticated channel: {channel_title} ({channel_id})")

    if EXPECTED_CHANNEL_ID:
        if not any(item["id"] == EXPECTED_CHANNEL_ID for item in channels):
            raise RuntimeError(
                "YouTube channel mismatch. "
                f"Expected {EXPECTED_CHANNEL_ID}; authenticated IDs: "
                f"{[item['id'] for item in channels]}. Upload cancelled."
            )
        print("[YOUTUBE] Channel ID verification successful.")


# ============================================================
# VIDEO AND NEWS DISCOVERY
# ============================================================

def load_content_index():
    if not CONTENT_DIR.exists():
        raise FileNotFoundError(f"Content directory not found: {CONTENT_DIR}")

    index = {}

    for json_path in CONTENT_DIR.rglob("*.json"):
        try:
            data = load_json(json_path)
            news_id = str(data.get("news_id") or json_path.stem)
            index[news_id] = (json_path, data)
        except Exception as exc:
            print(f"[SKIP] Invalid content JSON {json_path}: {exc}")

    return index

def discover_videos():
    if not PUBLIC_DIR.exists():
        raise FileNotFoundError(f"Video directory not found: {PUBLIC_DIR}")

    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    yesterday = today - timedelta(days=1)

    videos = []

    # Expected structure:
    # public/YYYY-MM-DD/news_id/video.mp4

    for video_path in PUBLIC_DIR.glob("*/*/video.mp4"):
        if video_path.is_file() and video_path.stat().st_size > 0:
            videos.append(video_path)

    def sort_priority(video_path):
        folder_date = video_path.parent.parent.name
        news_id = video_path.parent.name

        try:
            news_date = datetime.strptime(
                folder_date, "%Y-%m-%d"
            ).date()
        except ValueError:
            # Invalid date folders go last.
            return (4, 0, news_id)

        if news_date == today:
            priority = 0
        elif news_date == yesterday:
            priority = 1
        elif news_date < yesterday:
            priority = 2
        else:
            # Future date folders go after older news.
            priority = 3

        return (
            priority,
            -news_date.toordinal(),
            news_id
        )

    videos.sort(key=sort_priority)

    print(f"\n[DISCOVERY] Current date (IST): {today}")
    print(f"[DISCOVERY] Yesterday: {yesterday}")
    print(f"[DISCOVERY] Total videos found: {len(videos)}")

    return videos

def get_video_metadata(video_path, content_index):
    news_id = video_path.parent.name

    if news_id not in content_index:
        raise FileNotFoundError(
            f"No matching news JSON found for news_id '{news_id}' "
            f"under {CONTENT_DIR}"
        )

    json_path, data = content_index[news_id]
    content = data.get("content") or {}
    classification = data.get("classification") or {}

    title = clean_text(content.get("title") or data.get("title"))
    if not title:
        raise ValueError(f"Missing news title in {json_path}")

    caption = clean_text(content.get("caption") or data.get("description"))
    hashtags = content.get("hashtags") or []
    tags = clean_tags(
        title,
        classification.get("tags", []),
        hashtags,
        classification.get("category", "")
    )

    description_parts = []
    if caption:
        description_parts.append(caption)

    source = clean_text(data.get("source"))
    source_url = clean_text(data.get("source_url") or data.get("url"))

    if source:
        description_parts.append(f"Source: {source}")
    if source_url:
        description_parts.append(f"Article: {source_url}")

    description_parts.append("#Shorts #InstaNews24")
    description = "\n\n".join(description_parts)[:5000]

    return json_path, data, {
        "title": title[:100],
        "description": description,
        "tags": tags
    }


# ============================================================
# YOUTUBE UPLOAD
# ============================================================

def upload_video(youtube, video_path, metadata):
    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "categoryId": CATEGORY_ID
        },
        "status": {
            "privacyStatus": PRIVACY_STATUS,
            "selfDeclaredMadeForKids": False
        }
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        chunksize=CHUNK_SIZE,
        resumable=True
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    print(f"\n[YOUTUBE] Uploading: {video_path}")
    response = None

    while response is None:
        status, response = request.next_chunk()
        if status:
            print(
                f"[YOUTUBE] Upload progress: "
                f"{int(status.progress() * 100)}%"
            )

    video_id = response.get("id")
    if not video_id:
        raise RuntimeError("YouTube did not return a video ID.")

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    print(f"[YOUTUBE] Upload successful: {video_url}")
    return video_id, video_url


# ============================================================
# PROCESSING
# ============================================================

def process_video(youtube, video_path, content_index):
    json_path, data, metadata = get_video_metadata(
        video_path, content_index
    )

    if is_true(data.get("youtube_done")):
        print(f"[SKIP] Already uploaded: {video_path.parent.name}")
        return "already_done"

    video_id, video_url = upload_video(
        youtube, video_path, metadata
    )

    # Save immediately after a successful YouTube response.
    data["youtube_done"] = True
    data["youtube_video_id"] = video_id
    data["youtube_url"] = video_url
    data["youtube_uploaded_at"] = __import__("datetime").datetime.now().astimezone().isoformat()
    data["video_path"] = video_path.relative_to(PUBLIC_DIR).as_posix()
    save_json(json_path, data)

    print(f"[JSON] Updated: {json_path}")
    return "uploaded"


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Upload InstaNews24 videos from public/YYYY-MM-DD/news_id/video.mp4"
    )
    parser.add_argument(
        "video",
        nargs="?",
        help="Optional exact video path or news_id to upload"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List eligible videos without uploading"
    )
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("INSTANEWS24 YOUTUBE UPLOADER")
    print("=" * 60)
    print(f"Videos       : {PUBLIC_DIR}")
    print(f"Content JSON : {CONTENT_DIR}")
    print(f"Token file   : {TOKEN_FILE}")
    print(f"Privacy      : {PRIVACY_STATUS}")
    print(f"Max uploads  : {MAX_UPLOADS_PER_RUN}")
    print(f"Dry run      : {args.dry_run}")
    print("=" * 60)

    content_index = load_content_index()
    videos = discover_videos()

    if args.video:
        requested = Path(args.video)
        if not requested.is_absolute():
            requested = BASE_DIR / requested

        if requested.is_file():
            videos = [requested]
        else:
            videos = [
                path for path in videos
                if path.parent.name == args.video
            ]
            if not videos:
                raise FileNotFoundError(
                    f"No video.mp4 found for news_id: {args.video}"
                )

    pending = []
    for video_path in videos:
        try:
            _, data, _ = get_video_metadata(video_path, content_index)
            if not is_true(data.get("youtube_done")):
                pending.append(video_path)
        except Exception as exc:
            print(f"[SKIP] {video_path}: {exc}")

    pending = pending[:MAX_UPLOADS_PER_RUN]

    if not pending:
        print("[INFO] No pending videos to upload.")
        return

    if args.dry_run:
        print("\n[DRY RUN] Videos ready for upload:")
        for video_path in pending:
            _, _, metadata = get_video_metadata(video_path, content_index)
            print(f"- {video_path}")
            print(f"  Title: {metadata['title']}")
        print(f"\n[DRY RUN] Total: {len(pending)}")
        return

    youtube = load_youtube()
    verify_youtube_channel(youtube)

    uploaded = 0
    failed = 0

    for video_path in pending:
        try:
            result = process_video(youtube, video_path, content_index)
            if result == "uploaded":
                uploaded += 1
        except Exception as exc:
            failed += 1
            print(f"\n[ERROR] {video_path}: {exc}")
            print("[WARNING] Check YouTube Studio before retrying if upload outcome is uncertain.")
            break

    print("\n" + "=" * 60)
    print("YOUTUBE UPLOAD SUMMARY")
    print(f"Uploaded : {uploaded}")
    print(f"Failed   : {failed}")
    print("=" * 60)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
