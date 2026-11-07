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
import re
import time
import unicodedata
import urllib.parse
import urllib.request

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

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for _n in ("discord.voice_client", "discord.player", "discord.voice_state", "discord.gateway"):
    logging.getLogger(_n).setLevel(logging.DEBUG)

# --- FIX DAVE 14:41 PST 2026-11-06: discord.py 2.7.1 không bao giờ gọi
# dave_session.prepare_transition() / execute_transition() nên session DAVE
# không bao giờ ready (dave_proto=1, dave_ready=False, can_encrypt=False) và
# server Discord bỏ tiếng của bot dù bot vẫn phát nhạc. Patch hai chỗ theo
# đúng luồng DAVE (xem davey docs + daveprotocol.com):
# - op 21 DAVE_PREPARE_TRANSITION: chuẩn bị session trước khi handler gốc gửi
#   transition-ready (op 23).
# - _execute_transition (chạy khi server gửi op 22): chuyển session sang key mới.
from discord.voice_state import VoiceConnectionState as _VCS
from discord.gateway import DiscordVoiceWebSocket as _DWS

_orig_dave_recv = _DWS.received_message
async def _patched_dave_recv(self, msg):
    try:
        if isinstance(msg, dict) and msg.get('op') == _DWS.DAVE_PREPARE_TRANSITION:
            _d = msg.get('d') or {}
            _ds = getattr(self._connection, 'dave_session', None)
            if _ds is not None:
                _ds.prepare_transition(int(_d.get('transition_id', 0)), int(_d.get('protocol_version', 1)))
                print(f"DAVE prepare_transition id={_d.get('transition_id')} proto={_d.get('protocol_version')} status={_ds.status} ready={_ds.ready}")
    except Exception as _e:
        print(f"DAVE prepare_transition failed: {_e!r}")
    await _orig_dave_recv(self, msg)
_DWS.received_message = _patched_dave_recv

_orig_dave_exec = _VCS._execute_transition
async def _patched_dave_exec(self, transition_id):
    await _orig_dave_exec(self, transition_id)
    try:
        _ds = getattr(self, 'dave_session', None)
        if _ds is not None:
            _ok = _ds.execute_transition(int(transition_id))
            print(f"DAVE execute_transition id={transition_id} -> {_ok} status={_ds.status} ready={_ds.ready} can_encrypt={_ds.can_encrypt}")
    except Exception as _e:
        print(f"DAVE execute_transition failed: {_e!r}")
_VCS._execute_transition = _patched_dave_exec

# --- FIX DAVE (bổ sung) 15:00 PST 2026-11-06: server không phải lúc nào cũng
# gửi op 21 sau khi có người vào kênh — nó chỉ gửi op 29/30 (MLS commit /
# welcome, frame nhị phân). Theo davey docs, sau process_commit/process_welcome
# phải gọi prepare_transition thì session mới dùng được key mới. discord.py
# cũng không gọi ở đây. Patch thêm received_binary_message để gọi
# prepare_transition ngay sau khi handler gốc xử lý xong op 29/30.
_orig_dave_recv_bin = _DWS.received_binary_message
async def _patched_dave_recv_bin(self, msg):
    _op = None
    _tid = 0
    try:
        if isinstance(msg, (bytes, bytearray)) and len(msg) >= 5:
            _op = msg[2]
            _tid = int.from_bytes(msg[3:5], 'big')
    except Exception:
        _op = None
    await _orig_dave_recv_bin(self, msg)
    try:
        if _op in (_DWS.MLS_ANNOUNCE_COMMIT_TRANSITION, _DWS.MLS_WELCOME):
            _ds = getattr(self._connection, 'dave_session', None)
            if _ds is not None:
                _ds.prepare_transition(_tid, int(getattr(self._connection, 'dave_protocol_version', 1)))
                print(f"DAVE prepare_transition (bin op={_op}) id={_tid} status={_ds.status} ready={_ds.ready}")
    except Exception as _e:
        print(f"DAVE prepare_transition (bin) failed: {_e!r}")
_DWS.received_binary_message = _patched_dave_recv_bin


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
        vc = await _ensure_connected(guild)
        if vc is not None and not vc.is_playing() and not vc.is_paused():
            await _play_next(guild.id, vc)


@tasks.loop(minutes=5)
async def trend_loop():
    for guild in client.guilds:
        if guild.id not in trend_mode:
            continue
        vc = guild.voice_client
        if vc is not None and vc.is_connected() and not vc.is_playing() and not vc.is_paused():
            print(f"trend_loop: phát lại nhạc trend ở {guild.name}")
            await _play_next(guild.id, vc)


@tasks.loop(seconds=2)
async def karaoke_loop():
    """Karaoke tự chạy: sửa tin karaoke mỗi khi sang dòng lời mới."""
    for guild_id, st in list(karaoke_state.items()):
        guild = client.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if vc is None or not vc.is_connected():
            await _karaoke_stop(guild_id)
            continue
        if st.get("paused_at") is not None or not vc.is_playing():
            continue
        synced = st.get("synced") or []
        if not synced:
            continue
        elapsed = time.monotonic() - st["started_at"]
        idx = 0
        for i, (sec, _t) in enumerate(synced):
            if sec <= elapsed:
                idx = i
            else:
                break
        if idx == st.get("line_idx"):
            continue
        st["line_idx"] = idx
        msg = st.get("msg")
        if msg is None:
            continue
        try:
            await msg.edit(embed=_karaoke_embed(st["song"], synced, idx))
        except discord.DiscordException:
            pass


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
    if not karaoke_loop.is_running():
        karaoke_loop.start()
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
                    await asyncio.sleep(2)
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
}
FFMPEG_OPTS_FILE = {}
music_queues = {}  # guild_id -> list of (title, audio_url)
now_playing = {}  # guild_id -> song đang phát (để nút 📜 lấy lời)
karaoke_state = {}  # guild_id -> {"song", "synced", "started_at", "paused_at", "msg", "line_idx"}
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


def _clean_song_title(title):
    """Rút gọn tên bài để tìm lời: bỏ (feat...), [..], phần sau ' | ' và ' - '."""
    t = (title or "").strip()
    t = re.sub(r"\([^)]*\)", "", t)
    t = re.sub(r"\[[^\]]*\]", "", t)
    t = t.split("|")[0].split(" - ")[0]
    return t.strip(" -\u2013\u2014| \t")


def _norm_words(s):
    """Tách từ để so khớp nghệ sĩ: bỏ dấu, bỏ dấu câu, chữ thường."""
    s = (s or "").lower()
    s = s.replace("'", "").replace("’", "").replace('"', "").replace("“", "").replace("”", "")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.replace("đ", "d")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.split()


_LYRIC_STOPWORDS = {"say", "hi", "anh", "trai", "tinh", "ha", "official", "music", "channel", "records", "entertainment", "show", "the", "and", "feat", "ft", "lyrics", "lyric", "audio", "video", "mv", "remix", "remake", "cover", "version", "ver"}


def _version_num(s):
    """Số phiên bản trong tên bài ('(Version 2)', 'Ver 2', '(Bản 2)') hoặc None."""
    m = re.search(r"\bversion\s*(\d+)|ver\.?\s*(\d+)|bản\s*(\d+)", s or "", flags=re.I)
    if not m:
        return None
    return next((g for g in m.groups() if g), None)


def _performer_tokens_from_title(title):
    """Từ tên nghệ sĩ trong title: phần sau ' - ' và trong (feat. ...)/(cùng với ...)."""
    toks = set()
    t = (title or "")
    parts = t.split("|")[0].split(" - ")
    if len(parts) > 1:
        raw = " ".join(parts[1:])
        for chunk in re.split(r"[,/&+]|\bfeat\.?\b|\bft\.?\b|\bcùng với\b|\bx\b", raw, flags=re.I):
            for w in _norm_words(chunk):
                if len(w) > 2 and w not in _LYRIC_STOPWORDS:
                    toks.add(w)
    for m in re.finditer(r"\((?:feat\.?|ft\.?|cùng với)\s*([^)]*)\)", t, flags=re.I):
        for chunk in re.split(r"[,/&+]", m.group(1)):
            for w in _norm_words(chunk):
                if len(w) > 2 and w not in _LYRIC_STOPWORDS:
                    toks.add(w)
    return toks


def _performer_tokens_from_candidate(track_name, artist_name):
    """Từ tên nghệ sĩ của kết quả LRCLIB: artistName + phần (feat. ...) trong trackName."""
    toks = set()
    for w in _norm_words(artist_name):
        if len(w) > 2 and w not in _LYRIC_STOPWORDS:
            toks.add(w)
    for m in re.finditer(r"\((?:feat\.?|ft\.?|cùng với)\s*([^)]*)\)", track_name or "", flags=re.I):
        for chunk in re.split(r"[,/&+]", m.group(1)):
            for w in _norm_words(chunk):
                if len(w) > 2 and w not in _LYRIC_STOPWORDS:
                    toks.add(w)
    return toks


def _artist_tokens(title, artist):
    """Lấy các từ tên nghệ sĩ từ title ('Bài - Nghệ sĩ 1, Nghệ sĩ 2') + uploader."""
    toks = set()
    parts = (title or "").split("|")[0].split(" - ")
    if len(parts) > 1:
        raw = " ".join(parts[1:])
        for chunk in re.split(r"[,/&+]|\bfeat\.?\b|\bft\.?\b|\bx\b", raw, flags=re.I):
            for w in _norm_words(chunk):
                if len(w) > 2 and w not in _LYRIC_STOPWORDS:
                    toks.add(w)
    for w in _norm_words(artist):
        if len(w) > 2 and w not in _LYRIC_STOPWORDS:
            toks.add(w)
    return toks


def _parse_synced(synced_text):
    """Parse lời đồng bộ '[mm:ss.xx] dòng' -> [(giây, dòng)].
    Gộp các dòng cách nhau < 2s (kiểu word-sync) thành 1 dòng, tối đa ~80 ký tự."""
    lines = []
    for ln in (synced_text or "").splitlines():
        m = re.match(r"^\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)$", ln)
        if not m:
            continue
        text = m.group(3).strip()
        if text:
            lines.append((int(m.group(1)) * 60 + float(m.group(2)), text))
    if not lines:
        return []
    merged = [[lines[0][0], lines[0][1]]]
    for sec, text in lines[1:]:
        if sec - merged[-1][0] < 2.0 and len(merged[-1][1]) + len(text) + 1 <= 80:
            merged[-1][1] += " " + text
        else:
            merged.append([sec, text])
    return [(s, t) for s, t in merged]


def _parse_vtt(vtt):
    """Đổi file phụ đề WebVTT thành (lời thường, [(giây, dòng)]) để hát karaoke.
    Bỏ thẻ <c>/<v>, bỏ [Nhạc]/[Music], gộp các dòng trùng nhau liên tiếp."""
    lines = []
    cur_start = None
    buf = []

    def flush():
        nonlocal cur_start, buf
        if cur_start is None or not buf:
            return
        text = " ".join(buf)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\[[^\]]*\]", "", text)
        text = " ".join(text.split()).strip()
        if text:
            lines.append((cur_start, text))
        cur_start = None
        buf = []

    for raw in (vtt or "").splitlines():
        line = raw.strip()
        m = re.match(r"^(\d+):(\d+):(\d+)[.,](\d+)\s*-->", line)
        if m:
            flush()
            h, mi, s, ms = (int(x) for x in m.groups())
            cur_start = h * 3600 + mi * 60 + s + ms / 1000.0
            buf = []
            continue
        if not line or line.upper().startswith("WEBVTT") or line.upper().startswith("NOTE"):
            continue
        if re.match(r"^\d+$", line):
            continue
        if cur_start is not None:
            buf.append(line)
    flush()
    merged = []
    for t, text in lines:
        if merged and merged[-1][1] == text:
            continue
        merged.append((t, text))
    if not merged:
        return None, []
    return "\n".join(t for _, t in merged), merged


def _subtitles_from_youtube(webpage_url):
    """Lấy phụ đề tiếng Việt của video YouTube (phụ đề thật trước, tự động sau)
    để làm lời karaoke khi kho lời LRCLIB không có bài đó.
    Trả về (lời thường, [(giây, dòng)]) hoặc (None, [])."""
    if not webpage_url or ("youtube.com" not in webpage_url and "youtu.be" not in webpage_url):
        return None, []
    try:
        with yt_dlp.YoutubeDL(dict(YTDL_OPTS)) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
    except Exception as e:
        print("subtitle info failed:", e)
        return None, []
    tracks = (info or {}).get("subtitles") or {}
    auto = (info or {}).get("automatic_captions") or {}
    vi = tracks.get("vi") or auto.get("vi") or []
    vtt_url = None
    for f in vi:
        if f.get("ext") == "vtt" and f.get("url"):
            vtt_url = f["url"]
            break
    if not vtt_url and vi:
        vtt_url = vi[0].get("url")
    if not vtt_url:
        return None, []
    try:
        req = urllib.request.Request(vtt_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            vtt = resp.read().decode("utf-8", "replace")
    except Exception as e:
        print("subtitle download failed:", e)
        return None, []
    return _parse_vtt(vtt)


def _lrclib_search(track_name, artist_name=None):
    params = {"track_name": track_name}
    if artist_name:
        params["artist_name"] = artist_name
    url = "https://lrclib.net/api/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "ZoohoyoBot/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return data if isinstance(data, list) else []


def _artist_hints(title, artist):
    """Các tên nghệ sĩ/show để thử tìm kèm: uploader trước, rồi phần sau ' | ' trong title."""
    hints = []
    if artist and artist.strip():
        hints.append(artist.strip())
    parts = (title or "").split("|")
    if len(parts) > 1:
        show = parts[1].split("-")[0]
        show = re.sub(r"\([^)]*\)", "", show)
        show = re.sub(r"\[[^\]]*\]", "", show).strip()
        if show and show.lower() not in [h.lower() for h in hints]:
            hints.append(show)
    return hints


def _pick_lyrics(results, artist, duration, want_toks, title_perfs, title_ver):
    """Chấm điểm + lọc các kết quả LRCLIB, trả về (lời, synced_raw, điểm) tốt nhất."""
    artist_low = (artist or "").lower()
    best = None
    best_synced_raw = ""
    best_score = -1
    for r in results:
        # CHẶN LỜI SAI: khác phiên bản (Version 2 vs bản gốc) hoặc khác hẳn nghệ sĩ
        # thì bỏ qua kết quả này — thà không có lời còn hơn hiện lời sai.
        if title_ver is not None and _version_num(r.get("trackName") or "") != title_ver:
            continue
        if title_perfs:
            cand_perfs = _performer_tokens_from_candidate(r.get("trackName") or "", r.get("artistName") or "")
            if cand_perfs and title_perfs.isdisjoint(cand_perfs):
                continue
        lyrics = (r.get("plainLyrics") or "").strip()
        synced_raw = (r.get("syncedLyrics") or "").strip()
        if not lyrics and synced_raw:
            lyrics = "\n".join(re.sub(r"^\[\d+:\d+(?:\.\d+)?\]\s*", "", ln) for ln in synced_raw.splitlines()).strip()
        if not lyrics or r.get("instrumental"):
            continue
        score = 0
        dur = r.get("duration") or 0
        if duration and dur:
            if abs(dur - duration) <= 10:
                score += 2
            elif abs(dur - duration) <= 30:
                score += 1
        an = (r.get("artistName") or "").lower()
        if artist_low and an and (artist_low in an or an in artist_low):
            score += 2
        if want_toks:
            overlap = len(want_toks & set(_norm_words(r.get("artistName") or "")))
            score += min(6, 2 * overlap)
        if score > best_score:
            best_score = score
            best = lyrics
            best_synced_raw = synced_raw
    return best, best_synced_raw, best_score


def _lyrics_from_lrclib(title, artist, duration):
    """Tìm lời bài hát trên LRCLIB (miễn phí, không cần key).
    Trả về (lời thường, [(giây, dòng)] lời đồng bộ) hoặc (None, [])."""
    name = _clean_song_title(title)
    if not name:
        return None, []
    try:
        results = _lrclib_search(name)
    except Exception as e:
        print("lyrics search failed:", e)
        return None, []
    want_toks = _artist_tokens(title, artist)
    title_perfs = _performer_tokens_from_title(title)
    title_ver = _version_num(title)
    best, best_synced_raw, best_score = _pick_lyrics(results, artist, duration, want_toks, title_perfs, title_ver)
    # Nếu chưa khớp chắc chắn, thử tìm kèm tên nghệ sĩ (hữu ích với tên bài
    # chung chung như SECRET — tìm theo tên bài không thì toàn ra bài nước ngoài).
    if best is None or best_score < 3:
        for hint in _artist_hints(title, artist):
            try:
                results2 = _lrclib_search(name, hint)
            except Exception as e:
                print("lyrics artist search failed:", e)
                continue
            b2, s2, sc2 = _pick_lyrics(results2, artist, duration, want_toks, title_perfs, title_ver)
            if b2 is not None and sc2 > best_score:
                best, best_synced_raw, best_score = b2, s2, sc2
            if best_score >= 6:
                break
    if best is None:
        return None, []
    return best.strip(), _parse_synced(best_synced_raw)


async def _fetch_lyrics(song):
    """Lấy lời bài hát (nếu có) rồi lưu vào song['lyrics'] và song['synced']. Lỗi thì thôi, không chặn nhạc."""
    if "lyrics" in song:
        return song.get("lyrics")
    try:
        plain, synced = await asyncio.wait_for(
            asyncio.to_thread(_lyrics_from_lrclib, song.get("title", ""), song.get("artist", ""), song.get("duration") or 0),
            timeout=12,
        )
    except Exception as e:
        print("lyrics fetch failed:", e)
        plain, synced = None, []
    song["lyrics"] = plain
    song["synced"] = synced
    return plain


async def _fetch_subtitles(song):
    """Dự phòng khi LRCLIB không có lời: lấy phụ đề tiếng Việt của video YouTube
    làm lời karaoke. Chỉ chạy khi bài chưa có lời đồng bộ. Lỗi thì thôi."""
    if song.get("synced"):
        return song.get("lyrics")
    url = song.get("webpage_url") or ""
    if not url:
        return None
    try:
        plain, synced = await asyncio.wait_for(
            asyncio.to_thread(_subtitles_from_youtube, url),
            timeout=20,
        )
    except Exception as e:
        print("subtitle fetch failed:", e)
        return None
    if plain:
        song["lyrics"] = plain
        song["synced"] = synced
    return plain


def _karaoke_embed(song, synced, idx):
    """Thẻ karaoke: dòng trước + dòng đang hát (in đậm) + 2 dòng sau."""
    embed = discord.Embed(title=f"🎤 {song['title']}", color=0xFA57C1)
    parts = []
    for j in range(max(0, idx - 1), min(len(synced), idx + 3)):
        line = synced[j][1]
        parts.append(f"**🎵 {line}**" if j == idx else line)
    embed.description = "\n".join(parts) if parts else "…"
    embed.set_footer(text=f"🎤 {song.get('artist', '')}  •  Karaoke tự chạy")
    return embed


async def _karaoke_start(guild_id, song, started_at):
    """Bắt đầu karaoke cho bài mới (hoặc xoá tin karaoke nếu bài không có lời đồng bộ)."""
    synced = song.get("synced") or []
    st = karaoke_state.get(guild_id)
    if not synced:
        if st and st.get("msg") is not None:
            try:
                await st["msg"].delete()
            except discord.DiscordException:
                pass
        karaoke_state.pop(guild_id, None)
        return
    guild = client.get_guild(guild_id)
    ch = None
    if guild is not None:
        # Karaoke luôn ở kênh order-nhạc (nơi An nhìn), dù /nhac gõ ở kênh khác.
        ch = find_music_text_channel(guild)
        if ch is None:
            ch_id = music_text_channels.get(guild_id)
            ch = guild.get_channel(ch_id) if ch_id else None
        if ch is not None:
            music_text_channels[guild_id] = ch.id
    if ch is None:
        karaoke_state.pop(guild_id, None)
        return
    new_state = {
        "song": song,
        "synced": synced,
        "started_at": started_at,
        "paused_at": None,
        "line_idx": 0,
        "msg": st.get("msg") if st else None,
    }
    karaoke_state[guild_id] = new_state
    try:
        if new_state["msg"] is not None:
            await new_state["msg"].edit(embed=_karaoke_embed(song, synced, 0))
        else:
            new_state["msg"] = await ch.send(embed=_karaoke_embed(song, synced, 0))
    except discord.DiscordException as e:
        print("karaoke send failed:", e)
        karaoke_state.pop(guild_id, None)


async def _karaoke_stop(guild_id):
    """Dừng karaoke: xoá tin karaoke và trạng thái."""
    st = karaoke_state.pop(guild_id, None)
    if st and st.get("msg") is not None:
        try:
            await st["msg"].delete()
        except discord.DiscordException:
            pass


def _karaoke_pause(guild_id, paused):
    """Tạm dừng / phát tiếp karaoke cho khớp với nhạc (giữ nguyên dòng đang hát khi pause)."""
    st = karaoke_state.get(guild_id)
    if not st:
        return
    now = time.monotonic()
    if paused:
        if st.get("paused_at") is None:
            st["paused_at"] = now
    else:
        if st.get("paused_at") is not None:
            st["started_at"] += now - st["paused_at"]
            st["paused_at"] = None


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
    if song.get("lyrics"):
        preview = song["lyrics"].strip()
        if len(preview) > 990:
            preview = preview[:990] + "\n…"
        embed.add_field(name="📜 Lời bài hát", value=preview, inline=False)
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
                _karaoke_pause(self.guild_id, True)
                button.emoji = "▶️"
            elif vc.is_paused():
                vc.resume()
                _karaoke_pause(self.guild_id, False)
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

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary)
    async def lyrics(self, interaction: discord.Interaction, button: discord.ui.Button):
        song = now_playing.get(self.guild_id)
        text = ((song or {}).get("lyrics") or "").strip()
        if not text:
            await interaction.response.send_message("Bài này chưa tìm thấy lời 😢", ephemeral=True)
            return
        chunks = []
        while text:
            if len(text) <= 1900:
                chunks.append(text)
                break
            cut = text.rfind("\n\n", 0, 1900)
            if cut < 500:
                cut = text.rfind("\n", 0, 1900)
            if cut < 500:
                cut = 1900
            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for c in chunks[1:]:
            await interaction.followup.send(c, ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc:
            music_queues[interaction.guild_id] = []
            trend_mode.discard(interaction.guild_id)
            await _karaoke_stop(interaction.guild_id)
            await vc.disconnect()
        await interaction.response.defer()


def find_music_text_channel(guild):
    for ch in guild.text_channels:
        if "order-nhạc" in ch.name or "order-nhac" in ch.name:
            return ch
    return None


async def _ensure_connected(guild):
    """Đảm bảo bot đang ở trong voice. Rớt ra thì vô lại kênh trend."""
    vc = guild.voice_client
    if vc is not None and vc.is_connected():
        return vc
    if vc is not None:
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
    for ch in guild.voice_channels:
        if TREND_CHANNEL_NAME.lower() in ch.name.strip().lower():
            try:
                vc = await ch.connect()
                print(f"đã vô lại kênh trend: {ch.name}")
                return vc
            except Exception as e:
                print("reconnect voice failed:", e)
                return None
    return None


play_locks = {}

async def _play_next(guild_id, voice_client):
    lock = play_locks.setdefault(guild_id, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        await _play_next_locked(guild_id, voice_client)


async def _play_next_locked(guild_id, voice_client):
    guild = client.get_guild(guild_id)
    if guild is not None:
        vc = await _ensure_connected(guild)
        if vc is None:
            return
        voice_client = vc
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
                source = discord.FFmpegOpusAudio(local, **FFMPEG_OPTS_FILE)
            else:
                source = discord.FFmpegOpusAudio(song["url"], **FFMPEG_OPTS_URL)
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
        play_started = time.monotonic()
        g = client.get_guild(guild_id)
        vs = g.me.voice if g and g.me else None
        _sock = getattr(voice_client._connection, "socket", None)
        _ds = voice_client._connection.dave_session
        print(f"Đang phát: {song['title']} | connected={voice_client.is_connected()} playing={voice_client.is_playing()} latency={voice_client.latency:.3f} mode={voice_client.mode} dave_proto={voice_client._connection.dave_protocol_version} dave_ready={_ds.ready if _ds else None} can_encrypt={voice_client._connection.can_encrypt} sock_fileno={_sock.fileno() if _sock is not None and _sock is not discord.utils.MISSING else None} endpoint={voice_client.endpoint} | voice state: mute={vs.mute if vs else None} deaf={vs.deaf if vs else None} self_mute={vs.self_mute if vs else None} self_deaf={vs.self_deaf if vs else None} suppress={vs.suppress if vs else None} channel={vs.channel.name if vs and vs.channel else None}")
        await _fetch_lyrics(song)
        # Bài nào kho lời không có thì lấy phụ đề video làm lời karaoke.
        await _fetch_subtitles(song)
        now_playing[guild_id] = song
        await _karaoke_start(guild_id, song, play_started)
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
    await _fetch_lyrics(song)
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
    # /nhac là "phát ngay": chèn bài lên đầu hàng chờ rồi dừng bài đang phát,
    # callback after sẽ phát bài mới liền (không đợi hết bài cũ).
    music_queues.setdefault(interaction.guild_id, []).insert(0, song)
    if not vc.is_playing() and not vc.is_paused():
        await _play_next(interaction.guild_id, vc)
        await interaction.followup.send(embed=_now_playing_embed(song), view=MusicControls(interaction.guild_id))
    else:
        vc.stop()
        await interaction.followup.send(embed=_now_playing_embed(song).add_field(name="Đang chuyển bài 🎵", value="Phát bài này ngay nha", inline=False), view=MusicControls(interaction.guild_id))

@tree.command(name="dung", description="Tạm dừng nhạc")
async def dung(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        _karaoke_pause(interaction.guild_id, True)
        await interaction.response.send_message("Tạm dừng nhạc rồi ⏸️")
    else:
        await interaction.response.send_message("Đâu có nhạc đang phát đâu 😅", ephemeral=True)

@tree.command(name="tiep", description="Phát tiếp nhạc đang tạm dừng")
async def tiep(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        _karaoke_pause(interaction.guild_id, False)
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

@tree.command(name="kiemtra", description="Kiểm tra bot có đang phát nhạc và có quyền nói trong voice không")
async def kiemtra(interaction: discord.Interaction):
    guild = interaction.guild
    vc = guild.voice_client if guild else None
    me = guild.me if guild else None
    vs = me.voice if me else None
    lines = []
    if vc is None or vs is None or vs.channel is None:
        lines.append("Bot hiện không ở trong voice channel nào.")
    else:
        ch = vs.channel
        perms = ch.permissions_for(me)
        lines.append(f"Bot đang ở kênh: **{ch.name}**")
        lines.append(f"Đang phát nhạc: {'có' if vc.is_playing() else 'không'} | đang tạm dừng: {'có' if vc.is_paused() else 'không'}")
        lines.append(f"Quyền: kết nối={'có' if perms.connect else 'không'}, nói={'có' if perms.speak else 'không'}, dùng voice activity={'có' if perms.use_voice_activation else 'không'}")
        lines.append(f"Trạng thái voice: bị tắt tiếng={'có' if vs.mute else 'không'}, bị điếc={'có' if vs.deaf else 'không'}, tự tắt tiếng={'có' if vs.self_mute else 'không'}, bị chặn nói={'có' if vs.suppress else 'không'}")
        if not perms.speak or vs.suppress or vs.mute:
            lines.append("⚠️ Bot bị chặn nói nên An không nghe được nhạc. An cho bot quyền **Nói** trong kênh voice này nha (cài đặt kênh → Quyền → Bot tuất → bật Nói).")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


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
        await _karaoke_stop(interaction.guild_id)
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
