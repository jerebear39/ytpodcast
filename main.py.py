import asyncio
import re
import xml.etree.ElementTree as ET
from email.utils import format_datetime
from datetime import datetime
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, Response, StreamingResponse

app = FastAPI(title="YouTube to Podcast RSS")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

async def resolve_channel_id(url_or_handle: str) -> str:
    """Extracts UC... channel ID from a URL, handle, or raw ID."""
    clean_input = url_or_handle.strip()
    
    # If already a valid channel ID
    if re.fullmatch(r"UC[a-zA-Z0-9_-]{22}", clean_input):
        return clean_input

    # Normalize URL
    if not clean_input.startswith("http"):
        clean_input = f"https://www.youtube.com/{clean_input}"

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=10.0) as client:
        res = await client.get(clean_input)
        if res.status_code != 200:
            raise HTTPException(status_code=400, detail="Could not reach YouTube channel")

        # Match canonical channel ID from HTML
        match = re.search(r'itemprop="identifier"\s+content="(UC[\w-]{22})"', res.text)
        if not match:
            match = re.search(r'"channelId":"(UC[\w-]{22})"', res.text)
            
        if match:
            return match.group(1)

    raise HTTPException(status_code=404, detail="Could not extract Channel ID")


@app.get("/", response_class=HTMLResponse)
async def home():
    """Simple UI to generate feeds."""
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>YouTube to Podcast RSS</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
            .card { background: #1e293b; padding: 2rem; border-radius: 12px; width: 100%; max-width: 480px; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.5); }
            h2 { margin-top: 0; }
            input { width: 100%; padding: 0.75rem; border-radius: 6px; border: 1px solid #334155; background: #0f172a; color: #fff; margin-bottom: 1rem; box-sizing: border-box; }
            button { width: 100%; padding: 0.75rem; border-radius: 6px; border: none; background: #38bdf8; color: #0f172a; font-weight: bold; cursor: pointer; }
            button:hover { background: #0ea5e9; }
            #result { margin-top: 1rem; word-break: break-all; font-family: monospace; padding: 0.75rem; background: #0f172a; border-radius: 6px; display: none; }
        </style>
    </head>
    <body>
        <div class="card">
            <h2>YouTube &rarr; Podcast RSS</h2>
            <p style="color: #94a3b8; font-size: 0.9rem;">Paste a channel URL or handle (e.g. <code>@veritasium</code>):</p>
            <input type="text" id="channelInput" placeholder="https://www.youtube.com/@veritasium" />
            <button onclick="generate()">Generate RSS Feed</button>
            <div id="result"></div>
        </div>

        <script>
            async function generate() {
                const val = document.getElementById('channelInput').value.trim();
                const resBox = document.getElementById('result');
                if (!val) return;
                
                resBox.style.display = "block";
                resBox.innerText = "Resolving...";
                
                try {
                    const host = window.location.origin;
                    const res = await fetch(`/resolve?input=${encodeURIComponent(val)}`);
                    const data = await res.json();
                    if(data.channel_id) {
                        const feedUrl = `${host}/feed/${data.channel_id}`;
                        resBox.innerHTML = `<strong>Feed URL:</strong><br><a style="color:#38bdf8;" href="${feedUrl}" target="_blank">${feedUrl}</a>`;
                    } else {
                        resBox.innerText = "Failed: " + (data.detail || "Unknown error");
                    }
                } catch(e) {
                    resBox.innerText = "Error resolving channel.";
                }
            }
        </script>
    </body>
    </html>
    """

@app.get("/resolve")
async def resolve(input: str):
    cid = await resolve_channel_id(input)
    return {"channel_id": cid}


@app.get("/feed/{channel_id}")
async def get_podcast_feed(channel_id: str, request: Request):
    """Fetches YouTube native feed and converts to Podcast RSS 2.0."""
    yt_feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    
    async with httpx.AsyncClient(headers=HEADERS, timeout=10.0) as client:
        res = await client.get(yt_feed_url)
        if res.status_code != 200:
            raise HTTPException(status_code=404, detail="YouTube feed not found")

    root = ET.fromstring(res.text)
    channel_title = root.findtext("atom:title", namespaces=ATOM_NS) or "YouTube Channel"
    channel_url = root.find("atom:link[@rel='alternate']", namespaces=ATOM_NS).attrib.get("href", "")

    base_url = str(request.base_url).rstrip("/")

    # Build standard RSS 2.0 with iTunes extensions
    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"
    })
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = channel_title
    ET.SubElement(channel, "link").text = channel_url
    ET.SubElement(channel, "description").text = f"Audio podcast feed for {channel_title}"
    ET.SubElement(channel, "itunes:author").text = channel_title

    for entry in root.findall("atom:entry", namespaces=ATOM_NS):
        video_id = entry.findtext("yt:videoId", namespaces=ATOM_NS)
        title = entry.findtext("atom:title", namespaces=ATOM_NS)
        published_str = entry.findtext("atom:published", namespaces=ATOM_NS)
        
        media_group = entry.find("media:group", namespaces=ATOM_NS)
        desc = ""
        thumb_url = ""
        if media_group is not None:
            desc = media_group.findtext("media:description", namespaces=ATOM_NS) or ""
            thumb = media_group.find("media:thumbnail", namespaces=ATOM_NS)
            if thumb is not None:
                thumb_url = thumb.attrib.get("url", "")

        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = title
        ET.SubElement(item, "guid").text = video_id
        ET.SubElement(item, "link").text = f"https://www.youtube.com/watch?v={video_id}"
        ET.SubElement(item, "description").text = desc

        if published_str:
            dt = datetime.fromisoformat(published_str)
            ET.SubElement(item, "pubDate").text = format_datetime(dt)

        if thumb_url:
            ET.SubElement(item, "itunes:image", {"href": thumb_url})

        # The Enclosure tag points to our on-demand audio streamer
        audio_stream_url = f"{base_url}/audio/{video_id}.m4a"
        ET.SubElement(item, "enclosure", {
            "url": audio_stream_url,
            "length": "0",  # Chunked transfer
            "type": "audio/mp4"
        })

    return Response(
        content=ET.tostring(rss, encoding="utf-8", xml_declaration=True),
        media_type="application/rss+xml"
    )


@app.get("/audio/{video_id}.m4a")
async def stream_audio(video_id: str):
    """Pipes the best audio stream directly using yt-dlp stdout."""
    # Ensure ID contains only standard base64/alphanumeric characters
    if not re.fullmatch(r"[\w-]{11}", video_id):
        raise HTTPException(status_code=400, detail="Invalid video ID")

    video_url = f"https://www.youtube.com/watch?v={video_id}"

    # Use yt-dlp to stream best m4a/aac audio to stdout directly
    cmd = [
        "yt-dlp",
        "-f", "ba[ext=m4a]/ba",
        "-o", "-",
        "--quiet",
        "--no-playlist",
        video_url
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL
    )

    async def audio_generator():
        try:
            while True:
                chunk = await proc.stdout.read(64 * 1024)  # 64 KB chunks
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    return StreamingResponse(
        audio_generator(),
        media_type="audio/mp4",
        headers={"Content-Disposition": f'inline; filename="{video_id}.m4a"'}
    )