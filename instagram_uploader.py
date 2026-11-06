
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
from PIL import Image, ImageOps

# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
CONTENT_DIR = BASE_DIR / "data" / "content"
PUBLIC_DIR = BASE_DIR / "public"


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
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v25.0")

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY", "").strip()
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "").strip()

MAX_POSTS_PER_RUN = int(os.getenv("MAX_INSTAGRAM_POSTS_PER_RUN", "1"))
REQUEST_TIMEOUT = 60
POLL_INTERVAL = 10
MAX_POLL_ATTEMPTS = 60
MAX_CAPTION_LENGTH = 2200
MIN_CAROUSEL_IMAGES = 2
MAX_CAROUSEL_IMAGES = 10

GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# ============================================================
# INITIALIZATION
# ============================================================

def configure_services():
    missing = []

    if not IG_ACCESS_TOKEN:
        missing.append("IG_ACCESS_TOKEN")
    if not IG_USER_ID:
        missing.append("IG_USER_ID")
    if not CLOUDINARY_CLOUD_NAME:
        missing.append("CLOUDINARY_CLOUD_NAME")
    if not CLOUDINARY_API_KEY:
        missing.append("CLOUDINARY_API_KEY")
    if not CLOUDINARY_API_SECRET:
        missing.append("CLOUDINARY_API_SECRET")

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


def build_caption(data):
    content = get_content(data)

    caption = str(content.get("caption") or "").strip()
    title = str(content.get("title") or "").strip()
    hashtags = content.get("hashtags", [])

    if not caption:
        caption = title

    if isinstance(hashtags, list):
        hashtags = " ".join(
            str(tag).strip() for tag in hashtags if str(tag).strip()
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


def find_carousel_images(news_id):
    image_dir = PUBLIC_DIR / news_id

    if not image_dir.is_dir():
        raise FileNotFoundError(f"Image directory not found: {image_dir}")

    all_images = [
        path for path in image_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
    ]

    # Prefer generated slide files to avoid including unrelated assets.
    slide_images = [
        path for path in all_images
        if re.search(r"slide[_\-\s]*\d+", path.stem, re.IGNORECASE)
    ]

    images = slide_images if slide_images else all_images
    images.sort(key=natural_sort_key)

    if not MIN_CAROUSEL_IMAGES <= len(images) <= MAX_CAROUSEL_IMAGES:
        raise ValueError(
            f"{news_id}: Instagram carousel needs 2-10 images; "
            f"found {len(images)} in {image_dir}"
        )

    return images

# ============================================================
# CLOUDINARY IMAGE UPLOAD
# ============================================================

def convert_to_jpeg(image_path, output_path):
    with Image.open(image_path) as image:
        image = ImageOps.exif_transpose(image)

        if image.mode in ("RGBA", "LA"):
            background = Image.new("RGB", image.size, "white")
            if image.mode == "RGBA":
                background.paste(image, mask=image.getchannel("A"))
            else:
                background.paste(image.convert("RGB"))
            image = background
        else:
            image = image.convert("RGB")

        image.save(output_path, "JPEG", quality=92, optimize=True)


def upload_image_to_cloudinary(image_path, news_id, slide_number):
    with tempfile.TemporaryDirectory() as temp_dir:
        jpeg_path = Path(temp_dir) / f"slide_{slide_number}.jpg"
        convert_to_jpeg(image_path, jpeg_path)

        print(f"[CLOUDINARY] Uploading {image_path.name}")

        result = cloudinary.uploader.upload(
            str(jpeg_path),
            resource_type="image",
            folder=f"ai-news/{news_id}",
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
# INSTAGRAM API
# ============================================================

def instagram_request(method, endpoint, data=None, params=None):
    url = f"{GRAPH_URL}/{endpoint}"

    payload = dict(data or {})
    query = dict(params or {})

    if method.upper() == "POST":
        payload["access_token"] = IG_ACCESS_TOKEN
        response = requests.post(
            url,
            data=payload,
            timeout=REQUEST_TIMEOUT
        )
    else:
        query["access_token"] = IG_ACCESS_TOKEN
        response = requests.get(
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
            f"(code={error.get('code', 'unknown')})"
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
        raise RuntimeError("Instagram did not return a carousel container ID.")

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
                f"Instagram container processing failed: {result.get('status')}"
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
        raise RuntimeError("Instagram did not return a published media ID.")

    print(f"[INSTAGRAM] Published successfully: {media_id}")
    return media_id

# ============================================================
# PUBLISHING
# ============================================================

def is_already_posted(data):
    publishing = data.get("publishing", {})
    instagram = publishing.get("instagram", {})

    return (
        isinstance(instagram, dict)
        and instagram.get("done") is True
    )


def publish_news(json_path, dry_run=False):
    data = load_json(json_path)
    news_id = str(data.get("news_id") or json_path.stem)

    if is_already_posted(data):
        print(f"[SKIP] Already posted: {news_id}")
        return False

    caption = build_caption(data)
    images = find_carousel_images(news_id)

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

    media_id = publish_carousel(carousel_id)

    data.setdefault("publishing", {})
    data["publishing"]["instagram"] = {
        "done": True,
        "media_id": media_id,
        "carousel_container_id": carousel_id,
        "account_id": IG_USER_ID,
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "image_count": len(images)
    }

    if data.get("image_done") is not True:
        print(f"[SKIP] Images not marked complete: {news_id}")
        return False

    save_json(json_path, data)

    print(f"[SUCCESS] {news_id} published and status saved.")
    return True

# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Publish AI News carousels to Instagram."
    )
    parser.add_argument("--news-id", help="Process only this news ID.")
    parser.add_argument("--limit", type=int, default=MAX_POSTS_PER_RUN)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate content and images without uploading or publishing."
    )
    args = parser.parse_args()

    if not CONTENT_DIR.is_dir():
        raise FileNotFoundError(f"Content directory not found: {CONTENT_DIR}")

    json_files = (
        [CONTENT_DIR / f"{args.news_id}.json"]
        if args.news_id
        else sorted(CONTENT_DIR.glob("*.json"))
    )

    if args.news_id and not json_files[0].is_file():
        raise FileNotFoundError(f"News JSON not found: {json_files[0]}")

    if not args.dry_run:
        configure_services()

    processed = 0
    successful = 0
    failed = 0

    for json_path in json_files:
        if processed >= args.limit:
            break

        try:
            data = load_json(json_path)

            if is_already_posted(data):
                continue

            if not args.dry_run:
                print("\n[START] Preparing Instagram publishing...")

            if publish_news(json_path, dry_run=args.dry_run):
                successful += 1

            processed += 1

        except Exception as error:
            failed += 1
            processed += 1
            print(f"[ERROR] {json_path.name}: {error}")

    print("\n" + "=" * 55)
    print("[INSTAGRAM SUMMARY]")
    print(f"Processed: {processed}")
    print(f"Successful: {successful}")
    print(f"Failed: {failed}")

    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
