
import os
import re
import json
import time
import argparse
import tempfile
import requests
import cloudinary
import cloudinary.uploader

from pathlib import Path
from datetime import datetime
from PIL import Image, ImageOps

# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
CONTENT_DIR = BASE_DIR / "data" / "content"
PUBLIC_DIR = BASE_DIR / "public"

MAX_POSTS_PER_RUN = int(os.getenv("MAX_INSTAGRAM_POSTS_PER_RUN", "1"))
REQUEST_TIMEOUT = 60
POLL_INTERVAL = 10
MAX_POLL_ATTEMPTS = 60
MAX_CAPTION_LENGTH = 2200
MIN_CAROUSEL_IMAGES = 2
MAX_CAROUSEL_IMAGES = 10

SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
GRAPH_API_VERSION = "v25.0"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "AINewsPublisher/2.0"})

# ============================================================
# ENVIRONMENT
# ============================================================

def load_env():
    env_path = BASE_DIR / ".env"

    if not env_path.exists():
        return

    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


load_env()

IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "").strip()
IG_USER_ID = os.getenv("IG_USER_ID", "").strip()
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", GRAPH_API_VERSION)

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY", "").strip()
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "").strip()

GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# ============================================================
# INITIALIZATION
# ============================================================

def configure_services():
    missing = []

    required = {
        "IG_ACCESS_TOKEN": IG_ACCESS_TOKEN,
        "IG_USER_ID": IG_USER_ID,
        "CLOUDINARY_CLOUD_NAME": CLOUDINARY_CLOUD_NAME,
        "CLOUDINARY_API_KEY": CLOUDINARY_API_KEY,
        "CLOUDINARY_API_SECRET": CLOUDINARY_API_SECRET
    }

    for key, value in required.items():
        if not value:
            missing.append(key)

    if missing:
        raise RuntimeError(
            "Missing environment variables: " + ", ".join(missing)
        )

    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True
    )

# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path):
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def save_json(path, data):
    temp_path = path.with_suffix(".tmp")

    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=4)

    temp_path.replace(path)


def get_content(data):
    content = data.get("content", {})
    return content if isinstance(content, dict) else {}


def get_instagram_status(data):
    publishing = data.get("publishing", {})
    if not isinstance(publishing, dict):
        return {}

    instagram = publishing.get("instagram", {})
    return instagram if isinstance(instagram, dict) else {}


def is_already_posted(data):
    status = get_instagram_status(data)
    return status.get("done") is True


def is_ready_for_publishing(data):
    if data.get("image_done") is not True:
        return False

    if is_already_posted(data):
        return False

    status = get_instagram_status(data)

    # An interrupted/uncertain publication must be checked manually.
    if status.get("status") == "publishing":
        return False

    return True

# ============================================================
# CAPTION
# ============================================================

def build_caption(data):
    content = get_content(data)

    caption = str(content.get("caption") or "").strip()
    title = str(content.get("title") or "").strip()
    hashtags = content.get("hashtags", [])

    if not caption:
        caption = title

    if isinstance(hashtags, list):
        hashtags = " ".join(
            str(tag).strip()
            for tag in hashtags
            if str(tag).strip()
        )
    else:
        hashtags = str(hashtags or "").strip()

    if hashtags and hashtags.lower() not in caption.lower():
        caption = f"{caption}\n\n{hashtags}" if caption else hashtags

    caption = re.sub(r"\n{3,}", "\n\n", caption).strip()

    if not caption:
        raise ValueError("No caption or title found in content JSON.")

    if len(caption) > MAX_CAPTION_LENGTH:
        caption = caption[:MAX_CAPTION_LENGTH - 3].rstrip() + "..."

    return caption

# ============================================================
# IMAGE DISCOVERY
# ============================================================

def natural_sort_key(path):
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", path.name)
    ]


def get_date_key(json_path, published_at=None):
    # Prefer the date folder containing the content JSON.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", json_path.parent.name):
        return json_path.parent.name

    if published_at:
        try:
            parsed = datetime.fromisoformat(
                str(published_at).replace("Z", "+00:00")
            )
            return parsed.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass

    return None


def find_carousel_images(news_id, published_at=None, json_path=None):
    candidate_dirs = []

    if json_path is not None:
        date_key = get_date_key(json_path, published_at)

        if date_key:
            candidate_dirs.append(PUBLIC_DIR / date_key / news_id)

    candidate_dirs.append(PUBLIC_DIR / news_id)

    image_dir = next(
        (path for path in candidate_dirs if path.is_dir()),
        None
    )

    if image_dir is None:
        searched = ", ".join(str(path) for path in candidate_dirs)
        raise FileNotFoundError(
            f"Image directory not found. Searched: {searched}"
        )

    all_images = [
        path for path in image_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
    ]

    slide_images = [
        path for path in all_images
        if re.search(
            r"slide[_\-\s]*\d+",
            path.stem,
            re.IGNORECASE
        )
    ]

    images = slide_images if slide_images else all_images
    images.sort(key=natural_sort_key)

    if not MIN_CAROUSEL_IMAGES <= len(images) <= MAX_CAROUSEL_IMAGES:
        raise ValueError(
            f"{news_id}: Instagram carousel requires 2-10 images; "
            f"found {len(images)} in {image_dir}"
        )

    return images

# ============================================================
# IMAGE VALIDATION / CONVERSION
# ============================================================

def convert_to_jpeg(image_path, output_path):
    with Image.open(image_path) as source:
        image = ImageOps.exif_transpose(source)

        if "A" in image.getbands() or "transparency" in image.info:
            image = image.convert("RGBA")
            background = Image.new("RGBA", image.size, "white")
            background.alpha_composite(image)
            image = background.convert("RGB")
        else:
            image = image.convert("RGB")

        if image.width < 300 or image.height < 300:
            raise ValueError(f"Image is too small: {image_path}")

        image.save(
            output_path,
            "JPEG",
            quality=92,
            optimize=True
        )

# ============================================================
# CLOUDINARY
# ============================================================

def upload_image_to_cloudinary(image_path, news_id, slide_number):
    safe_news_id = re.sub(r"[^a-zA-Z0-9_-]", "_", news_id)

    with tempfile.TemporaryDirectory() as temp_dir:
        jpeg_path = Path(temp_dir) / f"slide_{slide_number}.jpg"

        convert_to_jpeg(image_path, jpeg_path)

        print(f"[CLOUDINARY] Uploading {image_path.name}")

        result = cloudinary.uploader.upload(
            str(jpeg_path),
            resource_type="image",
            folder=f"ai-news/{safe_news_id}",
            public_id=f"slide_{slide_number}",
            overwrite=True,
            format="jpg"
        )

        image_url = result.get("secure_url")

        if not image_url:
            raise RuntimeError(
                f"Cloudinary did not return a URL for {image_path.name}"
            )

        print(f"[CLOUDINARY] Uploaded slide {slide_number}")
        return image_url


def upload_carousel_images(news_id, images):
    urls = []

    for index, image_path in enumerate(images, start=1):
        url = upload_image_to_cloudinary(
            image_path,
            news_id,
            index
        )
        urls.append(url)

    return urls

# ============================================================
# INSTAGRAM GRAPH API
# ============================================================

def instagram_request(method, endpoint, data=None, params=None):
    url = f"{GRAPH_URL}/{endpoint}"

    payload = dict(data or {})
    query = dict(params or {})

    if method.upper() == "POST":
        payload["access_token"] = IG_ACCESS_TOKEN

        response = SESSION.post(
            url,
            data=payload,
            timeout=REQUEST_TIMEOUT
        )
    else:
        query["access_token"] = IG_ACCESS_TOKEN

        response = SESSION.get(
            url,
            params=query,
            timeout=REQUEST_TIMEOUT
        )

    try:
        result = response.json()
    except ValueError:
        raise RuntimeError(
            f"Instagram returned invalid response: HTTP {response.status_code}"
        )

    if not response.ok or "error" in result:
        error = result.get("error", {})

        raise RuntimeError(
            f"Instagram API error: {error.get('message', result)} "
            f"(code={error.get('code', 'unknown')}, "
            f"subcode={error.get('error_subcode', 'unknown')})"
        )

    return result


def create_image_container(image_url, slide_number):
    result = instagram_request(
        "POST",
        f"{IG_USER_ID}/media",
        data={
            "image_url": image_url,
            "is_carousel_item": "true"
        }
    )

    container_id = result.get("id")

    if not container_id:
        raise RuntimeError(
            f"Instagram did not return a container ID for slide {slide_number}"
        )

    print(f"[INSTAGRAM] Created slide {slide_number} container")
    return container_id


def create_carousel_container(container_ids, caption):
    if not MIN_CAROUSEL_IMAGES <= len(container_ids) <= MAX_CAROUSEL_IMAGES:
        raise ValueError("Invalid number of Instagram carousel items.")

    result = instagram_request(
        "POST",
        f"{IG_USER_ID}/media",
        data={
            "media_type": "CAROUSEL",
            "children": ",".join(container_ids),
            "caption": caption
        }
    )

    carousel_id = result.get("id")

    if not carousel_id:
        raise RuntimeError(
            "Instagram did not return a carousel container ID."
        )

    print(f"[INSTAGRAM] Created carousel container: {carousel_id}")
    return carousel_id


def wait_for_container(container_id):
    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        result = instagram_request(
            "GET",
            container_id,
            params={"fields": "status_code,status"}
        )

        status = result.get("status_code")

        print(
            f"[INSTAGRAM] Container status: {status} "
            f"({attempt}/{MAX_POLL_ATTEMPTS})"
        )

        if status in ("FINISHED", "PUBLISHED"):
            return True

        if status == "ERROR":
            raise RuntimeError(
                f"Instagram container processing failed: "
                f"{result.get('status')}"
            )

        if status == "EXPIRED":
            raise RuntimeError("Instagram container expired.")

        time.sleep(POLL_INTERVAL)

    raise TimeoutError("Instagram container processing timed out.")


def publish_carousel(carousel_id):
    result = instagram_request(
        "POST",
        f"{IG_USER_ID}/media_publish",
        data={"creation_id": carousel_id}
    )

    media_id = result.get("id")

    if not media_id:
        raise RuntimeError(
            "Instagram did not return a published media ID."
        )

    print(f"[INSTAGRAM] Published successfully: {media_id}")
    return media_id

# ============================================================
# PUBLISHING
# ============================================================

def mark_publication_in_progress(json_path, data, carousel_id):
    data.setdefault("publishing", {})
    data["publishing"]["instagram"] = {
        "done": False,
        "status": "publishing",
        "carousel_container_id": carousel_id,
        "account_id": IG_USER_ID,
        "started_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime()
        )
    }

    save_json(json_path, data)


def publish_news(json_path, dry_run=False):
    data = load_json(json_path)
    news_id = str(data.get("news_id") or json_path.stem)

    if is_already_posted(data):
        print(f"[SKIP] Already posted: {news_id}")
        return False

    if data.get("image_done") is not True:
        print(f"[SKIP] Images not marked complete: {news_id}")
        return False

    status = get_instagram_status(data)

    if status.get("status") == "publishing":
        print(
            f"[MANUAL CHECK] Previous publishing attempt may be incomplete: "
            f"{news_id}"
        )
        return False

    caption = build_caption(data)

    images = find_carousel_images(
        news_id,
        published_at=data.get("published_at"),
        json_path=json_path
    )

    print("\n" + "=" * 55)
    print(f"[NEWS] {news_id}")
    print(f"[IMAGES] {len(images)}")
    print(f"[CAPTION] {caption[:200]}")

    for image in images:
        print(f"  - {image.name}")

    if dry_run:
        print("[DRY RUN] Validation successful. Nothing uploaded or published.")
        return True

    image_urls = upload_carousel_images(news_id, images)

    child_ids = [
        create_image_container(url, index)
        for index, url in enumerate(image_urls, start=1)
    ]

    carousel_id = create_carousel_container(child_ids, caption)

    wait_for_container(carousel_id)

    # Save before publishing to prevent automatic duplicate retries
    # if the publishing request has an uncertain outcome.
    mark_publication_in_progress(json_path, data, carousel_id)

    try:
        media_id = publish_carousel(carousel_id)
    except Exception:
        print(
            f"[WARNING] Publication outcome may be uncertain for {news_id}. "
            "Check Instagram before retrying."
        )
        raise

    data["publishing"]["instagram"] = {
        "done": True,
        "status": "published",
        "media_id": media_id,
        "carousel_container_id": carousel_id,
        "account_id": IG_USER_ID,
        "published_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime()
        ),
        "image_count": len(images)
    }

    save_json(json_path, data)

    print(f"[SUCCESS] {news_id} published and status saved.")
    return True

# ============================================================
# FILE DISCOVERY
# ============================================================

def load_json_files(news_id=None):
    if not CONTENT_DIR.is_dir():
        raise FileNotFoundError(
            f"Content directory not found: {CONTENT_DIR}"
        )

    if news_id:
        files = [
            path for path in CONTENT_DIR.rglob("*.json")
            if path.stem == news_id
        ]

        if not files:
            raise FileNotFoundError(
                f"News JSON not found for ID: {news_id}"
            )

        return files

    return sorted(
        CONTENT_DIR.rglob("*.json"),
        key=lambda path: path.stat().st_mtime
    )


def get_eligible_files(json_files):
    eligible = []

    for path in json_files:
        try:
            data = load_json(path)

            if is_ready_for_publishing(data):
                eligible.append(path)
            else:
                news_id = data.get("news_id", path.stem)

                if data.get("image_done") is not True:
                    print(f"[SKIP] Images pending: {news_id}")
                elif is_already_posted(data):
                    print(f"[SKIP] Already published: {news_id}")
                elif get_instagram_status(data).get("status") == "publishing":
                    print(f"[SKIP] Manual publication check required: {news_id}")

        except Exception as exc:
            print(f"[INVALID JSON] {path.name}: {exc}")

    return eligible

# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Publish AI News carousels to Instagram."
    )

    parser.add_argument(
        "--news-id",
        help="Process only this news ID."
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=MAX_POSTS_PER_RUN
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate content and images without uploading or publishing."
    )

    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be at least 1.")

    json_files = load_json_files(args.news_id)
    eligible_files = get_eligible_files(json_files)
    selected_files = eligible_files[:args.limit]

    if not selected_files:
        print("[INFO] No eligible news available for Instagram publishing.")
        return

    if not args.dry_run:
        configure_services()

    processed = 0
    successful = 0
    failed = 0

    print("\n========== INSTAGRAM PUBLISHER ==========")
    print(f"Account ID: {IG_USER_ID or 'DRY RUN'}")
    print(f"Eligible news: {len(eligible_files)}")
    print(f"Run limit: {args.limit}")
    print(f"Selected: {len(selected_files)}")
    print(f"Dry run: {args.dry_run}")
    print("=========================================")

    for json_path in selected_files:
        processed += 1

        try:
            if publish_news(json_path, dry_run=args.dry_run):
                successful += 1

        except Exception as error:
            failed += 1
            print(f"[ERROR] {json_path.name}: {error}")

    print("\n" + "=" * 55)
    print("[INSTAGRAM SUMMARY]")
    print(f"Processed: {processed}")
    print(f"Successful: {successful}")
    print(f"Failed: {failed}")
    print("=" * 55)

    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
