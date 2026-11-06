# Bot bảng trạng thái Genshin cho server Discord
#
# Bot đăng một tin nhắn "Server Status" rồi tự sửa nó mỗi 10 phút:
#   - Số thành viên server
#   - Giờ hiện tại của từng cụm server NA / EU / ASIA / SAR
#   - Đếm ngược tới daily reset và weekly reset của từng cụm
#
# Cách dùng nhanh:
#   1. Tạo bot tại https://discord.com/developers/applications
#      (Application mới -> mục Bot -> Reset Token -> copy token, GIỮ KÍN,
#      đừng gửi token cho ai, kể cả qua chat)
#   2. Trong mục Bot, bật "Server Members Intent" để đếm thành viên chính xác.
#   3. Mời bot vào server: mục OAuth2 -> URL Generator -> chọn scope
#      "bot" và "applications.commands", quyền "Send Messages",
#      "Embed Links", "Read Message History" -> mở link, chọn server.
#   4. Cài đặt rồi chạy:
#        pip install -U discord.py
#        DISCORD_TOKEN=<token của bot> python bot.py
#   5. Trong Discord, vào kênh muốn đặt bảng rồi gõ /poststatus.
#      Bảng sẽ xuất hiện và tự cập nhật mỗi 10 phút.
#
# Muốn đổi kênh đặt bảng: vào kênh mới gõ lại /poststatus là xong.

import asyncio
import json
import os
import random

import yt_dlp
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
# --- Chẩn đoán voice: in ra để xem vì sao discord.py báo thiếu thư viện voice ---
try:
    import nacl as _nacl
    print("nacl import OK:", getattr(_nacl, "__version__", "?"))
except Exception as e:
    print("nacl import FAILED:", repr(e))
try:
    from discord.voice_client import has_nacl as _has_nacl
    print("discord has_nacl:", _has_nacl)
except Exception as e:
    print("has_nacl check FAILED:", repr(e))

from discord.ext import tasks

# --- Cấu hình ---
UPDATE_INTERVAL_MINUTES = 10  # cứ 10 phút sửa bảng một lần
STATE_FILE = "board.json"     # lưu id kênh + id tin nhắn bảng

# Genshin: reset ngày lúc 04:00 giờ server, reset tuần lúc 04:00 sáng thứ Hai
# giờ server. Múi giờ các cụm server là cố định, không đổi theo mùa hè/đông.
REGIONS = [
    ("NA", -5),    # Mỹ: UTC-5
    ("EU", 1),     # Châu Âu: UTC+1
    ("ASIA", 8),   # Châu Á: UTC+8
    ("SAR", 8),    # Đài Loan / Hong Kong / Macau: UTC+8
]
RESET_HOUR = 4  # 04:00


# --- Logic giờ giấc (không phụ thuộc Discord, dễ kiểm chứng) ---

def _server_now(now_utc, offset_hours):
    """Giờ hiện tại theo giờ của cụm server."""
    return now_utc + timedelta(hours=offset_hours)


def next_daily_reset(now_utc, offset_hours):
    """Mốc daily reset kế tiếp, trả về dạng giờ UTC."""
    server_now = _server_now(now_utc, offset_hours)
    target = server_now.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0)
    if target <= server_now:
        target += timedelta(days=1)
    return target - timedelta(hours=offset_hours)


def next_weekly_reset(now_utc, offset_hours):
    """Mốc weekly reset kế tiếp (sáng thứ Hai 04:00 giờ server), giờ UTC."""
    server_now = _server_now(now_utc, offset_hours)
    target = server_now.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0)
    days_until_monday = (0 - server_now.weekday()) % 7  # thứ Hai = weekday 0
    target += timedelta(days=days_until_monday)
    if target <= server_now:
        target += timedelta(days=7)
    return target - timedelta(hours=offset_hours)


def build_board(member_count, now_utc):
    """Dựng nội dung bảng, trả về dict để dễ kiểm chứng và dễ đổi mẫu."""
    fields = []
    for name, offset in REGIONS:
        server_now = _server_now(now_utc, offset)
        clock = server_now.strftime("%I:%M %p")  # ví dụ: 01:36 AM
        daily_ts = int(next_daily_reset(now_utc, offset).timestamp())
        weekly_ts = int(next_weekly_reset(now_utc, offset).timestamp())
        # <t:...:R> là timestamp của Discord: nó tự hiện dạng đếm ngược
        # ("2 giờ tới", "5 ngày tới") theo ngôn ngữ máy của người xem.
        fields.append({
            "name": name,
            "value": (
                f"# {clock}\n"
                f"• Daily reset <t:{daily_ts}:R>\n"
                f"• Weekly reset <t:{weekly_ts}:R>"
            ),
        })
    return {
        "title": "Server Status",
        "description": f"Members: {member_count}",
        "fields": fields,
    }


def board_to_embed(board):
    embed = discord.Embed(title=board["title"], description=board["description"])
    for field in board["fields"]:
        embed.add_field(name=field["name"], value=field["value"], inline=False)
    embed.set_footer(text=f"Tự động cập nhật mỗi {UPDATE_INTERVAL_MINUTES} phút")
    return embed


# --- Lưu / đọc vị trí bảng ---

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(channel_id, message_id):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"channel_id": channel_id, "message_id": message_id}, f)


# --- Game nối từ (kênh khu-vui-choi) ---
# Luật: từ sau phải bắt đầu bằng chữ cuối của từ trước.
# Ví dụ: trang sang -> sang mai -> mai mốt -> mốt ...
WORDCHAIN_CHANNEL_ID = 1535494079821910026  # khu-vui-choi-♠️♦️♥️♣️
WORDCHAIN_FILE = "wordchain.json"

# Từ điển nhỏ để bot nối tiếp: chữ bắt đầu -> cụm 2 từ bot sẽ dùng
WORD_BANK = {
    "an": [
        "an toàn",
        "an ninh",
        "an tâm"
    ],
    "anh": [
        "anh em",
        "anh hùng"
    ],
    "ba": [
        "ba mẹ",
        "ba lô"
    ],
    "bạc": [
        "bạc màu"
    ],
    "bài": [
        "bài học",
        "bài hát"
    ],
    "bạn": [
        "bạn bè",
        "bạn thân"
    ],
    "bán": [
        "bán hàng"
    ],
    "bao": [
        "bao giờ",
        "bao nhiêu"
    ],
    "bảo": [
        "bảo vệ"
    ],
    "bắc": [
        "bắc ninh"
    ],
    "bắt": [
        "bắt đầu"
    ],
    "bình": [
        "bình yên"
    ],
    "biển": [
        "biển xanh",
        "biển cả"
    ],
    "biết": [
        "biết ơn"
    ],
    "bỏ": [
        "bỏ qua"
    ],
    "bóng": [
        "bóng đá"
    ],
    "bơi": [
        "bơi lội"
    ],
    "bốn": [
        "bốn mùa"
    ],
    "buổi": [
        "buổi sáng",
        "buổi tối"
    ],
    "cá": [
        "cá heo",
        "cá chép"
    ],
    "cảm": [
        "cảm ơn",
        "cảm xúc"
    ],
    "cao": [
        "cao lớn",
        "cao cấp"
    ],
    "cây": [
        "cây xanh",
        "cây cối"
    ],
    "chào": [
        "chào hỏi"
    ],
    "chạy": [
        "chạy bộ"
    ],
    "chăm": [
        "chăm chỉ"
    ],
    "chân": [
        "chân thành"
    ],
    "chị": [
        "chị em"
    ],
    "chiều": [
        "chiều nay"
    ],
    "chơi": [
        "chơi game",
        "chơi đùa"
    ],
    "chó": [
        "chó cưng"
    ],
    "chung": [
        "chung tay"
    ],
    "chuyện": [
        "chuyện trò"
    ],
    "công": [
        "công việc",
        "công ty"
    ],
    "con": [
        "con mèo",
        "con cá",
        "con người"
    ],
    "cười": [
        "cười tươi"
    ],
    "cuối": [
        "cuối tuần"
    ],
    "dài": [
        "dài lâu"
    ],
    "dân": [
        "dân chủ"
    ],
    "dạy": [
        "dạy học"
    ],
    "dễ": [
        "dễ thương"
    ],
    "đẹp": [
        "đẹp trai",
        "đẹp gái",
        "đẹp mắt"
    ],
    "đêm": [
        "đêm khuya",
        "đêm nay"
    ],
    "đến": [
        "đến nơi"
    ],
    "đi": [
        "đi chơi",
        "đi học"
    ],
    "điện": [
        "điện thoại"
    ],
    "đỏ": [
        "đỏ tươi"
    ],
    "đói": [
        "đói bụng"
    ],
    "đồng": [
        "đồng hồ"
    ],
    "đông": [
        "đông vui"
    ],
    "đời": [
        "đời sống"
    ],
    "đủ": [
        "đủ đầy"
    ],
    "đường": [
        "đường phố"
    ],
    "em": [
        "em gái",
        "em trai"
    ],
    "gái": [
        "gái xinh"
    ],
    "game": [
        "game hay"
    ],
    "gió": [
        "gió mát"
    ],
    "giỏi": [
        "giỏi giang"
    ],
    "giấc": [
        "giấc mơ"
    ],
    "hạnh": [
        "hạnh phúc"
    ],
    "hay": [
        "hay ho"
    ],
    "học": [
        "học bài",
        "học giỏi"
    ],
    "hồng": [
        "hồng hào"
    ],
    "hôm": [
        "hôm nay",
        "hôm qua"
    ],
    "hứng": [
        "hứng thú"
    ],
    "khí": [
        "khí hậu"
    ],
    "không": [
        "không khí",
        "không gian",
        "không ngờ"
    ],
    "khỏe": [
        "khỏe mạnh"
    ],
    "khuya": [
        "khuya rồi"
    ],
    "làm": [
        "làm việc"
    ],
    "lần": [
        "lần sau",
        "lần đầu"
    ],
    "lá": [
        "lá cây"
    ],
    "lâu": [
        "lâu rồi"
    ],
    "lớn": [
        "lớn lên"
    ],
    "mai": [
        "mai mốt",
        "mai sau"
    ],
    "màu": [
        "màu sắc"
    ],
    "may": [
        "may mắn"
    ],
    "mắn": [
        "mắn đẻ"
    ],
    "mắt": [
        "mắt cười"
    ],
    "máy": [
        "máy tính"
    ],
    "mẹ": [
        "mẹ hiền"
    ],
    "mèo": [
        "mèo mun",
        "mèo con"
    ],
    "mình": [
        "mình ơi"
    ],
    "mới": [
        "mới mẻ",
        "mới lạ"
    ],
    "mốt": [
        "mốt mới"
    ],
    "một": [
        "một mình",
        "một ngày"
    ],
    "mưa": [
        "mưa rào"
    ],
    "mun": [
        "mun đen"
    ],
    "nắng": [
        "nắng mai",
        "nắng vàng"
    ],
    "ngày": [
        "ngày mai",
        "ngày mới"
    ],
    "ngon": [
        "ngon miệng"
    ],
    "ngủ": [
        "ngủ ngon"
    ],
    "nhà": [
        "nhà cửa"
    ],
    "nhanh": [
        "nhanh nhẹn"
    ],
    "nhỏ": [
        "nhỏ xinh"
    ],
    "ninh": [
        "ninh bình"
    ],
    "nói": [
        "nói chuyện"
    ],
    "nước": [
        "nước mắt",
        "nước biển"
    ],
    "ôn": [
        "ôn bài"
    ],
    "phố": [
        "phố xá"
    ],
    "qua": [
        "qua lại"
    ],
    "quà": [
        "quà tặng"
    ],
    "sáng": [
        "sáng mai",
        "sáng sớm"
    ],
    "sang": [
        "sang mai",
        "sang trọng"
    ],
    "sâu": [
        "sâu sắc"
    ],
    "siêu": [
        "siêu nhân"
    ],
    "sớm": [
        "sớm mai"
    ],
    "tây": [
        "tây ninh",
        "tây bắc"
    ],
    "thành": [
        "thành công"
    ],
    "thơm": [
        "thơm ngon"
    ],
    "thường": [
        "thường ngày",
        "thường xuyên"
    ],
    "thú": [
        "thú vị"
    ],
    "tiếng": [
        "tiếng việt"
    ],
    "tối": [
        "tối nay"
    ],
    "trà": [
        "trà sữa"
    ],
    "trắng": [
        "trắng tinh"
    ],
    "trẻ": [
        "trẻ con",
        "trẻ trung"
    ],
    "trời": [
        "trời xanh"
    ],
    "trọng": [
        "trọng lượng"
    ],
    "trưa": [
        "trưa nay"
    ],
    "tươi": [
        "tươi cười"
    ],
    "tuyết": [
        "tuyết trắng"
    ],
    "vàng": [
        "vàng óng"
    ],
    "vui": [
        "vui vẻ",
        "vui chơi"
    ],
    "xa": [
        "xa xôi"
    ],
    "xanh": [
        "xanh lá",
        "xanh dương"
    ],
    "xinh": [
        "xinh đẹp",
        "xinh xắn"
    ],
    "xuyên": [
        "xuyên suốt",
        "xuyên đêm"
    ],
    "yên": [
        "yên bình",
        "yên tĩnh"
    ],
    "yêu": [
        "yêu thương"
    ]
}


# --- Từ điển thật: file tu_dien.txt (22.673 cụm 2 từ tiếng Việt, nguồn bi-grams) + WORD_BANK ---
def load_dictionary():
    phrases = set()
    try:
        with open("tu_dien.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip().lower()
                if line:
                    phrases.add(line)
    except OSError:
        pass
    for vals in WORD_BANK.values():
        phrases.update(v.lower() for v in vals)
    idx = {}
    for ph in phrases:
        w = ph.split()
        if len(w) == 2:
            idx.setdefault(w[0], []).append(ph)
    for k in idx:
        idx[k].sort()
    return idx

DICTIONARY = load_dictionary()


def _norm(s):
    return " ".join(s.strip().lower().split())


def load_wordchain():
    try:
        with open(WORDCHAIN_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            last = d.get("last", "mốt")
            return last, set(d.get("used", [])), d.get("last_phrase", "mai mốt")
    except (OSError, ValueError):
        # Trạng thái hôm 2026-11-05: trang sang -> sang mai -> mai mốt
        return "mốt", {"trang sang", "sang mai", "mai mốt"}, "mai mốt"


def save_wordchain(last, used, last_phrase=None):
    try:
        with open(WORDCHAIN_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"last": last, "used": sorted(used),
                 "last_phrase": last_phrase or last},
                f, ensure_ascii=False,
            )
    except OSError:
        pass


def _continuations(word, used):
    """Các cụm chưa dùng mà bắt đầu bằng `word` — để bot tránh nối vào ngõ cụt."""
    return [q for q in WORD_BANK.get(word, []) if q not in used]


# --- Bot ---

intents = discord.Intents.default()
intents.members = True  # để đếm thành viên chính xác
intents.message_content = True  # để đọc từ nối trong kênh khu-vui-choi
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


async def update_board_once():
    """Cập nhật bảng đã đặt (nếu có). Tin nhắn cũ mất thì đăng bảng mới."""
    state = load_state()
    if not state:
        return
    try:
        channel = client.get_channel(state["channel_id"]) or await client.fetch_channel(
            state["channel_id"]
        )
    except Exception:
        return
    board = build_board(channel.guild.member_count or 0, datetime.now(timezone.utc))
    embed = board_to_embed(board)
    try:
        message = await channel.fetch_message(state["message_id"])
        await message.edit(embed=embed)
    except Exception:
        message = await channel.send(embed=embed)
        save_state(channel.id, message.id)


@tasks.loop(minutes=UPDATE_INTERVAL_MINUTES)
async def update_board_loop():
    await update_board_once()


@tasks.loop(minutes=2)
async def voice_watchdog():
    """Bot rớt khỏi voice thì tự vô lại kênh Nhạc hot trend."""
    for guild in client.guilds:
        if guild.id not in trend_mode:
            continue
        vc = guild.voice_client
        if vc is not None and vc.is_connected():
            continue
        for ch in guild.voice_channels:
            if TREND_CHANNEL_NAME.lower() in ch.name.strip().lower():
                try:
                    vc = await ch.connect()
                    print(f"voice_watchdog: đã vô lại kênh trend: {ch.name}")
                    await _play_next(guild.id, vc)
                except Exception as e:
                    print("voice_watchdog failed:", e)
                break


@tasks.loop(minutes=5)
async def trend_loop():
    for guild in client.guilds:
        if guild.id not in trend_mode:
            continue
        vc = guild.voice_client
        if vc is not None and vc.is_connected() and not vc.is_playing() and not vc.is_paused():
            print(f"trend_loop: phát lại nhạc trend ở {guild.name}")
            await _play_next(guild.id, vc)


@client.event
async def on_ready():
    print(f"Đã đăng nhập: {client.user} (id: {client.user.id})")
    await tree.sync()
    await update_board_once()
    if not update_board_loop.is_running():
        update_board_loop.start()
    if not trend_loop.is_running():
        trend_loop.start()
    if not voice_watchdog.is_running():
        voice_watchdog.start()
    # Ở lại voice 24/24: vô lại kênh voice đã lưu trước khi bot khởi động lại
    try:
        with open("voice.json", "r", encoding="utf-8") as f:
            saved = json.load(f)
        guild = client.get_guild(int(saved["guild_id"]))
        channel = guild.get_channel(int(saved["channel_id"])) if guild else None
        if channel is not None and guild.voice_client is None:
            await channel.connect()
            print(f"Đã vô lại voice: {channel.name}")
    except FileNotFoundError:
        pass  # chưa lưu kênh voice nào, lần đầu chạy là bình thường
    except (OSError, ValueError, KeyError, AttributeError, discord.DiscordException) as e:
        print("Không vô lại voice được:", e)
    # Vô kênh "Nhạc hot trend" và phát nhạc trend 24/7
    for guild in client.guilds:
        if guild.voice_client is not None:
            continue
        found_trend = False
        for ch in guild.voice_channels:
            if TREND_CHANNEL_NAME.lower() in ch.name.strip().lower():
                found_trend = True
                try:
                    vc = await ch.connect()
                    trend_mode.add(guild.id)
                    with open("voice.json", "w", encoding="utf-8") as f:
                        json.dump({"guild_id": guild.id, "channel_id": ch.id}, f)
                    print(f"Đã vô kênh trend: {ch.name}")
                    await _play_next(guild.id, vc)
                except Exception as e:
                    print("Không vô kênh trend được:", e)
                break
        if not found_trend:
            print("Không tìm thấy kênh Nhạc hot trend. Các kênh voice:",
                  [c.name for g in client.guilds for c in g.voice_channels])


@client.event
async def on_message(message):
    if message.author.bot or message.channel.id != WORDCHAIN_CHANNEL_ID:
        return
    text = _norm(message.content)
    if not text or text.startswith("/") or text.startswith("!"):
        return
    words = text.split()
    if len(words) > 2 or any(len(w) > 10 for w in words):
        return
    last, used, _phrase = load_wordchain()
    if words[0] != last:
        await message.channel.send(
            f"Chưa đúng nha {message.author.mention} 🥺 Từ phải bắt đầu bằng **{last}**, "
            f"chứ không phải **{words[0]}**. Thử lại đi!"
        )
        return
    if text in used:
        await message.channel.send(
            f"Từ **{text}** dùng rồi nè {message.author.mention}, nghĩ từ khác bắt đầu bằng **{last}** đi!"
        )
        return
    used.add(text)
    new_last = words[-1]
    save_wordchain(new_last, used, text)
    # Bot CHƠI CHUNG: chỉ nối bằng cụm có thật trong từ điển, không tự bịa
    options = [q for q in DICTIONARY.get(new_last, []) if q not in used]
    if not options:
        await message.channel.send(
            f"Đúng rồi {message.author.mention}! ✅ Tới **{new_last}** là Bom bí thật rồi, ai nối tiếp giúp Bom đi 😅"
        )
        return
    reply = random.choice(options)
    used.add(reply)
    save_wordchain(reply.split()[-1], used, reply)
    await message.channel.send(reply)



# --- Nhạc: phát nhạc YouTube vào voice channel ---
YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    # Dùng client Android của YouTube để khỏi bị chặn "Sign in to confirm you're not a bot"
    "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
}
# -reconnect chỉ có tác dụng với link http, dùng cho file local sẽ lỗi "Option reconnect not found"
FFMPEG_OPTS_URL = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}
FFMPEG_OPTS_FILE = {"options": "-vn"}
music_queues = {}  # guild_id -> list of (title, audio_url)
trend_mode = set()  # guild_id đang phát nhạc hot trend 24/7
TREND_CHANNEL_NAME = "Nhạc hot trend"
# Danh sách nhạc trend xoay vòng: show Việt + US-UK + Việt-Hàn hot TikTok 2020-2027
TREND_QUERIES = [
    "Anh Trai Say Hi 2",
    "Anh Trai Vượt Ngàn Chông Gai 2",
    "Chị Đẹp Đạp Gió Rẽ Sóng 2",
    "Em Xinh Say Hi 2",
    "nhạc US UK hot trend 2025 2026",
    "Billboard Hot 100 2026",
    "nhạc trẻ Việt hot TikTok 2026",
    "nhạc Hàn hot TikTok 2026",
    "nhạc hot trend 2024 2025",
    "nhạc trẻ hot trend 2020 2021 2022 2023",
    "nhạc Âu Mỹ hay nhất 2026",
    "nhạc Việt hot trend 2027",
]
trend_query_index = 0
music_text_channels = {}  # guild_id -> text channel id để đăng "đang phát" kiểu Apple Music

MUSIC_DIR = "/tmp/bot_music"
os.makedirs(MUSIC_DIR, exist_ok=True)

def _download_song(song):
    """Tải bài hát về file local rồi mới phát (tránh lỗi 403 khi stream trực tiếp)."""
    import glob
    import hashlib
    url = song.get("webpage_url") or song.get("url")
    if not url:
        return None
    key = hashlib.md5(url.encode()).hexdigest()[:12]
    existing = glob.glob(os.path.join(MUSIC_DIR, f"{key}.*"))
    if existing:
        return existing[0]
    outtmpl = os.path.join(MUSIC_DIR, f"{key}.%(ext)s")
    opts = dict(YTDL_OPTS)
    opts.update({"format": "bestaudio/best", "outtmpl": outtmpl})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        print(f"download failed ({url}):", e)
        return None
    got = glob.glob(os.path.join(MUSIC_DIR, f"{key}.*"))
    if got and os.path.getsize(got[0]) > 30 * 1024 * 1024:
        print(f"download skipped, file too big: {got[0]}")
        os.remove(got[0])
        return None
    return got[0] if got else None


def _trend_songs():
    global trend_query_index
    query = TREND_QUERIES[trend_query_index % len(TREND_QUERIES)]
    trend_query_index += 1
    print(f"trend search: {query}")
    for q in (f"ytsearch10:{query}", f"scsearch10:{query}"):
        try:
            with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
                info = ydl.extract_info(q, download=False)
            out = []
            for e in info.get("entries", []) or []:
                if e and e.get("url"):
                    dur = e.get("duration") or 0
                    if dur and dur > 480:
                        continue  # bỏ mix/set dài trên 8 phút
                    out.append({
                        "title": e.get("title", "Bài hát"),
                        "url": e["url"],
                        "artist": e.get("uploader", e.get("channel", "Không rõ nghệ sĩ")),
                        "thumbnail": e.get("thumbnail"),
                        "duration": dur,
                        "webpage_url": e.get("webpage_url", ""),
                    })
            if out:
                return out
        except Exception as e:
            print(f"trend search failed ({q}):", e)
    return []

def _ytdl_info(query):
    last_err = None
    for q in (query, f"scsearch1:{query}"):
        try:
            with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
                info = ydl.extract_info(q, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return {
                "title": info.get("title", "Bài hát"),
                "url": info.get("url"),
                "artist": info.get("uploader", info.get("channel", "Không rõ nghệ sĩ")),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration") or 0,
                "webpage_url": info.get("webpage_url", ""),
            }
        except Exception as e:
            last_err = e
            print(f"_ytdl_info failed ({q}):", e)
    raise last_err

def _fmt_duration(sec):
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


def _now_playing_embed(song):
    embed = discord.Embed(
        title=f"🎵 {song['title']}",
        description=f"🎤 {song.get('artist', 'Không rõ nghệ sĩ')}",
        color=0xFA57C1,  # hồng Apple Music
    )
    if song.get("thumbnail"):
        embed.set_thumbnail(url=song["thumbnail"])
    bar = "━━━━━━●━━━━━━━━━━"
    embed.add_field(name="Đang phát", value=f"{bar}\n0:00  •  {_fmt_duration(song.get('duration'))}", inline=False)
    if song.get("webpage_url"):
        embed.add_field(name="Nghe trên YouTube", value=song["webpage_url"], inline=False)
    embed.set_footer(text="Bot tuất Music  •  giống Apple Music")
    return embed


class MusicControls(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=600)
        self.guild_id = guild_id

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.secondary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc:
            if vc.is_playing():
                vc.pause()
                button.emoji = "▶️"
            elif vc.is_paused():
                vc.resume()
                button.emoji = "⏸️"
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        await interaction.response.defer()

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc:
            music_queues[interaction.guild_id] = []
            trend_mode.discard(interaction.guild_id)
            await vc.disconnect()
        await interaction.response.defer()


def find_music_text_channel(guild):
    for ch in guild.text_channels:
        if "order-nhạc" in ch.name or "order-nhac" in ch.name:
            return ch
    return None


play_locks = {}

async def _play_next(guild_id, voice_client):
    lock = play_locks.setdefault(guild_id, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        await _play_next_locked(guild_id, voice_client)


async def _play_next_locked(guild_id, voice_client):
    if voice_client.is_playing() or voice_client.is_paused():
        return
    while True:
        queue = music_queues.get(guild_id, [])
        if not queue and guild_id in trend_mode:
            try:
                songs = await asyncio.to_thread(_trend_songs)
            except Exception as e:
                print("trend search error:", e)
                songs = []
            music_queues[guild_id] = list(songs)
            queue = music_queues[guild_id]
        if not queue:
            return
        song = queue.pop(0)
        local = await asyncio.to_thread(_download_song, song)
        try:
            if local:
                source = discord.FFmpegPCMAudio(local, **FFMPEG_OPTS_FILE)
            else:
                source = discord.FFmpegPCMAudio(song["url"], **FFMPEG_OPTS_URL)
        except Exception as e:
            print("music error: không tạo được audio:", repr(e))
            continue
        def after(err):
            if err:
                print("music error:", err)
            coro = _play_next(guild_id, voice_client)
            asyncio.run_coroutine_threadsafe(coro, client.loop)
        try:
            voice_client.play(source, after=after)
        except Exception as e:
            print("music error: không phát được:", repr(e))
            continue
        break
    if guild_id in trend_mode:
        guild = client.get_guild(guild_id)
        ch = None
        if guild is not None:
            ch_id = music_text_channels.get(guild_id)
            ch = guild.get_channel(ch_id) if ch_id else find_music_text_channel(guild)
            if ch is not None:
                music_text_channels[guild_id] = ch.id
        if ch is not None:
            try:
                await ch.send(embed=_now_playing_embed(song), view=MusicControls(guild_id))
            except discord.DiscordException as e:
                print("now-playing embed failed:", e)

@tree.command(name="nhac", description="Phát nhạc YouTube: /nhac tên bài hát hoặc link")
@app_commands.describe(bai="Tên bài hát hoặc link YouTube")
async def nhac(interaction: discord.Interaction, bai: str):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("An vào voice channel trước rồi gõ lại nha 🎧", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        song = await asyncio.to_thread(_ytdl_info, bai)
    except Exception as e:
        await interaction.followup.send(f"Không tìm được bài này, YouTube chặn rồi thử bài khác nha 😢 ({e})")
        return
    if not song.get("url"):
        await interaction.followup.send("Không lấy được nhạc của bài này, thử bài khác nha 😢")
        return
    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    try:
        if vc is None:
            vc = await channel.connect()
        elif vc.channel != channel:
            await vc.move_to(channel)
    except discord.Forbidden:
        await interaction.followup.send("Bot chưa có quyền vào voice channel này. An cho bot quyền **Kết nối** và **Nói** trong kênh voice nha.")
        return
    with open("voice.json", "w", encoding="utf-8") as f:
        json.dump({"guild_id": interaction.guild_id, "channel_id": channel.id}, f)
    music_text_channels[interaction.guild_id] = interaction.channel_id
    music_queues.setdefault(interaction.guild_id, []).append(song)
    if not vc.is_playing() and not vc.is_paused():
        await _play_next(interaction.guild_id, vc)
        await interaction.followup.send(embed=_now_playing_embed(song), view=MusicControls(interaction.guild_id))
    else:
        await interaction.followup.send(embed=_now_playing_embed(song).add_field(name="Đã thêm vào hàng chờ 🎵", value="Phát sau bài đang phát nha", inline=False), view=MusicControls(interaction.guild_id))

@tree.command(name="dung", description="Tạm dừng nhạc")
async def dung(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("Tạm dừng nhạc rồi ⏸️")
    else:
        await interaction.response.send_message("Đâu có nhạc đang phát đâu 😅", ephemeral=True)

@tree.command(name="tiep", description="Phát tiếp nhạc đang tạm dừng")
async def tiep(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("Phát tiếp nè ▶️")
    else:
        await interaction.response.send_message("Không có nhạc đang tạm dừng 😅", ephemeral=True)

@tree.command(name="boi", description="Bỏ qua bài đang phát, sang bài tiếp theo")
async def boi(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await interaction.response.send_message("Bỏ qua bài này, sang bài tiếp ⏭️")
    else:
        await interaction.response.send_message("Đâu có nhạc đang phát đâu 😅", ephemeral=True)

@tree.command(name="vao", description="Gọi bot vào voice channel của An")
async def vao(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("An vào voice channel trước rồi gõ lại nha 🎧", ephemeral=True)
        return
    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    try:
        if vc is None:
            vc = await channel.connect()
        elif vc.channel != channel:
            await vc.move_to(channel)
    except discord.Forbidden:
        await interaction.response.send_message("Bot chưa có quyền vào voice channel này. An cho bot quyền **Kết nối** và **Nói** trong kênh voice nha.")
        return
    with open("voice.json", "w", encoding="utf-8") as f:
        json.dump({"guild_id": interaction.guild_id, "channel_id": channel.id}, f)
    await interaction.response.send_message(f"Bot vô kênh **{channel.name}** rồi nè 🎧")


@tree.command(name="trend", description="Phát nhạc hot trend liên tục trong voice channel")
async def trend(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("An vào voice channel trước rồi gõ lại nha 🎧", ephemeral=True)
        return
    await interaction.response.defer()
    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    try:
        if vc is None:
            vc = await channel.connect()
        elif vc.channel != channel:
            await vc.move_to(channel)
    except discord.Forbidden:
        await interaction.followup.send("Bot chưa có quyền vào voice channel này. An cho bot quyền **Kết nối** và **Nói** trong kênh voice nha.")
        return
    trend_mode.add(interaction.guild_id)
    music_text_channels[interaction.guild_id] = interaction.channel_id
    with open("voice.json", "w", encoding="utf-8") as f:
        json.dump({"guild_id": interaction.guild_id, "channel_id": channel.id}, f)
    if not vc.is_playing() and not vc.is_paused():
        await _play_next(interaction.guild_id, vc)
    await interaction.followup.send("Phát nhạc hot trend liên tục nè 🎵🔥")


@tree.command(name="roi", description="Bot rời khỏi voice channel")
async def roi(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        music_queues[interaction.guild_id] = []
        trend_mode.discard(interaction.guild_id)
        try:
            os.remove("voice.json")
        except OSError:
            pass
        await vc.disconnect()
        await interaction.response.send_message("Bot về rồi đây 👋")
    else:
        await interaction.response.send_message("Bot đâu có trong voice đâu 😅", ephemeral=True)

@tree.command(name="choilai", description="Xoá hết tin trong kênh này rồi chơi nối từ lại từ đầu")
async def choilai(interaction: discord.Interaction):
    if interaction.channel_id != WORDCHAIN_CHANNEL_ID:
        await interaction.response.send_message("Lệnh này chỉ dùng ở kênh khu-vui-choi thôi nha.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Chỉ admin mới xoá kênh chơi lại được nha.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=500)
    except discord.Forbidden:
        await interaction.followup.send(
            "Bom chưa có quyền xoá tin trong kênh này. An vào Cài đặt kênh khu-vui-choi → Quyền → cho Bot tuất quyền **Quản lý tin nhắn** rồi gõ lại /choilai nha.",
            ephemeral=True,
        )
        return
    save_wordchain("sang", {"trang sang"}, "trang sang")
    await interaction.channel.send("Chơi lại từ đầu nè! 🎉\n\n**trang sang**")
    await interaction.followup.send(f"Xong rồi, đã xoá {len(deleted)} tin và chơi lại từ đầu.", ephemeral=True)


@tree.command(name="goiy", description="Gợi ý 1 từ nối tiếp hợp lệ (từ điển thật)")
async def goiy(interaction: discord.Interaction):
    if interaction.channel_id != WORDCHAIN_CHANNEL_ID:
        await interaction.response.send_message("Lệnh này chỉ dùng ở kênh khu-vui-choi thôi nha.", ephemeral=True)
        return
    last, used, _phrase = load_wordchain()
    options = [w for w in DICTIONARY.get(last, []) if w not in used]
    if not options:
        await interaction.response.send_message(
            f"Tới **{last}** là Bom cũng bí thật rồi, mọi người nghĩ giúp Bom đi 😅", ephemeral=False
        )
        return
    await interaction.response.send_message(f"Gợi ý nè: **{options[0]}** — tới **{options[0].split()[-1]}** nha!")


@tree.command(name="poststatus", description="Đặt bảng trạng thái Genshin ở kênh này")
async def poststatus(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Chỉ admin server mới dùng được lệnh này.", ephemeral=True
        )
        return
    board = build_board(
        interaction.guild.member_count or 0, datetime.now(timezone.utc)
    )
    await interaction.response.send_message(embed=board_to_embed(board))
    message = await interaction.original_response()
    save_state(interaction.channel_id, message.id)


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Chưa đặt DISCORD_TOKEN. Ví dụ: DISCORD_TOKEN=xxx python bot.py")
    client.run(token)
