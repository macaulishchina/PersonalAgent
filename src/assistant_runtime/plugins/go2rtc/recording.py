"""On-demand gateway H.264 clips, finalized using the maintained FFmpeg package."""

from assistant_runtime.api.system import system

import asyncio
from pathlib import Path
import subprocess
import time
import uuid

import aiohttp
import imageio_ffmpeg
from imageio_ffmpeg._parsing import parse_ffmpeg_header

from assistant_runtime.api.contracts import VideoClip, MAX_VIDEO_BYTES
from .camera import GatewayUnavailable


def inspect_clip(path):
    # The package is pinned. Reuse its metadata parser with an owned subprocess:
    # its streaming reader leaves pipes unclosed when a one-frame process exits.
    result = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-nostdin",
            "-hide_banner",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    if result.returncode:
        raise ValueError("Recorded video has no decodable frame")
    metadata = parse_ffmpeg_header(result.stderr.decode(errors="replace"))
    width, height = metadata["size"]
    if metadata["codec"] != "h264" or not 0 < width <= 1920 or not 0 < height <= 1080:
        raise ValueError("Recorded video has unsupported format or dimensions")
    if len(result.stdout) != width * height * 3:
        raise ValueError("Recorded video has no decodable frame")
    return width, height, float(metadata["duration"])


async def owned_write(handle, block):
    worker = asyncio.create_task(asyncio.to_thread(handle.write, block))
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        await worker
        raise


async def finalize(source, target, duration):
    child = await system.spawn(
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "copy",
        "-t",
        str(duration),
        "-movflags",
        "+faststart",
        str(target),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(15):
            if await child.wait():
                raise ValueError("Recorded video could not be finalized")
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


async def record_clip(camera, folder, duration):
    if folder is None:
        raise RuntimeError("Video recording storage is not configured")
    if camera.closed or camera.session is None:
        raise RuntimeError("Camera provider closed or not started")
    if camera.lock.locked():
        raise RuntimeError("Camera busy; another acquisition is in progress")
    folder = Path(folder)
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    ident = uuid.uuid4().hex
    source_path, target = folder / f"{ident}.part", folder / f"{ident}.mp4"
    alias = "clip_" + ident
    task = asyncio.current_task()
    camera.operations.add(task)
    alias_attempted = False
    completed = False
    try:
        async with camera.lock, asyncio.timeout(duration + camera.timeout + 20):
            camera.recording = True
            identity = await camera.source_identity()
            epoch = camera.generation
            # GetOrPatch's named temporary stream uses the registered source. Camera
            # URLs/credentials and vendor protocols stay inside the gateway.
            params = {
                "src": f"ffmpeg:{camera.stream}#video=h264#width=1280#height=720",
                "name": alias,
                "video": "h264",
                "duration": duration,
            }
            alias_attempted = True
            async with camera.session.get(
                camera.base_url + "/api/stream.mp4",
                params=params,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=duration + camera.timeout + 5, connect=2),
            ) as response:
                if response.status != 200 or response.content_type != "video/mp4":
                    raise GatewayUnavailable("go2rtc video recording unavailable")
                recorded_at = time.time()
                received = 0
                with source_path.open("xb") as handle:
                    system.private(source_path)
                    async for block in response.content.iter_chunked(65536):
                        received += len(block)
                        if received > MAX_VIDEO_BYTES:
                            raise ValueError("Recorded video exceeded 64 MiB")
                        await owned_write(handle, block)
            if identity != await camera.source_identity() or epoch != camera.generation:
                raise RuntimeError("Camera source changed; recording discarded")
            await finalize(source_path, target, duration)
            system.private(target)
            worker = asyncio.create_task(asyncio.to_thread(inspect_clip, target))
            try:
                width, height, actual_duration = await asyncio.shield(worker)
            except asyncio.CancelledError:
                await asyncio.gather(worker, return_exceptions=True)
                raise
            if actual_duration < max(0.25, duration - 0.75) or actual_duration > duration + 1:
                raise ValueError("Recording ended before the requested duration")
            clip = VideoClip(
                camera.device_id,
                target,
                width,
                height,
                actual_duration,
                target.stat().st_size,
                recorded_at,
                epoch,
            )
            camera.last_error = None
            completed = True
            return clip
    except BaseException as exc:
        camera.last_error = (
            None
            if isinstance(exc, asyncio.CancelledError)
            else ("Video recording timed out" if isinstance(exc, TimeoutError) else str(exc)[:200])
        )
        if isinstance(exc, (TimeoutError, aiohttp.ClientError)):
            raise GatewayUnavailable("go2rtc recording failed or timed out") from None
        raise
    finally:
        try:
            if alias_attempted:
                try:
                    async with asyncio.timeout(2):
                        await camera.read("/api/streams", 4096, method="DELETE", params={"src": alias})
                except (RuntimeError, ValueError, TimeoutError, aiohttp.ClientError):
                    pass  # Closed consumers stop encoding; outages are never replayed.
        except asyncio.CancelledError:
            completed = False
            raise
        finally:
            source_path.unlink(missing_ok=True)
            if not completed:
                target.unlink(missing_ok=True)
            camera.operations.discard(task)
            camera.recording = False
