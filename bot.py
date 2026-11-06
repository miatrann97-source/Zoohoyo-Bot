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

import json
import os
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
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


# --- Bot ---

intents = discord.Intents.default()
intents.members = True  # để đếm thành viên chính xác
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


@client.event
async def on_ready():
    print(f"Đã đăng nhập: {client.user} (id: {client.user.id})")
    await tree.sync()
    await update_board_once()
    if not update_board_loop.is_running():
        update_board_loop.start()


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
