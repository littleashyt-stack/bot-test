# main.py - Discord Music Bot with Wavelink 3.x (SINGLE FILE)
# Dependencies: discord.py, wavelink, python-dotenv, spotipy (optional), PyNaCl
# Features: YouTube/Spotify support, Now Playing embeds with bot avatar, progress bar
#
# ✅ IMPORTANT:
# Put your .env in the SAME folder as this file.
# This version loads .env relative to THIS file, so it works no matter where you run it from.

import os
import re
import asyncio
import logging
import math
from pathlib import Path
from collections import deque
from typing import Optional, Dict, List, Tuple, Any
from datetime import datetime

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# ===================== LOAD ENV (FIXED PATH) =====================
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("musicbot")

# ===================== CONFIG =====================
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
BOT_PREFIX = os.getenv("BOT_PREFIX", "!")
VOLUME = float(os.getenv("VOLUME", "0.5"))
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "100"))
NOWPLAYING_CHANNEL_ID = os.getenv("NOWPLAYING_CHANNEL_ID")  # Optional: Channel ID for now playing updates

# Lavalink
LAVALINK_HOST = os.getenv("LAVALINK_HOST", "lavalink.silvie.org")
LAVALINK_PORT = int(os.getenv("LAVALINK_PORT", "443"))
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "lavasilvie")
LAVALINK_HTTPS = os.getenv("LAVALINK_HTTPS", "true").lower() == "true"

# Spotify (optional)
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

if not DISCORD_BOT_TOKEN:
    logger.error("Missing DISCORD_BOT_TOKEN in .env (looked for .env at: %s)", str(ENV_PATH))
    raise SystemExit(1)

# ===================== DEPENDENCIES =====================
try:
    import wavelink
    from wavelink import Playable
except ImportError:
    logger.error("wavelink not installed. Install: python -m pip install -U wavelink")
    raise SystemExit(1)

SPOTIFY_AVAILABLE = False
try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    SPOTIFY_AVAILABLE = True
except ImportError:
    logger.warning("spotipy not installed. Spotify features disabled.")

# ===================== HELPERS =====================
URL_RE = re.compile(r"^https?://", re.IGNORECASE)

SPOTIFY_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?(?:embed/)?(track|playlist|album)/([A-Za-z0-9]{22})",
    re.IGNORECASE,
)
SPOTIFY_URI_RE = re.compile(r"spotify:(track|playlist|album):([A-Za-z0-9]{22})", re.IGNORECASE)
SPOTIFY_SHORTLINK_RE = re.compile(r"^https?://spotify\.link/", re.IGNORECASE)


def is_url(s: str) -> bool:
    return bool(URL_RE.match((s or "").strip()))


def format_duration(ms: int) -> str:
    """Convert milliseconds to MM:SS or HH:MM:SS format"""
    seconds_total = ms // 1000
    minutes = seconds_total // 60
    seconds = seconds_total % 60
    hours = minutes // 60
    minutes = minutes % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_time_with_bar(position: int, duration: int, bar_length: int = 20) -> str:
    """Create a progress bar with timestamps"""
    if duration <= 0:
        return "0:00 ┃━━━━━━━━━━━━━━━━━━━━┃ 0:00"
    
    # Calculate percentage
    percent = position / duration if duration > 0 else 0
    
    # Create progress bar
    filled = int(bar_length * percent)
    bar = "━" * filled + "⚪" + "━" * (bar_length - filled - 1)
    
    # Format times
    current_time = format_duration(position)
    total_time = format_duration(duration)
    
    return f"{current_time} ┃{bar}┃ {total_time}"


def safe_track_ms(track) -> int:
    """Safely extract track duration in milliseconds"""
    for attr in ("length", "duration", "length_ms", "duration_ms"):
        v = getattr(track, attr, None)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def short_err(e: Exception, n: int = 140) -> str:
    s = str(e)
    return s if len(s) <= n else (s[: n - 3] + "...")


def parse_spotify_id(query: str) -> Optional[Tuple[str, str]]:
    q = (query or "").strip()
    
    # Skip short links
    if SPOTIFY_SHORTLINK_RE.match(q):
        return None
    
    # Check URI format
    m = SPOTIFY_URI_RE.search(q)
    if m:
        return m.group(1).lower(), m.group(2)
    
    # Check URL format
    m = SPOTIFY_URL_RE.search(q)
    if m:
        return m.group(1).lower(), m.group(2)
    
    return None


def is_spotify_link(query: str) -> bool:
    q = (query or "").lower()
    return ("open.spotify.com" in q) or q.startswith("spotify:") or q.startswith("https://spotify.link") or q.startswith("http://spotify.link")


def spotify_track_url(track_id: Optional[str]) -> Optional[str]:
    if not track_id:
        return None
    return f"https://open.spotify.com/track/{track_id}"


# ===================== DISCORD INTENTS =====================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True
intents.guild_messages = True


# ===================== BOT CLASS =====================
class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=BOT_PREFIX, intents=intents, help_command=None)
        self.queues: Dict[int, deque] = {}
        self.current_song: Dict[int, Optional[Dict]] = {}
        self.loop_mode: Dict[int, bool] = {}
        self.lavalink_connected: bool = False
        self.spotify = None
        
        # Now playing message tracking
        self.now_playing_messages: Dict[int, discord.Message] = {}
        
        # Store node
        self.node: Optional[wavelink.Node] = None

    def get_queue(self, guild_id: int) -> deque:
        if guild_id not in self.queues:
            self.queues[guild_id] = deque(maxlen=MAX_QUEUE_SIZE)
        return self.queues[guild_id]

    def add_many_to_queue(self, guild_id: int, songs: List[Dict]) -> int:
        q = self.get_queue(guild_id)
        space = max(0, MAX_QUEUE_SIZE - len(q))
        to_add = songs[:space]
        q.extend(to_add)
        return len(to_add)

    def clear_queue(self, guild_id: int):
        if guild_id in self.queues:
            self.queues[guild_id].clear()

    def toggle_loop(self, guild_id: int) -> bool:
        self.loop_mode[guild_id] = not self.loop_mode.get(guild_id, False)
        return self.loop_mode[guild_id]

    def get_loop_mode(self, guild_id: int) -> bool:
        return self.loop_mode.get(guild_id, False)

    def get_current_song(self, guild_id: int) -> Optional[Dict]:
        return self.current_song.get(guild_id)

    def set_current_song(self, guild_id: int, song_info: Optional[Dict]):
        self.current_song[guild_id] = song_info

    async def setup_hook(self):
        # Spotify init (optional)
        if SPOTIFY_AVAILABLE and SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
            try:
                auth_manager = SpotifyClientCredentials(
                    client_id=SPOTIFY_CLIENT_ID,
                    client_secret=SPOTIFY_CLIENT_SECRET,
                )
                self.spotify = spotipy.Spotify(auth_manager=auth_manager)
                logger.info("✅ Spotify API connected successfully")
            except Exception as e:
                logger.error(f"Spotify init failed: {short_err(e)}")
                self.spotify = None
        else:
            if SPOTIFY_AVAILABLE:
                logger.warning("Spotify creds missing. Spotify link playback will be disabled.")

        await self.setup_lavalink()

        # Sync slash commands
        try:
            await self.tree.sync()
            logger.info("✅ Slash commands synced")
        except Exception as e:
            logger.error(f"❌ Slash sync failed: {short_err(e)}")

    async def setup_lavalink(self):
        try:
            scheme = "https" if LAVALINK_HTTPS else "http"
            self.node = wavelink.Node(
                uri=f"{scheme}://{LAVALINK_HOST}:{LAVALINK_PORT}",
                password=LAVALINK_PASSWORD,
            )
            await wavelink.Pool.connect(client=self, nodes=[self.node])
            self.lavalink_connected = True
            logger.info("✅ Lavalink connected")
        except Exception as e:
            self.lavalink_connected = False
            logger.error(f"❌ Lavalink connect failed: {short_err(e)}")
            raise

    async def cleanup_now_playing_message(self, guild_id: int):
        """Clean up old now playing message"""
        if guild_id in self.now_playing_messages:
            try:
                await self.now_playing_messages[guild_id].delete()
            except:
                pass
            del self.now_playing_messages[guild_id]


bot = MusicBot()


# ===================== EVENTS =====================
@bot.event
async def on_ready():
    logger.info(f"{bot.user} connected!")
    logger.info(f"Guilds: {len(bot.guilds)}")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name="music | /help")
    )


@bot.event
async def on_wavelink_node_ready(payload):
    try:
        if hasattr(payload, "node") and hasattr(payload.node, "identifier"):
            logger.info(f"✅ Lavalink node ready: {payload.node.identifier}")
        elif hasattr(payload, "identifier"):
            logger.info(f"✅ Lavalink node ready: {payload.identifier}")
        else:
            logger.info("✅ Lavalink node ready")
    except Exception:
        logger.info("✅ Lavalink node ready")


@bot.event
async def on_wavelink_track_end(payload):
    try:
        player = getattr(payload, "player", None)
        if not player or not hasattr(player, "guild") or not player.guild:
            return

        gid = player.guild.id

        # Clean up now playing message
        await bot.cleanup_now_playing_message(gid)

        # Loop current song: requeue at front
        if bot.get_loop_mode(gid):
            current = bot.get_current_song(gid)
            if current and "track" in current:
                bot.get_queue(gid).appendleft(current)

        await play_next_track(player.guild)
    except Exception as e:
        logger.error(f"Track end error: {short_err(e)}")


# ===================== VOICE & SEARCH =====================
async def ensure_voice_connection(ctx_or_interaction):
    if not bot.lavalink_connected:
        raise Exception("Lavalink not connected. Check your Lavalink settings.")

    user = getattr(ctx_or_interaction, "author", None) or getattr(ctx_or_interaction, "user", None)
    if not user or not getattr(user, "voice", None) or not user.voice or not user.voice.channel:
        raise Exception("You must be in a voice channel first!")

    channel = user.voice.channel
    guild = channel.guild

    player = guild.voice_client
    if player and getattr(player, "connected", False):
        if getattr(player, "channel", None) and player.channel and player.channel.id != channel.id:
            await player.move_to(channel)
        return player

    return await channel.connect(cls=wavelink.Player)


async def wavelink_search(query: str):
    if not bot.lavalink_connected:
        return None

    q = (query or "").strip()
    if not q:
        return None

    search_q = q if is_url(q) else f"ytsearch:{q}"
    try:
        return await Playable.search(search_q)
    except Exception as e:
        logger.error(f"Lavalink search error: {short_err(e)}")
        return None


def spotify_fetch_tracks(query: str) -> List[Dict]:
    if not bot.spotify or not SPOTIFY_AVAILABLE:
        return []

    parsed = parse_spotify_id(query)
    if not parsed:
        return []

    kind, sid = parsed
    out: List[Dict] = []

    try:
        if kind == "track":
            t = bot.spotify.track(sid)
            out.append(
                {
                    "title": t["name"],
                    "artists": ", ".join(a["name"] for a in t["artists"]),
                    "duration_ms": t["duration_ms"],
                    "spotify_url": (t.get("external_urls") or {}).get("spotify") or spotify_track_url(t.get("id")),
                }
            )
            return out

        if kind == "playlist":
            page = bot.spotify.playlist_items(
                sid,
                fields="items(track(id,name,artists(name),duration_ms,external_urls)),next",
                additional_types=["track"],
                limit=100,
            )
            while page:
                for item in page.get("items", []):
                    tr = item.get("track")
                    if not tr:
                        continue
                    out.append(
                        {
                            "title": tr["name"],
                            "artists": ", ".join(a["name"] for a in tr.get("artists", [])),
                            "duration_ms": tr.get("duration_ms", 0),
                            "spotify_url": (tr.get("external_urls") or {}).get("spotify") or spotify_track_url(tr.get("id")),
                        }
                    )
                if not page.get("next"):
                    break
                page = bot.spotify.next(page)
            return out

        if kind == "album":
            page = bot.spotify.album_tracks(sid, limit=50)
            while page:
                for tr in page.get("items", []):
                    tid = tr.get("id")
                    out.append(
                        {
                            "title": tr.get("name", "Unknown"),
                            "artists": ", ".join(a["name"] for a in tr.get("artists", [])),
                            "duration_ms": tr.get("duration_ms", 0),
                            "spotify_url": spotify_track_url(tid),
                        }
                    )
                if not page.get("next"):
                    break
                page = bot.spotify.next(page)
            return out

    except Exception as e:
        logger.error(f"Spotify fetch error: {short_err(e)}")
        return []

    return out


async def resolve_to_song_items(query: str, requester_name: str) -> Tuple[List[Dict], str]:
    songs: List[Dict] = []
    qlow = (query or "").lower().strip()

    # Spotify shortlink warning
    if SPOTIFY_SHORTLINK_RE.match((query or "").strip()):
        return [], "Spotify short links (spotify.link/...) are redirects. Please paste the full open.spotify.com link."

    # Spotify
    if is_spotify_link(qlow):
        if not bot.spotify:
            return [], "Spotify is not configured (missing SPOTIFY_CLIENT_ID/SECRET)."

        parsed = parse_spotify_id(query)
        if not parsed:
            return [], "Spotify link not found or unsupported. Paste a full open.spotify.com track/playlist/album link."

        sp_tracks = spotify_fetch_tracks(query)
        if not sp_tracks:
            return [], "Spotify link not found or unsupported (could not fetch tracks)."

        # Convert each Spotify item to a YouTube search and grab first result
        for info in sp_tracks:
            yt_query = f"{info.get('title','')} {info.get('artists','')}".strip()
            if not yt_query:
                continue
            res = await wavelink_search(yt_query)
            if not res:
                continue

            track_obj = res[0] if isinstance(res, list) and res else None
            if not track_obj:
                continue

            dur_ms = int(info.get("duration_ms") or 0)
            songs.append(
                {
                    "title": info.get("title") or getattr(track_obj, "title", "Unknown"),
                    "artists": info.get("artists"),
                    "duration_ms": dur_ms or safe_track_ms(track_obj),
                    "duration": format_duration(dur_ms) if dur_ms else (format_duration(safe_track_ms(track_obj)) if safe_track_ms(track_obj) else "Unknown"),
                    "spotify_url": info.get("spotify_url"),
                    "track": track_obj,
                    "requester": requester_name,
                }
            )

        if not songs:
            return [], "Could not find playable audio for this Spotify content."
        return songs, "ok"

    # YouTube
    res = await wavelink_search(query)
    if not res:
        return [], "Track not found."

    if isinstance(res, list) and res:
        for track_obj in res[:10]:
            ms = safe_track_ms(track_obj)
            songs.append(
                {
                    "title": getattr(track_obj, "title", "Unknown"),
                    "duration_ms": ms,
                    "duration": format_duration(ms) if ms else "Unknown",
                    "track": track_obj,
                    "requester": requester_name,
                }
            )
        return songs, "ok"

    return [], "No tracks found."


# ===================== NOW PLAYING EMBED =====================
async def get_now_playing_embed(guild: discord.Guild, player=None) -> Optional[discord.Embed]:
    """Create a now playing embed with progress bar"""
    if not guild:
        return None

    gid = guild.id
    current = bot.get_current_song(gid)
    if not current:
        return None

    # Get bot avatar
    bot_avatar_url = None
    try:
        bot_avatar_url = bot.user.display_avatar.url
    except Exception:
        pass

    # Create embed
    embed = discord.Embed(
        title="🎵 Now Playing",
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )

    # Add title
    title_text = f"**{current.get('title', 'Unknown')}**"
    if current.get('spotify_url'):
        title_text = f"[{title_text}]({current['spotify_url']})"
    embed.add_field(name="Title", value=title_text, inline=False)

    # Add artists if available
    if current.get('artists'):
        embed.add_field(name="Artist(s)", value=current['artists'], inline=False)

    # Add progress bar
    if player and hasattr(player, 'position') and current.get('duration_ms'):
        position = getattr(player, 'position', 0)
        duration = current.get('duration_ms', 0)
        
        # Create progress bar
        progress_bar = format_time_with_bar(position, duration)
        embed.add_field(name="Progress", value=f"```\n{progress_bar}\n```", inline=False)
        
        # Add position/duration as separate field
        percentage = (position / duration * 100) if duration > 0 else 0
        time_left = format_duration(max(0, duration - position))
        embed.add_field(name="Time", value=f"**{format_duration(position)}** / **{format_duration(duration)}**", inline=True)
        embed.add_field(name="Remaining", value=time_left, inline=True)
        embed.add_field(name="% Complete", value=f"{percentage:.1f}%", inline=True)
    else:
        # Fallback if no progress available
        if current.get('duration'):
            embed.add_field(name="Duration", value=current['duration'], inline=True)

    # Add requester
    embed.add_field(name="Requested by", value=current.get('requester', 'Unknown'), inline=True)

    # Add loop status
    loop_status = "🔁 ON" if bot.get_loop_mode(gid) else "🔁 OFF"
    embed.add_field(name="Loop", value=loop_status, inline=True)

    # Add volume
    if player and hasattr(player, 'volume'):
        embed.add_field(name="Volume", value=f"{player.volume}%", inline=True)

    # Add bot avatar as thumbnail
    if bot_avatar_url:
        embed.set_thumbnail(url=bot_avatar_url)

    # Add footer
    embed.set_footer(text="Now Playing • Use /skip to skip")

    return embed


async def update_now_playing_message(guild: discord.Guild, player=None):
    """Update or create now playing message in designated channel"""
    if not guild:
        return

    gid = guild.id
    
    # Get the channel for now playing updates
    channel = None
    if NOWPLAYING_CHANNEL_ID:
        try:
            channel = guild.get_channel(int(NOWPLAYING_CHANNEL_ID))
        except:
            pass
    
    # If no specific channel, use first text channel bot can send to
    if not channel:
        for ch in guild.text_channels:
            me = guild.me
            if me and ch.permissions_for(me).send_messages:
                channel = ch
                break
    
    if not channel:
        return
    
    # Get current embed
    embed = await get_now_playing_embed(guild, player)
    if not embed:
        return
    
    # Clean up old message
    await bot.cleanup_now_playing_message(gid)
    
    try:
        # Send new message
        message = await channel.send(embed=embed)
        bot.now_playing_messages[gid] = message
    except Exception as e:
        logger.warning(f"Failed to send now playing message: {short_err(e)}")


# ===================== PLAYBACK =====================
async def play_next_track(guild: discord.Guild):
    if not bot.lavalink_connected:
        return

    gid = guild.id
    queue = bot.get_queue(gid)

    if not queue:
        bot.set_current_song(gid, None)
        await bot.cleanup_now_playing_message(gid)
        return

    song = queue.popleft()
    bot.set_current_song(gid, song)

    player = guild.voice_client
    if not player or not getattr(player, "connected", False):
        return

    try:
        await player.set_volume(int(VOLUME * 100))
        track = song.get("track")
        if not track:
            await play_next_track(guild)
            return

        await player.play(track)
        
        # Send now playing message
        await update_now_playing_message(guild, player)

    except Exception as e:
        logger.error(f"Play error: {short_err(e)}")
        await asyncio.sleep(1)
        await play_next_track(guild)


# ===================== SLASH COMMANDS =====================
@bot.tree.command(name="help", description="Show bot help")
async def help_slash(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎵 Music Bot Commands",
        description="Here are all available commands:",
        color=discord.Color.blue(),
    )

    commands_list = [
        ("`/join`", "Join your voice channel"),
        ("`/leave`", "Leave the voice channel"),
        ("`/play <query>`", "Play a song (YouTube/Spotify)"),
        ("`/pause`", "Pause the current song"),
        ("`/resume`", "Resume the paused song"),
        ("`/skip`", "Skip the current song"),
        ("`/queue`", "Show the current queue"),
        ("`/loop`", "Toggle loop mode"),
        ("`/clear`", "Clear the queue"),
        ("`/nowplaying`", "Show current song info with progress bar"),
        ("`/volume <0-100>`", "Set volume level"),
    ]

    for cmd, desc in commands_list:
        embed.add_field(name=cmd, value=desc, inline=False)

    embed.set_footer(text=f"Prefix commands also available with {BOT_PREFIX}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="join", description="Join your voice channel")
async def join_slash(interaction: discord.Interaction):
    try:
        player = await ensure_voice_connection(interaction)
        await interaction.response.send_message(f"✅ Joined **{player.channel.name}**!")
    except Exception as e:
        await interaction.response.send_message(f"❌ {short_err(e)}", ephemeral=True)


@bot.tree.command(name="leave", description="Leave the voice channel")
async def leave_slash(interaction: discord.Interaction):
    player = interaction.guild.voice_client
    gid = interaction.guild.id

    if player and getattr(player, "connected", False):
        await bot.cleanup_now_playing_message(gid)
        await player.disconnect()
        bot.clear_queue(gid)
        bot.set_current_song(gid, None)
        await interaction.response.send_message("✅ Left voice.")
    else:
        await interaction.response.send_message("❌ Not connected.", ephemeral=True)


@bot.tree.command(name="play", description="Play a song (YouTube search/URL or Spotify link)")
@app_commands.describe(query="Song name or URL (YouTube/Spotify)")
async def play_slash(interaction: discord.Interaction, query: str):
    if not bot.lavalink_connected:
        await interaction.response.send_message("❌ Lavalink not connected.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    try:
        player = await ensure_voice_connection(interaction)

        songs, hint = await resolve_to_song_items(query, requester_name=interaction.user.name)
        if not songs:
            await interaction.followup.send(f"❌ {hint}")
            return

        added = bot.add_many_to_queue(interaction.guild.id, songs)
        if added <= 0:
            await interaction.followup.send("❌ Queue is full!")
            return

        if len(songs) == 1:
            await interaction.followup.send(f"✅ Added to queue: **{songs[0]['title']}**")
        else:
            await interaction.followup.send(f"✅ Added **{added}** track(s) to queue.")

        if not getattr(player, "playing", False):
            await play_next_track(interaction.guild)

    except Exception as e:
        await interaction.followup.send(f"❌ {short_err(e)}")


@bot.tree.command(name="pause", description="Pause the current song")
async def pause_slash(interaction: discord.Interaction):
    player = interaction.guild.voice_client
    if player and getattr(player, "playing", False):
        await player.pause()
        await update_now_playing_message(interaction.guild, player)
        await interaction.response.send_message("⏸️ Paused.")
    else:
        await interaction.response.send_message("❌ Nothing playing.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume the paused song")
async def resume_slash(interaction: discord.Interaction):
    player = interaction.guild.voice_client
    if player and getattr(player, "paused", False):
        await player.resume()
        await update_now_playing_message(interaction.guild, player)
        await interaction.response.send_message("▶️ Resumed.")
    else:
        await interaction.response.send_message("❌ Nothing paused.", ephemeral=True)


@bot.tree.command(name="skip", description="Skip the current song")
async def skip_slash(interaction: discord.Interaction):
    player = interaction.guild.voice_client
    if player and (getattr(player, "playing", False) or getattr(player, "paused", False)):
        await bot.cleanup_now_playing_message(interaction.guild.id)
        await player.stop()
        await interaction.response.send_message("⏭️ Skipped.")
    else:
        await interaction.response.send_message("❌ Nothing to skip.", ephemeral=True)


@bot.tree.command(name="queue", description="Show the current queue")
async def queue_slash(interaction: discord.Interaction):
    gid = interaction.guild.id
    q = bot.get_queue(gid)
    current = bot.get_current_song(gid)

    embed = discord.Embed(title="🎶 Music Queue", color=discord.Color.blue())

    if current:
        embed.add_field(
            name="🎵 Now Playing",
            value=f"**{current.get('title','Unknown')}**\n👤 {current.get('requester','Unknown')}",
            inline=False,
        )

    if q:
        lines = []
        for i, s in enumerate(list(q)[:10], 1):
            lines.append(f"{i}. **{s.get('title','Unknown')}** — 👤 {s.get('requester','Unknown')}")
        embed.add_field(name=f"📋 Up Next ({len(q)})", value="\n".join(lines) if lines else "Empty", inline=False)
    elif not current:
        embed.description = "Queue is empty."

    player = interaction.guild.voice_client
    if player and hasattr(player, "volume"):
        embed.add_field(name="Volume", value=f"{player.volume}%", inline=True)

    embed.set_footer(text=f"Loop: {'🔁 ON' if bot.get_loop_mode(gid) else '🔁 OFF'}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="loop", description="Toggle loop mode (loops current song)")
async def loop_slash(interaction: discord.Interaction):
    enabled = bot.toggle_loop(interaction.guild.id)
    await update_now_playing_message(interaction.guild)
    await interaction.response.send_message(f"🔁 Loop {'enabled' if enabled else 'disabled'}")


@bot.tree.command(name="clear", description="Clear the queue")
async def clear_slash(interaction: discord.Interaction):
    gid = interaction.guild.id
    was_playing = bot.get_current_song(gid) is not None
    bot.clear_queue(gid)

    if not was_playing:
        await bot.cleanup_now_playing_message(gid)

    await interaction.response.send_message("🧹 Queue cleared!")


@bot.tree.command(name="nowplaying", description="Show current song with progress bar")
async def nowplaying_slash(interaction: discord.Interaction):
    player = interaction.guild.voice_client
    embed = await get_now_playing_embed(interaction.guild, player)
    
    if embed:
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)


@bot.tree.command(name="volume", description="Set volume (0-100)")
@app_commands.describe(level="0-100")
async def volume_slash(interaction: discord.Interaction, level: int):
    if level < 0 or level > 100:
        await interaction.response.send_message("❌ Volume must be 0-100.", ephemeral=True)
        return
    player = interaction.guild.voice_client
    if not player:
        await interaction.response.send_message("❌ Not connected to voice.", ephemeral=True)
        return
    await player.set_volume(level)
    await update_now_playing_message(interaction.guild, player)
    await interaction.response.send_message(f"🔊 Volume set to {level}%.")


# ===================== PREFIX COMMANDS =====================
@bot.command(name="play", aliases=["p"])
async def play_cmd(ctx, *, query: str):
    if not bot.lavalink_connected:
        await ctx.send("❌ Lavalink not connected.")
        return
    try:
        player = await ensure_voice_connection(ctx)

        songs, hint = await resolve_to_song_items(query, requester_name=ctx.author.name)
        if not songs:
            await ctx.send(f"❌ {hint}")
            return

        added = bot.add_many_to_queue(ctx.guild.id, songs)
        if added <= 0:
            await ctx.send("❌ Queue is full!")
            return

        if len(songs) == 1:
            await ctx.send(f"✅ Added: **{songs[0]['title']}**")
        else:
            await ctx.send(f"✅ Added **{added}** track(s).")

        if not getattr(player, "playing", False):
            await play_next_track(ctx.guild)

    except Exception as e:
        await ctx.send(f"❌ {short_err(e)}")


@bot.command(name="leave", aliases=["disconnect", "dc"])
async def leave_cmd(ctx):
    player = ctx.guild.voice_client
    gid = ctx.guild.id

    if player and getattr(player, "connected", False):
        await bot.cleanup_now_playing_message(gid)
        await player.disconnect()
        bot.clear_queue(gid)
        bot.set_current_song(gid, None)
        await ctx.send("✅ Left voice.")
    else:
        await ctx.send("❌ Not connected.")


@bot.command(name="skip", aliases=["s", "next"])
async def skip_cmd(ctx):
    player = ctx.guild.voice_client
    if player and (getattr(player, "playing", False) or getattr(player, "paused", False)):
        await bot.cleanup_now_playing_message(ctx.guild.id)
        await player.stop()
        await ctx.send("⏭️ Skipped.")
    else:
        await ctx.send("❌ Nothing to skip.")


@bot.command(name="clear", aliases=["cq"])
async def clear_cmd(ctx):
    gid = ctx.guild.id
    was_playing = bot.get_current_song(gid) is not None
    bot.clear_queue(gid)

    if not was_playing:
        await bot.cleanup_now_playing_message(gid)

    await ctx.send("🧹 Queue cleared!")


@bot.command(name="pause")
async def pause_cmd(ctx):
    player = ctx.guild.voice_client
    if player and getattr(player, "playing", False):
        await player.pause()
        await update_now_playing_message(ctx.guild, player)
        await ctx.send("⏸️ Paused.")
    else:
        await ctx.send("❌ Nothing playing.")


@bot.command(name="resume")
async def resume_cmd(ctx):
    player = ctx.guild.voice_client
    if player and getattr(player, "paused", False):
        await player.resume()
        await update_now_playing_message(ctx.guild, player)
        await ctx.send("▶️ Resumed.")
    else:
        await ctx.send("❌ Nothing paused.")


@bot.command(name="queue", aliases=["q"])
async def queue_cmd(ctx):
    gid = ctx.guild.id
    q = bot.get_queue(gid)
    current = bot.get_current_song(gid)

    embed = discord.Embed(title="🎶 Music Queue", color=discord.Color.blue())

    if current:
        embed.add_field(
            name="🎵 Now Playing",
            value=f"**{current.get('title','Unknown')}**\n👤 {current.get('requester','Unknown')}",
            inline=False,
        )

    if q:
        lines = []
        for i, s in enumerate(list(q)[:10], 1):
            lines.append(f"{i}. **{s.get('title','Unknown')}** — 👤 {s.get('requester','Unknown')}")
        embed.add_field(name=f"📋 Up Next ({len(q)})", value="\n".join(lines) if lines else "Empty", inline=False)
    elif not current:
        embed.description = "Queue is empty."

    player = ctx.guild.voice_client
    if player and hasattr(player, "volume"):
        embed.add_field(name="Volume", value=f"{player.volume}%", inline=True)

    embed.set_footer(text=f"Loop: {'🔁 ON' if bot.get_loop_mode(gid) else '🔁 OFF'}")
    await ctx.send(embed=embed)


@bot.command(name="nowplaying", aliases=["np", "current"])
async def nowplaying_cmd(ctx):
    player = ctx.guild.voice_client
    embed = await get_now_playing_embed(ctx.guild, player)
    
    if embed:
        await ctx.send(embed=embed)
    else:
        await ctx.send("❌ Nothing is playing.")


@bot.command(name="loop", aliases=["repeat"])
async def loop_cmd(ctx):
    enabled = bot.toggle_loop(ctx.guild.id)
    await update_now_playing_message(ctx.guild)
    await ctx.send(f"🔁 Loop {'enabled' if enabled else 'disabled'}")


@bot.command(name="volume", aliases=["vol"])
async def volume_cmd(ctx, level: int):
    if level < 0 or level > 100:
        await ctx.send("❌ Volume must be 0-100.")
        return
    player = ctx.guild.voice_client
    if not player:
        await ctx.send("❌ Not connected to voice.")
        return
    await player.set_volume(level)
    await update_now_playing_message(ctx.guild, player)
    await ctx.send(f"🔊 Volume set to {level}%.")


# ===================== ERROR HANDLING =====================
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid argument.")
    else:
        logger.error(f"Command error in {getattr(ctx, 'command', None)}: {error}")
        await ctx.send(f"❌ Error: {short_err(error)}")


# ===================== RUN =====================
if __name__ == "__main__":
    print("=" * 50)
    print("🎵 Discord Music Bot (Wavelink 3.x)")
    print("=" * 50)
    logger.info(f"Prefix: {BOT_PREFIX}")
    logger.info(f"Lavalink: {LAVALINK_HOST}:{LAVALINK_PORT}")
    logger.info(f"Spotify: {'Enabled' if SPOTIFY_AVAILABLE and SPOTIFY_CLIENT_ID else 'Disabled'}")
    if NOWPLAYING_CHANNEL_ID:
        logger.info(f"Now Playing Channel ID: {NOWPLAYING_CHANNEL_ID}")
    logger.info(f"Loaded .env from: {ENV_PATH}")

    try:
        bot.run(DISCORD_BOT_TOKEN)
    except discord.LoginFailure:
        logger.error("❌ Invalid Discord bot token. Check DISCORD_BOT_TOKEN in .env")
        raise SystemExit(1)
    except Exception as e:
        logger.error(f"❌ Failed to start: {short_err(e)}")
        raise SystemExit(1)