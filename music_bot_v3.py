"""
🎵 Discord Music Bot v3
Нове у v3: COOKIES_B64 (env var з base64 cookies.txt),
           iOS player client для обходу YouTube bot-detection,
           DISCORD_TOKEN з env (без fallback)
"""

import discord
import asyncio
import yt_dlp
import random
import time
import re
import os
import base64
import tempfile
from discord.ext import commands
from discord import app_commands, ui, ButtonStyle
from typing import Optional, List

# ─── Токен та налаштування ───────────────────────────────────────────────────
TOKEN  = os.environ.get('DISCORD_TOKEN')
PREFIX = '!'
INACTIVITY_TIMEOUT = 300  # 5 хвилин без музики → відключення

# ─── COOKIES (v3) ─────────────────────────────────────────────────────────────
# Варіант А — COOKIES_B64 (рекомендовано для Railway):
#   1. Створи окремий Google-акаунт (не особистий!) спеціально для бота
#   2. Зайди на youtube.com в браузері з цим акаунтом
#   3. Розширення "Get cookies.txt LOCALLY" → Export
#   4. Закодуй: base64 -w 0 cookies.txt  (Linux/Mac) або через Python
#   5. Додай у Railway Variables: COOKIES_B64 = <закодований рядок>
#
# Варіант Б — файл cookies.txt поряд з ботом:
#   COOKIE_FILE = 'cookies.txt'
COOKIE_BROWSER: Optional[str] = None
COOKIE_FILE:    Optional[str] = None

# Розпакувати COOKIES_B64 → тимчасовий файл
_COOKIES_TMP: Optional[str] = None
_cookies_b64 = os.environ.get('COOKIES_B64', '')
if _cookies_b64:
    try:
        _decoded = base64.b64decode(_cookies_b64.encode())
        _tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.txt', mode='wb')
        _tmp.write(_decoded)
        _tmp.close()
        _COOKIES_TMP = _tmp.name
        print(f'[cookies] Завантажено з COOKIES_B64 → {_COOKIES_TMP}')
    except Exception as e:
        print(f'[cookies] Помилка розпакування COOKIES_B64: {e}')

# Активний файл cookies: спочатку з env, потім локальний
_ACTIVE_COOKIE_FILE: Optional[str] = _COOKIES_TMP or COOKIE_FILE

# ─── YT-DLP налаштування ─────────────────────────────────────────────────────
def _build_ytdl_base() -> dict:
    base = {
        'format': 'bestaudio/best',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'nocheckcertificate': True,
        'quiet': True,
        'no_warnings': True,
        'source_address': '0.0.0.0',
        'socket_timeout': 15,
        'retries': 3,
        # (v3) iOS player client — обхід YouTube bot-detection на datacenter IP
        'extractor_args': {'youtube': {'player_client': ['ios']}},
    }
    if COOKIE_BROWSER:
        base['cookiesfrombrowser'] = (COOKIE_BROWSER,)
    elif _ACTIVE_COOKIE_FILE:
        base['cookiefile'] = _ACTIVE_COOKIE_FILE
    return base

_YTDL_BASE = _build_ytdl_base()

YTDL_OPTIONS = {
    **_YTDL_BASE,
    'ignoreerrors': False,
    'noplaylist': False,
    'default_search': 'ytsearch',
    'extract_flat': False,
}

YTDL_SEARCH_OPTIONS = {
    **_YTDL_BASE,
    'ignoreerrors': True,
    'noplaylist': True,
    'extract_flat': True,
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_delay_max 3',
    'options': '-vn',
}

ytdl        = yt_dlp.YoutubeDL(YTDL_OPTIONS)
ytdl_search = yt_dlp.YoutubeDL(YTDL_SEARCH_OPTIONS)

# ─── Семафор і кеш (захист від флуду YouTube) ────────────────────────────────
_ytdl_semaphore: asyncio.Semaphore = asyncio.Semaphore(5)
_url_cache: dict[str, tuple[str, float]] = {}
CACHE_TTL = 7200  # 2 години


async def _ytdl_extract(url: str, **kwargs) -> Optional[dict]:
    loop = asyncio.get_event_loop()
    async with _ytdl_semaphore:
        try:
            return await loop.run_in_executor(
                None, lambda: ytdl.extract_info(url, download=False, **kwargs)
            )
        except BaseException as e:
            print(f'[yt-dlp] помилка: {e}')
            return None


async def _get_stream_url(webpage_url: str) -> Optional[str]:
    now = time.time()
    if webpage_url in _url_cache:
        stream_url, expires = _url_cache[webpage_url]
        if now < expires:
            print(f'[cache] HIT {webpage_url[:60]}')
            return stream_url
        else:
            del _url_cache[webpage_url]

    print(f'[cache] MISS {webpage_url[:60]}')
    data = await _ytdl_extract(webpage_url)
    if not data:
        return None
    if 'entries' in data:
        entries = [e for e in data['entries'] if e]
        data = entries[0] if entries else None
    if not data:
        return None

    stream_url = data.get('url')
    if stream_url:
        _url_cache[webpage_url] = (stream_url, now + CACHE_TTL)
    return stream_url


# ─── Кольори ──────────────────────────────────────────────────────────────────
COLOR_BLUE   = 0x5865F2
COLOR_GREEN  = 0x57F287
COLOR_RED    = 0xED4245
COLOR_YELLOW = 0xFEE75C
COLOR_PURPLE = 0x9B59B6
COLOR_CYAN   = 0x1ABC9C

# ─── Аудіофільтри ────────────────────────────────────────────────────────────
class AudioFilter:
    NONE      = 'none'
    BASSBOOST = 'bassboost'
    NIGHTCORE = 'nightcore'

FILTER_LABELS = {
    AudioFilter.NONE:      ('🎵 Без фільтру',  COLOR_BLUE),
    AudioFilter.BASSBOOST: ('🔊 Bass Boost',   COLOR_PURPLE),
    AudioFilter.NIGHTCORE: ('⚡ Nightcore',    COLOR_CYAN),
}

FILTER_AF = {
    AudioFilter.NONE:      '',
    AudioFilter.BASSBOOST: 'bass=g=15,dynaudnorm=f=200',
    AudioFilter.NIGHTCORE: 'aresample=48000,asetrate=48000*1.25,dynaudnorm=f=150',
}

def build_ffmpeg_options(audio_filter: str = AudioFilter.NONE) -> dict:
    af = FILTER_AF.get(audio_filter, '')
    opts = '-vn'
    if af:
        opts += f' -af "{af}"'
    return {
        'before_options': '-reconnect 1 -reconnect_delay_max 3',
        'options': opts,
    }


# ─── Трек ─────────────────────────────────────────────────────────────────────
class Track:
    def __init__(self, data: dict, requester: discord.Member):
        self.url:         str = data.get('url', '')
        self.webpage_url: str = data.get('webpage_url', data.get('url', ''))
        self.title:       str = data.get('title', 'Невідома назва')
        self.duration:    int = data.get('duration', 0)
        self.thumbnail:   str = data.get('thumbnail', '')
        self.uploader:    str = data.get('uploader', 'Невідомо')
        self.video_id:    str = data.get('id', '')
        self.requester:  discord.Member = requester
        self.start_time: float = 0.0

    @property
    def duration_str(self) -> str:
        if not self.duration:
            return '⏳ Невідомо'
        h, rem = divmod(int(self.duration), 3600)
        m, s   = divmod(rem, 60)
        return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'

    @property
    def elapsed_str(self) -> str:
        elapsed = int(time.time() - self.start_time) if self.start_time else 0
        h, rem = divmod(elapsed, 3600)
        m, s   = divmod(rem, 60)
        return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'

    def progress_bar(self, width: int = 12) -> str:
        if not self.duration or not self.start_time:
            return '▬' * width
        elapsed = min(time.time() - self.start_time, self.duration)
        filled  = int((elapsed / self.duration) * width)
        bar     = '▬' * filled + '🔘' + '▬' * (width - filled - 1)
        return bar

    def make_embed(self, status: str = '▶️ Зараз грає', filter_name: str = AudioFilter.NONE) -> discord.Embed:
        filter_label, _ = FILTER_LABELS.get(filter_name, FILTER_LABELS[AudioFilter.NONE])
        embed = discord.Embed(
            title=f'{status}  •  {filter_label}',
            description=f'## [{self.title}]({self.webpage_url})',
            color=COLOR_BLUE,
        )
        if self.thumbnail:
            embed.set_thumbnail(url=self.thumbnail)
        embed.add_field(name='⏱️ Тривалість', value=self.duration_str,      inline=True)
        embed.add_field(name='🎤 Виконавець', value=self.uploader,           inline=True)
        embed.add_field(name='👤 Додав',      value=self.requester.mention,  inline=True)
        if self.duration and self.start_time:
            bar = self.progress_bar()
            embed.add_field(
                name='​',
                value=f'{self.elapsed_str} {bar} {self.duration_str}',
                inline=False,
            )
        embed.set_footer(text='🎵 Music Bot v3')
        return embed


# ─── Функції отримання інформації ─────────────────────────────────────────────
async def fetch_track(query: str, requester: discord.Member, loop=None) -> Optional[Track]:
    if not query.startswith(('http://', 'https://')):
        query = f'ytsearch1:{query}'
    else:
        query = clean_url(query)
    data = await _ytdl_extract(query)
    if not data:
        return None
    if 'entries' in data:
        entries = [e for e in data['entries'] if e]
        if not entries:
            return None
        data = entries[0]
        full = await _ytdl_extract(data.get('webpage_url', data.get('url', '')))
        if full:
            data = full
    return Track(data, requester) if data else None


def is_real_playlist(url: str) -> bool:
    if not url.startswith('http'):
        return False
    match = re.search(r'[?&]list=([A-Za-z0-9_-]+)', url)
    if not match:
        return False
    list_id = match.group(1)
    if list_id in ('LL', 'WL') or list_id.startswith('RD'):
        return False
    return list_id.startswith(('PL', 'FL', 'OL', 'UU'))


def clean_url(url: str) -> str:
    if not url.startswith('http') or is_real_playlist(url):
        return url
    url = re.sub(r'[?&]list=[^&]+', '', url)
    url = re.sub(r'[?&]si=[^&]+', '', url)
    url = re.sub(r'[?&]index=[^&]+', '', url)
    url = re.sub(r'\?&', '?', url)
    url = url.rstrip('?&')
    return url


async def fetch_playlist(url: str, requester: discord.Member, loop=None) -> List[Track]:
    loop = loop or asyncio.get_event_loop()
    opts = {**YTDL_OPTIONS, 'extract_flat': True, 'noplaylist': False}
    try:
        data = await loop.run_in_executor(
            None, lambda: yt_dlp.YoutubeDL(opts).extract_info(url, download=False)
        )
    except BaseException as e:
        print(f'[yt-dlp] fetch_playlist помилка: {e}')
        return []
    entries = data.get('entries', []) if data else []
    tracks  = []
    for entry in entries:
        if not entry:
            continue
        video_id = entry.get('id', '')
        web_url  = entry.get('webpage_url') or (
            f'https://www.youtube.com/watch?v={video_id}' if video_id else ''
        )
        if not web_url:
            continue
        entry.setdefault('webpage_url', web_url)
        entry.setdefault('url', web_url)
        t = Track(entry, requester)
        t.webpage_url = web_url
        t.url         = web_url
        tracks.append(t)
    return tracks


async def fetch_autoplay_tracks(video_id: str, requester: discord.Member, count: int = 3) -> List[Track]:
    if not video_id:
        return []
    mix_url = f'https://www.youtube.com/watch?v={video_id}&list=RD{video_id}'
    opts = {**YTDL_OPTIONS, 'extract_flat': True, 'noplaylist': False, 'playlistend': count + 2}
    loop = asyncio.get_event_loop()
    try:
        async with _ytdl_semaphore:
            data = await loop.run_in_executor(
                None, lambda: yt_dlp.YoutubeDL(opts).extract_info(mix_url, download=False)
            )
    except BaseException as e:
        print(f'[autoplay] помилка: {e}')
        return []
    entries = (data.get('entries') or []) if data else []
    tracks  = []
    for entry in entries:
        if not entry:
            continue
        vid = entry.get('id', '')
        if vid == video_id:
            continue
        web_url = entry.get('webpage_url') or (
            f'https://www.youtube.com/watch?v={vid}' if vid else ''
        )
        if not web_url:
            continue
        entry.setdefault('webpage_url', web_url)
        entry.setdefault('url', web_url)
        t = Track(entry, requester)
        t.webpage_url = web_url
        t.url         = web_url
        tracks.append(t)
        if len(tracks) >= count:
            break
    return tracks


async def fetch_mix_tracks(query: str, requester: discord.Member, count: int = 10) -> List[Track]:
    count = max(1, min(count, 50))
    loop  = asyncio.get_event_loop()

    video_id = None

    rd_match = re.search(r'[?&]list=RD([A-Za-z0-9_-]+)', query)
    if rd_match:
        video_id = rd_match.group(1)
        v_match = re.search(r'[?&v=/]v=([A-Za-z0-9_-]{11})', query)
        if v_match:
            video_id = v_match.group(1)

    if not video_id:
        v_match = re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', query)
        if v_match:
            video_id = v_match.group(1)

    if not video_id:
        data = await _ytdl_extract(f'ytsearch1:{query}')
        if data and 'entries' in data:
            entries = [e for e in data.get('entries', []) if e]
            if entries:
                video_id = entries[0].get('id', '')
        elif data:
            video_id = data.get('id', '')

    if not video_id:
        return []

    mix_url = f'https://www.youtube.com/watch?v={video_id}&list=RD{video_id}'
    opts    = {
        **YTDL_OPTIONS,
        'extract_flat': True,
        'noplaylist':   False,
        'playlistend':  count + 3,
    }
    try:
        async with _ytdl_semaphore:
            data = await loop.run_in_executor(
                None, lambda: yt_dlp.YoutubeDL(opts).extract_info(mix_url, download=False)
            )
    except BaseException as e:
        print(f'[mix] помилка: {e}')
        return []

    entries = (data.get('entries') or []) if data else []
    tracks  = []
    for entry in entries:
        if not entry:
            continue
        vid = entry.get('id', '')
        web_url = entry.get('webpage_url') or (
            f'https://www.youtube.com/watch?v={vid}' if vid else ''
        )
        if not web_url:
            continue
        entry.setdefault('webpage_url', web_url)
        entry.setdefault('url', web_url)
        t = Track(entry, requester)
        t.webpage_url = web_url
        t.url         = web_url
        tracks.append(t)
        if len(tracks) >= count:
            break
    return tracks


async def get_audio_source(track: Track, volume: float = 1.0,
                           audio_filter: str = AudioFilter.NONE) -> discord.PCMVolumeTransformer:
    stream_url = await _get_stream_url(track.webpage_url or track.url)
    if not stream_url:
        raise RuntimeError(f'Не вдалося отримати stream URL для: {track.title}')
    track.url = stream_url
    ffmpeg_opts = build_ffmpeg_options(audio_filter)
    source = discord.FFmpegPCMAudio(stream_url, **ffmpeg_opts)
    return discord.PCMVolumeTransformer(source, volume=volume)


# ─── Режим повторення ─────────────────────────────────────────────────────────
class LoopMode:
    OFF   = 0
    TRACK = 1
    QUEUE = 2

LOOP_LABELS = {
    LoopMode.OFF:   ('🔁 Повтор вимкнено', COLOR_RED),
    LoopMode.TRACK: ('🔂 Повтор треку',    COLOR_GREEN),
    LoopMode.QUEUE: ('🔁 Повтор черги',    COLOR_GREEN),
}


# ─── Музичний плеєр ───────────────────────────────────────────────────────────
class MusicPlayer:
    def __init__(self, guild: discord.Guild, text_channel: discord.TextChannel, bot: commands.Bot):
        self.guild        = guild
        self.text_channel = text_channel
        self.bot          = bot

        self.queue:    List[Track] = []
        self.history:  List[Track] = []
        self.current:  Optional[Track] = None
        self.loop_mode: int   = LoopMode.OFF
        self.volume:    float = 0.5
        self.autoplay:  bool  = False
        self.audio_filter: str = AudioFilter.NONE
        self.now_playing_msg: Optional[discord.Message] = None

        self._next       = asyncio.Event()
        self._task       = bot.loop.create_task(self._player_loop())
        self._idle_since: Optional[float] = None

    async def _player_loop(self):
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self._next.clear()

            if self.loop_mode == LoopMode.TRACK and self.current:
                track = self.current
            elif self.queue:
                track = self.queue.pop(0)
                if self.loop_mode == LoopMode.QUEUE:
                    self.queue.append(track)
            else:
                last = self.current
                self.current = None
                self._idle_since = time.time()

                if self.autoplay and last and last.video_id:
                    await self.text_channel.send(
                        embed=discord.Embed(
                            description='🔄 Autoplay: шукаю схожі треки...',
                            color=COLOR_CYAN,
                        )
                    )
                    ap_tracks = await fetch_autoplay_tracks(last.video_id, last.requester, count=3)
                    if ap_tracks:
                        self.queue.extend(ap_tracks)
                        self._next.set()
                        continue

                try:
                    await asyncio.wait_for(self._next.wait(), timeout=INACTIVITY_TIMEOUT)
                    continue
                except asyncio.TimeoutError:
                    await self._auto_disconnect()
                    return

            self.current     = track
            self._idle_since = None

            vc = self.guild.voice_client
            if not vc or not vc.is_connected():
                return

            try:
                source = await get_audio_source(track, volume=self.volume,
                                                audio_filter=self.audio_filter)
                track.start_time = time.time()
            except BaseException as e:
                await self.text_channel.send(
                    embed=discord.Embed(
                        description=f'❌ Помилка завантаження треку: `{e}`\nПропускаю...',
                        color=COLOR_RED,
                    )
                )
                self._next.set()
                continue

            def after_playing(error):
                if error:
                    print(f'[player] Помилка відтворення: {error}')
                self.bot.loop.call_soon_threadsafe(self._next.set)

            vc.play(source, after=after_playing)

            self.history.append(track)
            if len(self.history) > 50:
                self.history.pop(0)

            await self._send_now_playing(track, vc)
            await self._next.wait()

            if vc and vc.is_playing():
                vc.stop()

    async def _send_now_playing(self, track: Track, vc):
        if self.now_playing_msg:
            try:
                await self.now_playing_msg.delete()
            except Exception:
                pass
        embed = track.make_embed(filter_name=self.audio_filter)
        view  = NowPlayingView(self)
        try:
            self.now_playing_msg = await self.text_channel.send(embed=embed, view=view)
        except Exception as e:
            print(f'[player] Не вдалося надіслати now-playing: {e}')

    async def _auto_disconnect(self):
        vc = self.guild.voice_client
        if vc:
            try:
                await self.text_channel.send(
                    embed=discord.Embed(
                        description='😴 Відключився через тривалий простій.',
                        color=COLOR_YELLOW,
                    )
                )
                await vc.disconnect()
            except Exception:
                pass
        await self.destroy()

    def add(self, track: Track):
        self.queue.append(track)
        if not self.guild.voice_client or not self.guild.voice_client.is_playing():
            self._next.set()

    def add_many(self, tracks: List[Track]):
        self.queue.extend(tracks)
        if not self.guild.voice_client or not self.guild.voice_client.is_playing():
            self._next.set()

    def skip(self):
        vc = self.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    def shuffle(self):
        random.shuffle(self.queue)

    def clear(self):
        self.queue.clear()

    def remove(self, index: int) -> Optional[Track]:
        if 0 <= index < len(self.queue):
            return self.queue.pop(index)
        return None

    def set_volume(self, vol: float):
        self.volume = vol
        vc = self.guild.voice_client
        if vc and vc.source:
            vc.source.volume = vol

    def cycle_loop(self):
        self.loop_mode = (self.loop_mode + 1) % 3

    def set_filter(self, audio_filter: str):
        self.audio_filter = audio_filter
        if self.current:
            self.queue.insert(0, self.current)
            self.skip()

    async def destroy(self):
        self._task.cancel()
        if self.now_playing_msg:
            try:
                await self.now_playing_msg.delete()
            except Exception:
                pass


# ─── UI: Кнопки Now Playing ───────────────────────────────────────────────────
class NowPlayingView(ui.View):
    def __init__(self, player: MusicPlayer):
        super().__init__(timeout=None)
        self.player = player

    async def _check_vc(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild.voice_client:
            await interaction.response.send_message('❌ Бот не у голосовому каналі!', ephemeral=True)
            return False
        return True

    @ui.button(emoji='⏮️', style=ButtonStyle.secondary, row=0)
    async def prev(self, interaction: discord.Interaction, button: ui.Button):
        if not await self._check_vc(interaction): return
        h = self.player.history
        if len(h) < 2:
            await interaction.response.send_message('Немає попереднього треку!', ephemeral=True)
            return
        prev_track = h[-2]
        self.player.queue.insert(0, prev_track)
        self.player.queue.insert(0, h[-1] if h else prev_track)
        self.player.skip()
        await interaction.response.send_message(f'⏮️ Повернувся до: **{prev_track.title}**', ephemeral=True)

    @ui.button(emoji='⏸️', style=ButtonStyle.primary, row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: ui.Button):
        if not await self._check_vc(interaction): return
        vc = interaction.guild.voice_client
        if vc.is_paused():
            vc.resume()
            await interaction.response.send_message('▶️ Відтворення відновлено', ephemeral=True)
        elif vc.is_playing():
            vc.pause()
            await interaction.response.send_message('⏸️ Відтворення на паузі', ephemeral=True)
        else:
            await interaction.response.send_message('Нічого не грає!', ephemeral=True)

    @ui.button(emoji='⏭️', style=ButtonStyle.primary, row=0)
    async def skip(self, interaction: discord.Interaction, button: ui.Button):
        if not await self._check_vc(interaction): return
        self.player.skip()
        await interaction.response.send_message('⏭️ Трек пропущено', ephemeral=True)

    @ui.button(emoji='🔀', style=ButtonStyle.secondary, row=0)
    async def shuffle(self, interaction: discord.Interaction, button: ui.Button):
        self.player.shuffle()
        await interaction.response.send_message(
            f'🔀 Чергу перемішано ({len(self.player.queue)} треків)', ephemeral=True)

    @ui.button(emoji='🔁', style=ButtonStyle.secondary, row=0)
    async def loop(self, interaction: discord.Interaction, button: ui.Button):
        self.player.cycle_loop()
        label, _ = LOOP_LABELS[self.player.loop_mode]
        await interaction.response.send_message(label, ephemeral=True)

    @ui.button(emoji='🔉', style=ButtonStyle.secondary, row=1)
    async def vol_down(self, interaction: discord.Interaction, button: ui.Button):
        if not await self._check_vc(interaction): return
        vol = max(0.0, self.player.volume - 0.1)
        self.player.set_volume(vol)
        await interaction.response.send_message(f'🔉 Гучність: **{int(vol * 100)}%**', ephemeral=True)

    @ui.button(emoji='🔊', style=ButtonStyle.secondary, row=1)
    async def vol_up(self, interaction: discord.Interaction, button: ui.Button):
        if not await self._check_vc(interaction): return
        vol = min(2.0, self.player.volume + 0.1)
        self.player.set_volume(vol)
        await interaction.response.send_message(f'🔊 Гучність: **{int(vol * 100)}%**', ephemeral=True)

    @ui.button(label='📋 Черга', style=ButtonStyle.success, row=1)
    async def show_queue(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.guild.id not in players:
            await interaction.response.send_message('❌ Плеєр не запущено!', ephemeral=True)
            return
        embed = make_queue_embed(self.player, page=0)
        view  = QueueView(self.player, page=0)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @ui.button(label='⏹️ Стоп', style=ButtonStyle.danger, row=1)
    async def stop(self, interaction: discord.Interaction, button: ui.Button):
        if not await self._check_vc(interaction): return
        gid = interaction.guild.id
        if gid in players:
            players[gid].clear()
            players[gid].skip()
            await players[gid].destroy()
            del players[gid]
        vc = interaction.guild.voice_client
        if vc:
            await vc.disconnect()
        await interaction.response.send_message('⏹️ Відтворення зупинено, відключився.', ephemeral=True)


# ─── Пагінація черги ──────────────────────────────────────────────────────────
class QueueView(ui.View):
    def __init__(self, player: MusicPlayer, page: int = 0):
        super().__init__(timeout=120)
        self.player = player
        self.page   = page
        self._update_buttons()

    def _update_buttons(self):
        per_page = 10
        total    = len(self.player.queue)
        pages    = max(1, (total + per_page - 1) // per_page)
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= pages - 1
        self.page_label.label  = f'Стор. {self.page + 1}/{pages}'

    @ui.button(emoji='◀️', style=ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: ui.Button):
        self.page = max(0, self.page - 1)
        self._update_buttons()
        embed = make_queue_embed(self.player, page=self.page)
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label='Стор. 1/1', style=ButtonStyle.secondary, disabled=True)
    async def page_label(self, interaction: discord.Interaction, button: ui.Button):
        pass

    @ui.button(emoji='▶️', style=ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: ui.Button):
        per_page = 10
        total    = len(self.player.queue)
        pages    = max(1, (total + per_page - 1) // per_page)
        self.page = min(pages - 1, self.page + 1)
        self._update_buttons()
        embed = make_queue_embed(self.player, page=self.page)
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(emoji='🔄', style=ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: ui.Button):
        self._update_buttons()
        embed = make_queue_embed(self.player, page=self.page)
        await interaction.response.edit_message(embed=embed, view=self)


# ─── Пошук з мультиселектом ───────────────────────────────────────────────────
class SearchSelect(ui.Select):
    def __init__(self, results: List[dict], requester: discord.Member):
        self.results   = results
        self.requester = requester

        options = []
        for i, r in enumerate(results):
            dur = r.get('duration', 0)
            m, s = divmod(int(dur), 60) if dur else (0, 0)
            dur_str = f'{m}:{s:02d}' if dur else '?'
            options.append(discord.SelectOption(
                label=r.get('title', f'Трек {i+1}')[:100],
                description=f'{r.get("uploader", "")} • {dur_str}'[:100],
                value=str(i),
            ))
        super().__init__(
            placeholder='Оберіть один або декілька треків...',
            options=options,
            min_values=1,
            max_values=min(5, len(options)),
        )

    async def callback(self, interaction: discord.Interaction):
        selected_indices = [int(v) for v in self.values]
        await interaction.response.defer()

        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send(
                embed=discord.Embed(
                    description='❌ Підключіться до голосового каналу перед вибором треку!',
                    color=COLOR_RED,
                ),
                ephemeral=True,
            )
            return

        vc = interaction.guild.voice_client
        if vc:
            if vc.channel != interaction.user.voice.channel:
                await vc.move_to(interaction.user.voice.channel)
        else:
            await interaction.user.voice.channel.connect()

        player = await ensure_player(interaction.guild, interaction.channel, interaction.client)

        await interaction.followup.send(
            embed=discord.Embed(
                description=f'⏳ Завантажую {len(selected_indices)} трек(и)...',
                color=COLOR_YELLOW,
            ),
            ephemeral=True,
        )

        added  = []
        failed = []

        for idx in selected_indices:
            result    = self.results[idx]
            video_url = result.get('webpage_url') or result.get('url') or ''
            if result.get('id') and not video_url.startswith('http'):
                video_url = f'https://www.youtube.com/watch?v={result["id"]}'
            try:
                data = await _ytdl_extract(video_url)
                if data:
                    track = Track(data, self.requester)
                    player.add(track)
                    added.append(track)
                else:
                    failed.append(result.get('title', '?'))
            except BaseException:
                failed.append(result.get('title', '?'))

        if added:
            lines = '\n'.join(f'✅ **{t.title}** • {t.duration_str}' for t in added)
            embed = discord.Embed(description=lines, color=COLOR_GREEN)
            embed.set_author(name=f'Додано до черги: {len(added)} трек(и)')
            if added[0].thumbnail:
                embed.set_thumbnail(url=added[0].thumbnail)
            if failed:
                embed.add_field(name='❌ Не вдалося додати', value='\n'.join(failed), inline=False)
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(
                embed=discord.Embed(description='❌ Жодного треку не вдалося додати.', color=COLOR_RED)
            )
        self.view.stop()


class SearchView(ui.View):
    def __init__(self, results, requester):
        super().__init__(timeout=60)
        self.add_item(SearchSelect(results, requester))

    @ui.button(label='Скасувати', style=ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message('🔍 Пошук скасовано.', ephemeral=True)
        self.stop()


# ─── Допоміжні функції ────────────────────────────────────────────────────────
def make_queue_embed(player: MusicPlayer, page: int = 0) -> discord.Embed:
    per_page = 10
    queue    = player.queue
    total    = len(queue)
    pages    = max(1, (total + per_page - 1) // per_page)
    page     = min(page, pages - 1)
    start    = page * per_page

    loop_label, _ = LOOP_LABELS[player.loop_mode]
    filter_label, _ = FILTER_LABELS.get(player.audio_filter, FILTER_LABELS[AudioFilter.NONE])
    ap_label     = '🔄 Autoplay ON' if player.autoplay else ''

    embed = discord.Embed(title=f'📋 Черга — сторінка {page + 1}/{pages}', color=COLOR_PURPLE)

    if player.current:
        t = player.current
        now_value = (
            f'[{t.title[:80]}]({t.webpage_url}) • {t.duration_str}\n'
            f'{t.progress_bar()} • {t.requester.mention}'
        )
        embed.add_field(name='▶️ Зараз грає', value=now_value[:1024], inline=False)

    if total == 0:
        embed.add_field(name='Черга порожня', value='Додайте треки командою `/play`', inline=False)
    else:
        lines  = []
        budget = 1000
        for i, t in enumerate(queue[start:start + per_page], start=start + 1):
            line = f'`{i}.` [{t.title[:45]}]({t.webpage_url}) • {t.duration_str}\n'
            if len(''.join(lines)) + len(line) > budget:
                lines.append('`...` список обрізано (занадто довгі назви)')
                break
            lines.append(line)
        embed.add_field(name='Наступні треки', value=''.join(lines)[:1024], inline=False)

    footer_parts = [f'{total} треків', loop_label, filter_label, f'Гучність: {int(player.volume * 100)}%']
    if ap_label:
        footer_parts.append(ap_label)
    embed.set_footer(text=' • '.join(p for p in footer_parts if p))
    return embed


async def ensure_player(guild: discord.Guild, text_channel, bot: commands.Bot) -> MusicPlayer:
    if guild.id not in players:
        players[guild.id] = MusicPlayer(guild, text_channel, bot)
    return players[guild.id]


async def ensure_voice(ctx_or_interaction) -> bool:
    if isinstance(ctx_or_interaction, discord.Interaction):
        guild  = ctx_or_interaction.guild
        author = ctx_or_interaction.user
        send   = ctx_or_interaction.followup.send
    else:
        guild  = ctx_or_interaction.guild
        author = ctx_or_interaction.author
        send   = ctx_or_interaction.send

    if not author.voice or not author.voice.channel:
        await send(embed=discord.Embed(description='❌ Спочатку підключіться до голосового каналу!', color=COLOR_RED))
        return False

    vc = guild.voice_client
    if vc:
        if vc.channel != author.voice.channel:
            await vc.move_to(author.voice.channel)
    else:
        await author.voice.channel.connect()
    return True


# ─── Глобальний словник плеєрів ───────────────────────────────────────────────
players: dict[int, MusicPlayer] = {}


# ─── Ініціалізація бота ───────────────────────────────────────────────────────
intents                 = discord.Intents.default()
intents.message_content = True
intents.voice_states    = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


# ─── SLASH-КОМАНДИ ────────────────────────────────────────────────────────────

@bot.tree.command(name='play', description='▶️ Відтворити музику (посилання або пошук)')
@app_commands.describe(query='YouTube URL, плейлист або пошуковий запрос')
async def slash_play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    if not await ensure_voice(interaction):
        return
    player = await ensure_player(interaction.guild, interaction.channel, bot)
    is_playlist = is_real_playlist(query)
    if not is_playlist:
        query = clean_url(query)

    if is_playlist:
        await interaction.followup.send(embed=discord.Embed(description='⏳ Завантажую плейлист...', color=COLOR_YELLOW))
        tracks = await fetch_playlist(query, interaction.user)
        if not tracks:
            await interaction.followup.send(embed=discord.Embed(description='❌ Не вдалося завантажити плейлист.', color=COLOR_RED))
            return
        player.add_many(tracks)
        await interaction.followup.send(embed=discord.Embed(description=f'✅ Додано **{len(tracks)}** треків з плейлиста до черги!', color=COLOR_GREEN))
    else:
        await interaction.followup.send(embed=discord.Embed(description=f'⏳ Шукаю: `{query}`...', color=COLOR_YELLOW))
        track = await fetch_track(query, interaction.user, loop=bot.loop)
        if not track:
            await interaction.followup.send(embed=discord.Embed(description='❌ Нічого не знайдено.', color=COLOR_RED))
            return
        player.add(track)
        embed = discord.Embed(
            description=f'✅ Додано до черги: **[{track.title}]({track.webpage_url})**\n⏱️ {track.duration_str}',
            color=COLOR_GREEN,
        )
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        await interaction.followup.send(embed=embed)


@bot.tree.command(name='search', description='🔍 Знайти треки та обрати зі списку (можна кілька)')
@app_commands.describe(query='Пошуковий запит')
async def slash_search(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    await interaction.followup.send(embed=discord.Embed(description=f'🔍 Шукаю: `{query}`...', color=COLOR_YELLOW))
    try:
        data = await bot.loop.run_in_executor(None, lambda: ytdl_search.extract_info(f'ytsearch5:{query}', download=False))
    except BaseException as e:
        await interaction.followup.send(embed=discord.Embed(description=f'❌ Помилка пошуку: `{e}`', color=COLOR_RED))
        return

    entries = [e for e in (data.get('entries') or []) if e][:5]
    if not entries:
        await interaction.followup.send(embed=discord.Embed(description='❌ Нічого не знайдено.', color=COLOR_RED))
        return

    embed = discord.Embed(title=f'🔍 Результати для: {query}', color=COLOR_PURPLE)
    for i, e in enumerate(entries, 1):
        dur = e.get('duration', 0)
        m, s = divmod(int(dur), 60) if dur else (0, 0)
        dur_str  = f'{m}:{s:02d}' if dur else '?'
        title    = e.get('title', 'Невідомо')[:70]
        uploader = e.get('uploader', '') or e.get('channel', '')
        embed.add_field(name=f'{i}. {title}', value=f'`{uploader}` • ⏱️ {dur_str}', inline=False)
    embed.set_footer(text='Можна вибрати декілька треків одночасно • Спливає через 60 сек')

    view = SearchView(entries, interaction.user)
    await interaction.followup.send(embed=embed, view=view)


@bot.tree.command(name='skip', description='⏭️ Пропустити поточний трек')
async def slash_skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        await interaction.response.send_message(embed=discord.Embed(description='❌ Зараз нічого не грає!', color=COLOR_RED), ephemeral=True)
        return
    gid = interaction.guild.id
    if gid in players:
        players[gid].skip()
    await interaction.response.send_message(embed=discord.Embed(description='⏭️ Трек пропущено!', color=COLOR_GREEN), ephemeral=True)


@bot.tree.command(name='pause', description='⏸️ Поставити на паузу / Зняти з паузи')
async def slash_pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        await interaction.response.send_message('❌ Бот не у голосовому каналі!', ephemeral=True)
        return
    if vc.is_paused():
        vc.resume()
        await interaction.response.send_message(embed=discord.Embed(description='▶️ Відтворення відновлено!', color=COLOR_GREEN), ephemeral=True)
    elif vc.is_playing():
        vc.pause()
        await interaction.response.send_message(embed=discord.Embed(description='⏸️ Відтворення на паузі!', color=COLOR_YELLOW), ephemeral=True)
    else:
        await interaction.response.send_message('❌ Нічого не грає!', ephemeral=True)


@bot.tree.command(name='stop', description='⏹️ Зупинити відтворення та відключитися')
async def slash_stop(interaction: discord.Interaction):
    gid = interaction.guild.id
    if gid in players:
        players[gid].clear()
        players[gid].skip()
        await players[gid].destroy()
        del players[gid]
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
    await interaction.response.send_message(embed=discord.Embed(description='⏹️ Зупинено. До побачення!', color=COLOR_RED))


@bot.tree.command(name='queue', description='📋 Показати чергу відтворення з кнопками навігації')
async def slash_queue(interaction: discord.Interaction):
    gid = interaction.guild.id
    if gid not in players:
        await interaction.response.send_message(embed=discord.Embed(description='❌ Плеєр не запущено!', color=COLOR_RED), ephemeral=True)
        return
    embed = make_queue_embed(players[gid], page=0)
    view  = QueueView(players[gid], page=0)
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name='nowplaying', description='🎵 Показати поточний трек')
async def slash_nowplaying(interaction: discord.Interaction):
    gid = interaction.guild.id
    if gid not in players or not players[gid].current:
        await interaction.response.send_message(embed=discord.Embed(description='❌ Зараз нічого не грає!', color=COLOR_RED), ephemeral=True)
        return
    track = players[gid].current
    embed = track.make_embed(filter_name=players[gid].audio_filter)
    view  = NowPlayingView(players[gid])
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name='volume', description='🔊 Встановити гучність (0–200%)')
@app_commands.describe(percent='Гучність від 0 до 200')
async def slash_volume(interaction: discord.Interaction, percent: int):
    if not (0 <= percent <= 200):
        await interaction.response.send_message('❌ Гучність має бути від 0 до 200!', ephemeral=True)
        return
    gid = interaction.guild.id
    if gid not in players:
        await interaction.response.send_message('❌ Плеєр не запущено!', ephemeral=True)
        return
    players[gid].set_volume(percent / 100)
    emoji = '🔇' if percent == 0 else '🔉' if percent < 50 else '🔊'
    await interaction.response.send_message(embed=discord.Embed(description=f'{emoji} Гучність: **{percent}%**', color=COLOR_GREEN))


@bot.tree.command(name='shuffle', description='🔀 Перемішати чергу')
async def slash_shuffle(interaction: discord.Interaction):
    gid = interaction.guild.id
    if gid not in players:
        await interaction.response.send_message('❌ Плеєр не запущено!', ephemeral=True)
        return
    players[gid].shuffle()
    await interaction.response.send_message(embed=discord.Embed(description=f'🔀 Чергу перемішано! ({len(players[gid].queue)} треків)', color=COLOR_GREEN))


@bot.tree.command(name='loop', description='🔁 Переключити режим повторення')
async def slash_loop(interaction: discord.Interaction):
    gid = interaction.guild.id
    if gid not in players:
        await interaction.response.send_message('❌ Плеєр не запущено!', ephemeral=True)
        return
    players[gid].cycle_loop()
    label, color = LOOP_LABELS[players[gid].loop_mode]
    await interaction.response.send_message(embed=discord.Embed(description=label, color=color))


@bot.tree.command(name='remove', description='🗑️ Видалити трек з черги за номером')
@app_commands.describe(position='Номер треку в черзі')
async def slash_remove(interaction: discord.Interaction, position: int):
    gid = interaction.guild.id
    if gid not in players:
        await interaction.response.send_message('❌ Плеєр не запущено!', ephemeral=True)
        return
    track = players[gid].remove(position - 1)
    if track:
        await interaction.response.send_message(embed=discord.Embed(description=f'🗑️ Видалено: **{track.title}**', color=COLOR_YELLOW))
    else:
        await interaction.response.send_message('❌ Невірний номер треку!', ephemeral=True)


@bot.tree.command(name='clear', description='🧹 Очистити чергу')
async def slash_clear(interaction: discord.Interaction):
    gid = interaction.guild.id
    if gid not in players:
        await interaction.response.send_message('❌ Плеєр не запущено!', ephemeral=True)
        return
    count = len(players[gid].queue)
    players[gid].clear()
    await interaction.response.send_message(embed=discord.Embed(description=f'🧹 Черга очищена! Видалено **{count}** треків.', color=COLOR_YELLOW))


@bot.tree.command(name='history', description='📜 Показати останні 10 зіграних треків')
async def slash_history(interaction: discord.Interaction):
    gid = interaction.guild.id
    if gid not in players or not players[gid].history:
        await interaction.response.send_message(embed=discord.Embed(description='📜 Історія порожня.', color=COLOR_YELLOW), ephemeral=True)
        return
    history = players[gid].history[-10:][::-1]
    embed   = discord.Embed(title='📜 Нещодавно грали', color=COLOR_BLUE)
    lines   = []
    for i, t in enumerate(history, 1):
        lines.append(f'`{i}.` [{t.title[:55]}]({t.webpage_url}) • {t.duration_str} • {t.requester.mention}')
    embed.description = '\n'.join(lines)
    embed.set_footer(text=f'Усього в історії: {len(players[gid].history)} треків')
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name='autoplay', description='🔄 Увімкнути/вимкнути автовідтворення схожих треків')
async def slash_autoplay(interaction: discord.Interaction):
    gid = interaction.guild.id
    if gid not in players:
        await interaction.response.send_message('❌ Плеєр не запущено!', ephemeral=True)
        return
    players[gid].autoplay = not players[gid].autoplay
    state = '🟢 Увімкнено' if players[gid].autoplay else '🔴 Вимкнено'
    desc  = (
        f'🔄 Autoplay: **{state}**\n'
        + ('Після закінчення черги бот автоматично додасть схожі треки з YouTube Mix.'
           if players[gid].autoplay else 'Бот зупиниться після закінчення черги.')
    )
    await interaction.response.send_message(embed=discord.Embed(description=desc, color=COLOR_GREEN if players[gid].autoplay else COLOR_RED))


@bot.tree.command(name='mix', description='🎲 Додати треки з YouTube Mix (авто-генерований плейлист)')
@app_commands.describe(query='Відео URL, Mix URL або назва пісні', count='Кількість треків (1–50, за замовчуванням 10)')
async def slash_mix(interaction: discord.Interaction, query: str, count: int = 10):
    if not (1 <= count <= 50):
        await interaction.response.send_message(embed=discord.Embed(description='❌ Кількість має бути від 1 до 50!', color=COLOR_RED), ephemeral=True)
        return

    await interaction.response.defer()
    if not await ensure_voice(interaction):
        return

    player = await ensure_player(interaction.guild, interaction.channel, bot)
    await interaction.followup.send(embed=discord.Embed(description=f'🎲 Завантажую YouTube Mix ({count} треків)...', color=COLOR_YELLOW))

    tracks = await fetch_mix_tracks(query, interaction.user, count=count)

    if not tracks:
        await interaction.followup.send(embed=discord.Embed(description='❌ Не вдалося завантажити Mix. Спробуй інше відео або запит.', color=COLOR_RED))
        return

    player.add_many(tracks)

    preview = '\n'.join(f'`{i}.` {t.title[:55]}' for i, t in enumerate(tracks[:5], 1))
    if len(tracks) > 5:
        preview += f'\n`...` і ще {len(tracks) - 5} треків'

    embed = discord.Embed(title=f'🎲 YouTube Mix — додано {len(tracks)} треків', description=preview, color=COLOR_CYAN)
    if tracks[0].thumbnail:
        embed.set_thumbnail(url=tracks[0].thumbnail)
    embed.set_footer(text=f'На основі: {tracks[0].title[:60]}' if tracks else '')
    await interaction.followup.send(embed=embed)


@bot.tree.command(name='bassboost', description='🔊 Увімкнути/вимкнути Bass Boost')
async def slash_bassboost(interaction: discord.Interaction):
    gid = interaction.guild.id
    if gid not in players:
        await interaction.response.send_message('❌ Плеєр не запущено!', ephemeral=True)
        return
    if players[gid].audio_filter == AudioFilter.BASSBOOST:
        players[gid].set_filter(AudioFilter.NONE)
        await interaction.response.send_message(embed=discord.Embed(description='🎵 Bass Boost вимкнено.', color=COLOR_BLUE))
    else:
        players[gid].set_filter(AudioFilter.BASSBOOST)
        await interaction.response.send_message(embed=discord.Embed(description='🔊 **Bass Boost увімкнено!** Трек перезапускається...', color=COLOR_PURPLE))


@bot.tree.command(name='nightcore', description='⚡ Увімкнути/вимкнути Nightcore (прискорення + підвищення тону)')
async def slash_nightcore(interaction: discord.Interaction):
    gid = interaction.guild.id
    if gid not in players:
        await interaction.response.send_message('❌ Плеєр не запущено!', ephemeral=True)
        return
    if players[gid].audio_filter == AudioFilter.NIGHTCORE:
        players[gid].set_filter(AudioFilter.NONE)
        await interaction.response.send_message(embed=discord.Embed(description='🎵 Nightcore вимкнено.', color=COLOR_BLUE))
    else:
        players[gid].set_filter(AudioFilter.NIGHTCORE)
        await interaction.response.send_message(embed=discord.Embed(description='⚡ **Nightcore увімкнено!** Трек перезапускається...', color=COLOR_CYAN))


@bot.tree.command(name='join', description='🔗 Приєднатися до голосового каналу')
async def slash_join(interaction: discord.Interaction):
    await interaction.response.defer()
    if not await ensure_voice(interaction):
        return
    ch = interaction.guild.voice_client.channel
    await interaction.followup.send(embed=discord.Embed(description=f'✅ Підключився до **{ch.name}**', color=COLOR_GREEN))


@bot.tree.command(name='leave', description='👋 Відключитися від голосового каналу')
async def slash_leave(interaction: discord.Interaction):
    gid = interaction.guild.id
    if gid in players:
        await players[gid].destroy()
        del players[gid]
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        await interaction.response.send_message(embed=discord.Embed(description='👋 Відключився!', color=COLOR_YELLOW))
    else:
        await interaction.response.send_message('❌ Бот не у голосовому каналі!', ephemeral=True)


@bot.tree.command(name='help', description='❓ Список команд')
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(title='📖 Команди Music Bot v3', color=COLOR_BLUE)
    embed.add_field(
        name='🎵 Відтворення',
        value='`/play [URL або пошук]` • `/search [запит]` • `/nowplaying` • `/skip` • `/pause` • `/stop`',
        inline=False,
    )
    embed.add_field(
        name='📋 Черга',
        value='`/queue` • `/shuffle` • `/remove [номер]` • `/clear` • `/loop`',
        inline=False,
    )
    embed.add_field(
        name='✨ Функції',
        value='`/history` • `/autoplay` • `/mix [к-сть] [запит]` • `/bassboost` • `/nightcore`',
        inline=False,
    )
    embed.add_field(name='⚙️ Інше', value='`/volume [0-200]` • `/join` • `/leave`\nПрефікс `!` теж працює', inline=False)
    cookies_status = '✅ COOKIES_B64' if _COOKIES_TMP else ('✅ файл' if COOKIE_FILE else '❌ без cookies')
    embed.set_footer(text=f'⏱️ Автовідключення через 5 хв • Cookies: {cookies_status} • iOS client: ✅')
    await interaction.response.send_message(embed=embed)


# ─── PREFIX-КОМАНДИ ───────────────────────────────────────────────────────────

async def _prefix_play(ctx: commands.Context, query: str):
    if not ctx.author.voice:
        await ctx.send(embed=discord.Embed(description='❌ Підключіться до голосового каналу!', color=COLOR_RED))
        return
    vc = ctx.voice_client
    if vc:
        if vc.channel != ctx.author.voice.channel:
            await vc.move_to(ctx.author.voice.channel)
    else:
        await ctx.author.voice.channel.connect()

    player      = await ensure_player(ctx.guild, ctx.channel, bot)
    is_playlist = is_real_playlist(query)
    if not is_playlist:
        query = clean_url(query)
    msg = await ctx.send(embed=discord.Embed(description='⏳ Завантажую...', color=COLOR_YELLOW))

    if is_playlist:
        tracks = await fetch_playlist(query, ctx.author)
        player.add_many(tracks)
        await msg.edit(embed=discord.Embed(description=f'✅ Додано **{len(tracks)}** треків з плейлиста!', color=COLOR_GREEN))
    else:
        track = await fetch_track(query, ctx.author, loop=bot.loop)
        if not track:
            await msg.edit(embed=discord.Embed(description='❌ Нічого не знайдено.', color=COLOR_RED))
            return
        player.add(track)
        embed = discord.Embed(
            description=f'✅ Додано: **[{track.title}]({track.webpage_url})**\n⏱️ {track.duration_str}',
            color=COLOR_GREEN,
        )
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        await msg.edit(embed=embed)


@bot.command(name='play', aliases=['p'])
async def cmd_play(ctx: commands.Context, *, query: str = None):
    if not query:
        await ctx.send(embed=discord.Embed(description='❌ Вкажіть URL або назву!', color=COLOR_RED))
        return
    await _prefix_play(ctx, query)

@bot.command(name='skip', aliases=['s', 'next'])
async def cmd_skip(ctx: commands.Context):
    if ctx.guild.id in players:
        players[ctx.guild.id].skip()
        await ctx.send(embed=discord.Embed(description='⏭️ Пропущено!', color=COLOR_GREEN))

@bot.command(name='stop', aliases=['dc'])
async def cmd_stop(ctx: commands.Context):
    gid = ctx.guild.id
    if gid in players:
        players[gid].clear(); players[gid].skip()
        await players[gid].destroy(); del players[gid]
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
    await ctx.send(embed=discord.Embed(description='⏹️ Зупинено!', color=COLOR_RED))

@bot.command(name='pause')
async def cmd_pause(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send(embed=discord.Embed(description='⏸️ Пауза', color=COLOR_YELLOW))
    elif vc and vc.is_paused():
        vc.resume()
        await ctx.send(embed=discord.Embed(description='▶️ Відновлено', color=COLOR_GREEN))

@bot.command(name='queue', aliases=['q'])
async def cmd_queue(ctx: commands.Context):
    gid = ctx.guild.id
    if gid not in players:
        await ctx.send('❌ Плеєр не запущено!'); return
    embed = make_queue_embed(players[gid], page=0)
    view  = QueueView(players[gid], page=0)
    await ctx.send(embed=embed, view=view)

@bot.command(name='np', aliases=['nowplaying', 'now'])
async def cmd_now(ctx: commands.Context):
    gid = ctx.guild.id
    if gid not in players or not players[gid].current:
        await ctx.send('❌ Нічого не грає!'); return
    track = players[gid].current
    embed = track.make_embed(filter_name=players[gid].audio_filter)
    view  = NowPlayingView(players[gid])
    await ctx.send(embed=embed, view=view)

@bot.command(name='volume', aliases=['vol', 'v'])
async def cmd_volume(ctx: commands.Context, percent: int = None):
    if percent is None:
        gid = ctx.guild.id
        vol = int(players[gid].volume * 100) if gid in players else 50
        await ctx.send(f'🔊 Поточна гучність: **{vol}%**'); return
    if not (0 <= percent <= 200):
        await ctx.send('❌ Гучність від 0 до 200!'); return
    gid = ctx.guild.id
    if gid in players:
        players[gid].set_volume(percent / 100)
    await ctx.send(embed=discord.Embed(description=f'🔊 Гучність: **{percent}%**', color=COLOR_GREEN))

@bot.command(name='shuffle')
async def cmd_shuffle(ctx: commands.Context):
    if ctx.guild.id in players:
        players[ctx.guild.id].shuffle()
        await ctx.send(embed=discord.Embed(description='🔀 Чергу перемішано!', color=COLOR_GREEN))

@bot.command(name='loop')
async def cmd_loop(ctx: commands.Context):
    gid = ctx.guild.id
    if gid in players:
        players[gid].cycle_loop()
        label, color = LOOP_LABELS[players[gid].loop_mode]
        await ctx.send(embed=discord.Embed(description=label, color=color))

@bot.command(name='history')
async def cmd_history(ctx: commands.Context):
    gid = ctx.guild.id
    if gid not in players or not players[gid].history:
        await ctx.send('📜 Історія порожня.'); return
    history = players[gid].history[-10:][::-1]
    embed   = discord.Embed(title='📜 Нещодавно грали', color=COLOR_BLUE)
    lines   = [f'`{i}.` [{t.title[:55]}]({t.webpage_url}) • {t.duration_str}' for i, t in enumerate(history, 1)]
    embed.description = '\n'.join(lines)
    await ctx.send(embed=embed)

@bot.command(name='bassboost', aliases=['bb'])
async def cmd_bassboost(ctx: commands.Context):
    gid = ctx.guild.id
    if gid not in players:
        await ctx.send('❌ Плеєр не запущено!'); return
    if players[gid].audio_filter == AudioFilter.BASSBOOST:
        players[gid].set_filter(AudioFilter.NONE)
        await ctx.send(embed=discord.Embed(description='🎵 Bass Boost вимкнено.', color=COLOR_BLUE))
    else:
        players[gid].set_filter(AudioFilter.BASSBOOST)
        await ctx.send(embed=discord.Embed(description='🔊 **Bass Boost увімкнено!**', color=COLOR_PURPLE))

@bot.command(name='nightcore', aliases=['nc'])
async def cmd_nightcore(ctx: commands.Context):
    gid = ctx.guild.id
    if gid not in players:
        await ctx.send('❌ Плеєр не запущено!'); return
    if players[gid].audio_filter == AudioFilter.NIGHTCORE:
        players[gid].set_filter(AudioFilter.NONE)
        await ctx.send(embed=discord.Embed(description='🎵 Nightcore вимкнено.', color=COLOR_BLUE))
    else:
        players[gid].set_filter(AudioFilter.NIGHTCORE)
        await ctx.send(embed=discord.Embed(description='⚡ **Nightcore увімкнено!**', color=COLOR_CYAN))

@bot.command(name='mix', aliases=['m'])
async def cmd_mix(ctx: commands.Context, count: str = '10', *, query: str = None):
    if not count.isdigit():
        query = (count + ' ' + query) if query else count
        count = '10'
    count = max(1, min(int(count), 50))

    if not query:
        await ctx.send(embed=discord.Embed(description='❌ Вкажи URL або назву!\nПриклад: `!mix 15 назва пісні`', color=COLOR_RED))
        return

    if not ctx.author.voice:
        await ctx.send(embed=discord.Embed(description='❌ Підключіться до голосового каналу!', color=COLOR_RED))
        return
    vc = ctx.voice_client
    if vc:
        if vc.channel != ctx.author.voice.channel:
            await vc.move_to(ctx.author.voice.channel)
    else:
        await ctx.author.voice.channel.connect()

    player = await ensure_player(ctx.guild, ctx.channel, bot)
    msg    = await ctx.send(embed=discord.Embed(description=f'🎲 Завантажую YouTube Mix ({count} треків)...', color=COLOR_YELLOW))

    tracks = await fetch_mix_tracks(query, ctx.author, count=count)

    if not tracks:
        await msg.edit(embed=discord.Embed(description='❌ Не вдалося завантажити Mix. Спробуй інше відео або запит.', color=COLOR_RED))
        return

    player.add_many(tracks)

    preview = '\n'.join(f'`{i}.` {t.title[:55]}' for i, t in enumerate(tracks[:5], 1))
    if len(tracks) > 5:
        preview += f'\n`...` і ще {len(tracks) - 5} треків'

    embed = discord.Embed(title=f'🎲 YouTube Mix — додано {len(tracks)} треків', description=preview, color=COLOR_CYAN)
    if tracks[0].thumbnail:
        embed.set_thumbnail(url=tracks[0].thumbnail)
    await msg.edit(embed=embed)


@bot.command(name='autoplay', aliases=['ap'])
async def cmd_autoplay(ctx: commands.Context):
    gid = ctx.guild.id
    if gid not in players:
        await ctx.send('❌ Плеєр не запущено!'); return
    players[gid].autoplay = not players[gid].autoplay
    state = '🟢 Увімкнено' if players[gid].autoplay else '🔴 Вимкнено'
    await ctx.send(embed=discord.Embed(description=f'🔄 Autoplay: **{state}**', color=COLOR_GREEN))

@bot.command(name='join')
async def cmd_join(ctx: commands.Context):
    if ctx.author.voice:
        ch = ctx.author.voice.channel
        if ctx.voice_client:
            await ctx.voice_client.move_to(ch)
        else:
            await ch.connect()
        await ctx.send(embed=discord.Embed(description=f'✅ Підключився до **{ch.name}**', color=COLOR_GREEN))
    else:
        await ctx.send('❌ Ви не у голосовому каналі!')

@bot.command(name='leave', aliases=['disconnect'])
async def cmd_leave(ctx: commands.Context):
    gid = ctx.guild.id
    if gid in players:
        await players[gid].destroy(); del players[gid]
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
    await ctx.send(embed=discord.Embed(description='👋 Відключився!', color=COLOR_YELLOW))

@bot.command(name='help', aliases=['h', 'commands'])
async def cmd_help(ctx: commands.Context):
    embed = discord.Embed(title='📖 Команди Music Bot v3', color=COLOR_BLUE)
    embed.add_field(name='🎵', value='`!play/p` `!skip/s` `!pause` `!stop/dc` `!np`', inline=False)
    embed.add_field(name='📋', value='`!queue/q` `!shuffle` `!loop` `!vol [0-200]`', inline=False)
    embed.add_field(name='✨', value='`!history` `!autoplay/ap` `!mix` `!bassboost/bb` `!nightcore/nc`', inline=False)
    embed.add_field(name='💡', value='Також є `/slash`-команди — введіть `/` у Discord', inline=False)
    await ctx.send(embed=embed)


# ─── Швидке відтворення: !https://... ────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    content = message.content.strip()
    if content.startswith('!http://') or content.startswith('!https://'):
        url = content[1:]
        ctx = await bot.get_context(message)
        await _prefix_play(ctx, url)
        return
    await bot.process_commands(message)


# ─── Очищення при відключенні від каналу ─────────────────────────────────────
@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    if member == bot.user and before.channel and not after.channel:
        gid = member.guild.id
        if gid in players:
            await players[gid].destroy()
            del players[gid]

    if not member.bot:
        vc = member.guild.voice_client
        if vc and before.channel == vc.channel:
            non_bots = [m for m in vc.channel.members if not m.bot]
            if not non_bots:
                await asyncio.sleep(10)
                vc = member.guild.voice_client
                if vc and not [m for m in vc.channel.members if not m.bot]:
                    gid = member.guild.id
                    if gid in players:
                        try:
                            await players[gid].text_channel.send(
                                embed=discord.Embed(description='😴 Усі пішли з каналу — відключився.', color=COLOR_YELLOW)
                            )
                        except Exception:
                            pass
                        await players[gid].destroy()
                        del players[gid]
                    await vc.disconnect()


# ─── on_ready ─────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f'✅ Бот {bot.user} готовий до роботи! (v3)')
    print(f'   Сервери: {len(bot.guilds)}')
    cookies_status = 'COOKIES_B64' if _COOKIES_TMP else ('файл: ' + COOKIE_FILE if COOKIE_FILE else 'вимкнено')
    print(f'   Cookies: {cookies_status}')
    print(f'   YouTube client: iOS')
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name='/play | !play'))
    try:
        synced = await bot.tree.sync()
        print(f'   Slash-команд синхронізовано: {len(synced)}')
    except Exception as e:
        print(f'   Помилка синхронізації: {e}')


# ─── Запуск ───────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    bot.run(TOKEN)
