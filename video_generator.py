import os
import sys
import json
import random
import shutil
import subprocess
import tempfile

from pathlib import Path
from PIL import Image, ImageDraw

import image_generator


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
CONTENT_DIR = BASE_DIR / "data" / "content"
PUBLIC_DIR = BASE_DIR / "public"
MUSIC_DIR = BASE_DIR / "assets" / "music"

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
VIDEO_FPS = 30
SLIDE_DURATION = 4
FRAMES_PER_SLIDE = VIDEO_FPS * SLIDE_DURATION

# Keep the original 1080x1350 carousel fully visible in the 9:16 video.
IMAGE_TOP = (VIDEO_HEIGHT - image_generator.HEIGHT) // 2
PROGRESS_X = 70
PROGRESS_Y = IMAGE_TOP + image_generator.HEIGHT - 85
PROGRESS_WIDTH = VIDEO_WIDTH - 140
PROGRESS_HEIGHT = 7
COUNTER_Y = IMAGE_TOP + image_generator.HEIGHT - 69

PROGRESS_COLOR = (32, 188, 229, 255)
TRACK_COLOR = (255, 255, 255, 90)
MUSIC_VOLUME = 0.20
MUSIC_FADE_SECONDS = 1.5
MAX_VIDEOS_PER_RUN = 10

FFMPEG = os.getenv("FFMPEG_PATH", "ffmpeg")


# ============================================================
# HELPERS
# ============================================================

def run_command(command):
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-6000:] or "FFmpeg command failed")
    return result


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path, data):
    Path(path).write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def get_music_files():
    extensions = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}
    if not MUSIC_DIR.exists():
        raise FileNotFoundError(f"Music folder not found: {MUSIC_DIR}")

    files = sorted(
        path for path in MUSIC_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() in extensions
    )
    if not files:
        raise FileNotFoundError(
            f"No supported music files found in {MUSIC_DIR}"
        )
    return files


def get_output_folder(data, json_file):
    news_id = str(data.get("news_id") or Path(json_file).stem)
    date_key = image_generator.content_date_key(
        data.get("published_at"), json_file
    )
    return PUBLIC_DIR / date_key / news_id


def get_slide_images(output_dir):
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Image manifest not found: {manifest_path}")

    manifest = load_json(manifest_path)
    images = []

    for item in manifest.get("images", []):
        filename = item.get("file")
        if not filename:
            continue

        image_path = output_dir / filename
        if not image_path.is_file():
            raise FileNotFoundError(f"Manifest image missing: {image_path}")
        images.append(image_path)

    if not images:
        raise ValueError(f"No slide images listed in {manifest_path}")

    return images


def create_counter_overlay(path, index, total):
    overlay = Image.new(
        "RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0)
    )
    draw = ImageDraw.Draw(overlay)
    selected_font = image_generator.font(30, "bold")

    draw.text(
        (VIDEO_WIDTH - 70, COUNTER_Y),
        f"{index:02d} / {total:02d}",
        font=selected_font,
        fill="white",
        anchor="ra",
        stroke_width=1,
        stroke_fill=(0, 0, 0, 150)
    )
    overlay.save(path, "PNG", optimize=True)


def create_progress_frames(progress_dir):
    """
    Create one transparent progress overlay per video frame.
    The fill advances linearly from 0% to 100% over this slide's
    four seconds. A new sequence is created for every slide.
    """
    progress_dir.mkdir(parents=True, exist_ok=True)
    radius = max(1, PROGRESS_HEIGHT // 2)

    for frame_index in range(FRAMES_PER_SLIDE):
        progress = frame_index / max(1, FRAMES_PER_SLIDE - 1)
        fill_width = round(PROGRESS_WIDTH * progress)

        overlay = Image.new(
            "RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0)
        )
        draw = ImageDraw.Draw(overlay)

        track_box = (
            PROGRESS_X,
            PROGRESS_Y,
            PROGRESS_X + PROGRESS_WIDTH,
            PROGRESS_Y + PROGRESS_HEIGHT
        )
        draw.rounded_rectangle(
            track_box, radius=radius, fill=TRACK_COLOR
        )

        if fill_width > 0:
            fill_box = (
                PROGRESS_X,
                PROGRESS_Y,
                PROGRESS_X + fill_width,
                PROGRESS_Y + PROGRESS_HEIGHT
            )
            draw.rounded_rectangle(
                fill_box, radius=radius, fill=PROGRESS_COLOR
            )

        overlay.save(
            progress_dir / f"frame_{frame_index:04d}.png",
            "PNG",
            optimize=True
        )


# ============================================================
# VIDEO GENERATION
# ============================================================

def create_slide_segment(image_path, counter_path, progress_dir,
                         output_path, index):
    # The blurred background fills 9:16. The original 4:5 slide
    # stays centered, fully visible and uncropped.
    progress_pattern = progress_dir / "frame_%04d.png"

    filter_complex = (
        "[0:v]split=2[bg][fg];"
        f"[bg]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:"
        "force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
        "boxblur=30:10,eq=brightness=-0.30[background];"
        f"[fg]scale={VIDEO_WIDTH}:{image_generator.HEIGHT}:"
        "force_original_aspect_ratio=decrease[foreground];"
        f"[background][foreground]overlay=0:{IMAGE_TOP}[base];"
        "[base][1:v]overlay=0:0[countered];"
        "[countered][2:v]overlay=0:0,format=yuv420p[out]"
    )

    command = [
        FFMPEG, "-y",
        "-loop", "1", "-framerate", str(VIDEO_FPS),
        "-t", str(SLIDE_DURATION), "-i", str(image_path),
        "-loop", "1", "-framerate", str(VIDEO_FPS),
        "-t", str(SLIDE_DURATION), "-i", str(counter_path),
        "-framerate", str(VIDEO_FPS),
        "-start_number", "0", "-i", str(progress_pattern),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-an",
        "-t", str(SLIDE_DURATION),
        "-r", str(VIDEO_FPS),
        "-frames:v", str(FRAMES_PER_SLIDE),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path)
    ]

    print(f"[VIDEO] Rendering slide {index} with animated progress...")
    run_command(command)


def concat_quote(path):
    # Escape a path for FFmpeg concat-demuxer file syntax.
    return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")


def create_video(images, output_path):
    music_file = random.choice(get_music_files())
    total = len(images)
    duration = total * SLIDE_DURATION

    print(f"[VIDEO] Slides: {total}")
    print(f"[VIDEO] Duration: {duration} seconds")
    print(f"[VIDEO] Random music selected: {music_file.name}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="instanews_video_") as temp:
        temp_dir = Path(temp)
        segment_paths = []

        for index, image_path in enumerate(images, start=1):
            counter_path = temp_dir / f"counter_{index:02d}.png"
            progress_dir = temp_dir / f"progress_{index:02d}"
            segment_path = temp_dir / f"segment_{index:02d}.mp4"

            create_counter_overlay(counter_path, index, total)
            create_progress_frames(progress_dir)
            create_slide_segment(
                image_path,
                counter_path,
                progress_dir,
                segment_path,
                index
            )
            segment_paths.append(segment_path)

        concat_file = temp_dir / "segments.txt"
        concat_file.write_text(
            "".join(
                f"file '{concat_quote(path)}'\n"
                for path in segment_paths
            ),
            encoding="utf-8"
        )

        silent_video = temp_dir / "silent_video.mp4"
        run_command([
            FFMPEG, "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(silent_video)
        ])

        fade_duration = min(MUSIC_FADE_SECONDS, duration)
        fade_start = max(0, duration - fade_duration)

        run_command([
            FFMPEG, "-y",
            "-i", str(silent_video),
            "-stream_loop", "-1", "-i", str(music_file),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-t", str(duration),
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "160k",
            "-af",
            f"volume={MUSIC_VOLUME},"
            f"afade=t=out:st={fade_start}:d={fade_duration}",
            "-movflags", "+faststart",
            str(output_path)
        ])

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("FFmpeg did not create a valid video file")

    print(
        f"[VIDEO SAVED] {output_path} "
        f"({output_path.stat().st_size / 1024 / 1024:.2f} MB)"
    )
    return output_path


# ============================================================
# PROCESS ONE NEWS STORY
# ============================================================

def generate_video_for_json(json_file):
    json_file = Path(json_file)
    if not json_file.is_absolute():
        json_file = BASE_DIR / json_file

    data = load_json(json_file)
    news_id = str(data.get("news_id") or json_file.stem)

    print(f"\n[NEWS] {news_id}")
    image_generator.discover_fonts()

    output_dir = get_output_folder(data, json_file)
    manifest_path = output_dir / "manifest.json"

    # Reuse completed carousel images. Otherwise generate them once.
    if data.get("image_done") is not True or not manifest_path.is_file():
        print("[IMAGES] Generating carousel images...")
        relative_folder = image_generator.generate_post(json_file)
        output_dir = PUBLIC_DIR / relative_folder
        data = load_json(json_file)
    else:
        print("[IMAGES] Reusing existing carousel images")

    images = get_slide_images(output_dir)
    video_path = output_dir / "video.mp4"

    create_video(images, video_path)

    # Update JSON only after successful video generation.
    data["video_done"] = True
    data["video_path"] = video_path.relative_to(PUBLIC_DIR).as_posix()
    save_json(json_file, data)

    print(f"[DONE] {news_id}")
    print(f"[OUTPUT FOLDER] {output_dir}")
    return data["video_path"]


# ============================================================
# BATCH PROCESSING
# ============================================================

def load_pending_files():
    if not CONTENT_DIR.exists():
        raise FileNotFoundError(f"Content folder not found: {CONTENT_DIR}")

    files = []
    for path in CONTENT_DIR.rglob("*.json"):
        try:
            data = load_json(path)
            if data.get("video_done") is not True:
                files.append(path)
        except Exception as exc:
            print(f"[SKIP] Invalid JSON {path.name}: {exc}")

    return sorted(files, key=lambda path: path.stat().st_mtime)[
        :MAX_VIDEOS_PER_RUN
    ]


def main(filename=None):
    if not shutil.which(FFMPEG) and not Path(FFMPEG).is_file():
        raise FileNotFoundError(
            f"FFmpeg not found. Install FFmpeg or set FFMPEG_PATH. "
            f"Current: {FFMPEG}"
        )

    if filename:
        path = Path(filename)
        if not path.is_absolute():
            path = BASE_DIR / path
        files = [path]
    else:
        files = load_pending_files()

    if not files:
        print("[INFO] No pending videos")
        return

    success = 0
    failed = 0

    print("\n========== INSTA NEWS VIDEO GENERATOR ==========")
    print(f"Output: {PUBLIC_DIR}")
    print(f"Slide duration: {SLIDE_DURATION}s")
    print(f"Resolution: {VIDEO_WIDTH}x{VIDEO_HEIGHT}")
    print("Progress: animated independently on every slide")
    print("Music: random licensed track per video")

    for path in files:
        try:
            if not path.is_file():
                raise FileNotFoundError(path)
            generate_video_for_json(path)
            success += 1
        except Exception as exc:
            print(f"[FAILED] {path.name}: {exc}")
            failed += 1

    print("\n========== VIDEO SUMMARY ==========")
    print(f"Success: {success}")
    print(f"Failed: {failed}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
