import os
import asyncio
import logging
import random
import time
import datetime
import json
import re
import io
from collections import defaultdict
from openai import OpenAI
import discord
from discord import app_commands
from discord.ext import commands
import requests as req
from urllib.parse import quote
import cloudscraper
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
MODEL = "openai/gpt-oss-120b:free"
MAX_HISTORY = 10

client_ai = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

GENIUS_TOKEN = os.environ["GENIUS_ACCESS_TOKEN"]
GENIUS_HEADERS = {"Authorization": f"Bearer {GENIUS_TOKEN}"}

conversation_history: dict[int, list[dict]] = defaultdict(list)
START_TIME = time.time()

# ── Economy storage ───────────────────────────────────────────────────────────

ECONOMY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "economy.json")

_ECO_DEFAULT_SETTINGS = {
    "daily_min": 80, "daily_max": 150,
    "work_min": 50,  "work_max": 120,
    "work_cooldown": 3600,
    "rob_chance": 40, "rob_fine": 30,
}
_ECO_USER_DEFAULT = {
    "coins": 0, "last_daily": 0, "last_work": 0, "last_rob": 0,
    "badges": [], "total_earned": 0, "total_lost": 0,
}

def eco_load() -> dict:
    if os.path.exists(ECONOMY_FILE):
        try:
            with open(ECONOMY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def eco_save(data: dict) -> None:
    with open(ECONOMY_FILE, "w") as f:
        json.dump(data, f, indent=2)

def eco_settings() -> dict:
    return eco_load().get("__settings__", dict(_ECO_DEFAULT_SETTINGS))

def eco_save_settings(s: dict) -> None:
    data = eco_load()
    data["__settings__"] = s
    eco_save(data)

def eco_get(user_id: int) -> dict:
    data = eco_load()
    uid = str(user_id)
    if uid not in data:
        data[uid] = dict(_ECO_USER_DEFAULT)
        eco_save(data)
    else:
        # backfill new keys for older users
        for k, v in _ECO_USER_DEFAULT.items():
            data[uid].setdefault(k, v)
    return data[uid]

def eco_update(user_id: int, **kwargs) -> dict:
    """Update specific fields of a user's economy record."""
    data = eco_load()
    uid = str(user_id)
    if uid not in data:
        data[uid] = dict(_ECO_USER_DEFAULT)
    for k, v in kwargs.items():
        data[uid][k] = v
    eco_save(data)
    return data[uid]

def eco_add(user_id: int, amount: int) -> int:
    data = eco_load()
    uid = str(user_id)
    if uid not in data:
        data[uid] = dict(_ECO_USER_DEFAULT)
    old = data[uid].get("coins", 0)
    data[uid]["coins"] = max(0, old + amount)
    if amount > 0:
        data[uid]["total_earned"] = data[uid].get("total_earned", 0) + amount
    else:
        data[uid]["total_lost"] = data[uid].get("total_lost", 0) + abs(amount)
    eco_save(data)
    return data[uid]["coins"]

# Shop items: name → (cost, emoji, description)
SHOP_ITEMS: dict[str, tuple[int, str, str]] = {
    "VIP":       (500,   "🌟", "Shiny VIP role"),
    "Diamond":   (1000,  "💎", "Sparkling Diamond role"),
    "King":      (2500,  "👑", "The almighty King/Queen role"),
    "Legend":    (5000,  "🔥", "Legendary status — flex on everyone"),
}

def parse_duration(s: str) -> int | None:
    """Parse '30m', '2h', '1d' etc. → seconds. Returns None if invalid."""
    m = re.fullmatch(r"(\d+)\s*(s|sec|m|min|h|hr|d|day)s?", s.strip().lower())
    if not m:
        return None
    val, unit = int(m.group(1)), m.group(2)[0]
    return val * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]

BOT_OWNER_ID: int | None = None


# ── AI helpers ────────────────────────────────────────────────────────────────

def trim_history(channel_id: int) -> None:
    history = conversation_history[channel_id]
    if len(history) > MAX_HISTORY * 2:
        conversation_history[channel_id] = history[-(MAX_HISTORY * 2):]


def ask_ai_once(prompt: str, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            response = client_ai.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": get_system_prompt()},
                    {"role": "user", "content": prompt},
                ],
                extra_body={"reasoning": {"enabled": True}},
                timeout=30,
            )
            return response.choices[0].message.content or ""
        except Exception as exc:
            log.warning("ask_ai_once attempt %d failed: %s", attempt + 1, exc)
            if attempt < retries - 1:
                time.sleep(2)
    return ""


def ask_ai(channel_id: int, user_message: str, retries: int = 3) -> str:
    history = conversation_history[channel_id]
    history.append({"role": "user", "content": user_message})
    trim_history(channel_id)
    messages = [{"role": "system", "content": get_system_prompt()}] + history

    for attempt in range(retries):
        try:
            response = client_ai.chat.completions.create(
                model=MODEL,
                messages=messages,
                extra_body={"reasoning": {"enabled": True}},
                timeout=30,
            )
            assistant_msg = response.choices[0].message
            reply_text = assistant_msg.content or ""
            history.append({
                "role": "assistant",
                "content": assistant_msg.content,
                "reasoning_details": assistant_msg.reasoning_details,
            })
            return reply_text
        except Exception as exc:
            log.warning("ask_ai attempt %d failed: %s", attempt + 1, exc)
            if attempt < retries - 1:
                time.sleep(2)
    return ""


async def send_long(interaction: discord.Interaction, text: str) -> None:
    if len(text) <= 2000:
        await interaction.followup.send(text)
    else:
        for i in range(0, len(text), 2000):
            await interaction.followup.send(text[i:i + 2000])


async def reply_long(message: discord.Message, text: str) -> None:
    if len(text) <= 2000:
        await message.reply(text)
    else:
        first = True
        for i in range(0, len(text), 2000):
            if first:
                await message.reply(text[i:i + 2000])
                first = False
            else:
                await message.channel.send(text[i:i + 2000])


# ── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="b ", intents=intents, help_command=None)

AUTO_REPLY_CHANNEL_ID: int = 1492160189489741858


@bot.event
async def on_ready():
    global BOT_OWNER_ID
    info = await bot.application_info()
    BOT_OWNER_ID = info.owner.id
    for guild in bot.guilds:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync()
    log.info("Logged in as %s (ID: %s) — synced to %d guild(s)! Owner: %d",
             bot.user, bot.user.id, len(bot.guilds), BOT_OWNER_ID)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)

    mentioned = bot.user in message.mentions
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_auto_channel = message.channel.id == AUTO_REPLY_CHANNEL_ID

    if not (mentioned or is_dm or is_auto_channel):
        return

    content = message.content
    for m in message.mentions:
        content = content.replace(f"<@{m.id}>", "").replace(f"<@!{m.id}>", "")
    content = content.strip()

    if not content:
        if not is_auto_channel:
            await message.reply("Hey! How can I help you? Try `/help` to see all commands.")
        return

    async with message.channel.typing():
        try:
            reply = await asyncio.get_event_loop().run_in_executor(
                None, ask_ai, message.channel.id, content
            )
        except Exception as exc:
            log.exception("AI error: %s", exc)
            await message.reply("Something went wrong. Please try again.")
            return

    if not reply:
        await message.reply("Got an empty response. Please try again.")
        return

    await reply_long(message, reply)


# ── /admin (admin only) ───────────────────────────────────────────────────────

admin_group = app_commands.Group(
    name="admin",
    description="Admin-only bot management commands",
    default_permissions=discord.Permissions(administrator=True),
)


@admin_group.command(name="status", description="Show bot status and current settings")
async def admin_status(interaction: discord.Interaction):
    uptime_secs = int(time.time() - START_TIME)
    hours, rem = divmod(uptime_secs, 3600)
    minutes, seconds = divmod(rem, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"

    total_history = sum(len(v) for v in conversation_history.values())
    label = RAGE_LABELS[RAGE_LEVEL]
    bar = "🟥" * RAGE_LEVEL + "⬛" * (10 - RAGE_LEVEL)

    embed = discord.Embed(title="⚙️ Bot Status", color=discord.Color.orange())
    embed.add_field(name="Uptime", value=uptime_str, inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Guilds", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Stored Messages", value=str(total_history), inline=True)
    embed.add_field(name="Auto-Reply Channel", value=f"<#{AUTO_REPLY_CHANNEL_ID}>", inline=True)
    embed.add_field(name="Model", value=f"`{MODEL}`", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@admin_group.command(name="clearall", description="Clear ALL conversation history across every channel")
async def admin_clearall(interaction: discord.Interaction):
    count = len(conversation_history)
    conversation_history.clear()
    await interaction.response.send_message(
        f"🧹 Cleared conversation history from **{count}** channel(s).",
        ephemeral=False
    )
    log.info("All conversation history cleared by %s", interaction.user)


@admin_group.command(name="setchannel", description="Change the auto-reply channel")
@app_commands.describe(channel="The channel where the bot auto-replies to every message")
async def admin_setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    global AUTO_REPLY_CHANNEL_ID
    AUTO_REPLY_CHANNEL_ID = channel.id
    await interaction.response.send_message(
        f"✅ Auto-reply channel set to {channel.mention}",
        ephemeral=False
    )
    log.info("Auto-reply channel changed to %d by %s", channel.id, interaction.user)


@admin_group.command(name="say", description="Make the bot say something in a channel")
@app_commands.describe(channel="Target channel", message="What to say")
async def admin_say(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    await channel.send(message)
    await interaction.response.send_message(f"✅ Sent to {channel.mention}", ephemeral=True)


@admin_group.command(name="announce", description="Send a fancy announcement embed to a channel")
@app_commands.describe(
    channel="Target channel",
    title="Announcement title",
    message="Announcement body",
    ping="Ping @everyone? (default: no)",
    color="Embed color: red, green, blue, gold, purple (default: gold)",
)
async def admin_announce(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str,
    message: str,
    ping: bool = False,
    color: str = "gold",
):
    colors = {
        "red": discord.Color.red(), "green": discord.Color.green(),
        "blue": discord.Color.blue(), "gold": discord.Color.gold(),
        "purple": discord.Color.purple(), "orange": discord.Color.orange(),
        "pink": discord.Color.magenta(),
    }
    embed = discord.Embed(
        title=f"📢 {title}",
        description=message,
        color=colors.get(color.lower(), discord.Color.gold()),
        timestamp=datetime.datetime.utcnow(),
    )
    embed.set_footer(text=f"Announced by {interaction.user.display_name}")
    content = "@everyone" if ping else None
    await channel.send(content=content, embed=embed)
    await interaction.response.send_message(f"✅ Announcement sent to {channel.mention}!", ephemeral=True)


@admin_group.command(name="poll", description="Create a poll with up to 4 options")
@app_commands.describe(
    question="The poll question",
    option1="First option", option2="Second option",
    option3="Third option (optional)", option4="Fourth option (optional)",
)
async def admin_poll(
    interaction: discord.Interaction,
    question: str,
    option1: str,
    option2: str,
    option3: str = "",
    option4: str = "",
):
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
    options = [o for o in [option1, option2, option3, option4] if o]
    desc = "\n".join(f"{emojis[i]} {opt}" for i, opt in enumerate(options))
    embed = discord.Embed(
        title=f"📊 {question}",
        description=desc,
        color=discord.Color.blurple(),
        timestamp=datetime.datetime.utcnow(),
    )
    embed.set_footer(text=f"Poll by {interaction.user.display_name} • React to vote!")
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    for i in range(len(options)):
        await msg.add_reaction(emojis[i])


@admin_group.command(name="purge", description="Delete the last X messages in this channel")
@app_commands.describe(amount="Number of messages to delete (max 100)")
async def admin_purge(interaction: discord.Interaction, amount: int):
    amount = min(max(amount, 1), 100)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"🗑️ Deleted **{len(deleted)}** messages.", ephemeral=True)
    log.info("%s purged %d messages in #%s", interaction.user, len(deleted), interaction.channel)


@admin_group.command(name="mute", description="Timeout (mute) a user for X minutes")
@app_commands.describe(user="User to mute", minutes="Duration in minutes (max 40320 = 28 days)", reason="Reason")
async def admin_mute(interaction: discord.Interaction, user: discord.Member, minutes: int = 10, reason: str = "No reason given"):
    minutes = min(max(minutes, 1), 40320)
    duration = datetime.timedelta(minutes=minutes)
    await user.timeout(duration, reason=f"{reason} (by {interaction.user})")
    await interaction.response.send_message(
        f"🔇 **{user.display_name}** has been muted for **{minutes} minute(s)**.\n📝 Reason: {reason}"
    )
    log.info("%s muted %s for %d minutes", interaction.user, user, minutes)


@admin_group.command(name="unmute", description="Remove timeout from a user")
@app_commands.describe(user="User to unmute")
async def admin_unmute(interaction: discord.Interaction, user: discord.Member):
    await user.timeout(None)
    await interaction.response.send_message(f"🔊 **{user.display_name}** has been unmuted.")


@admin_group.command(name="nickname", description="Change a user's nickname")
@app_commands.describe(user="Target user", nickname="New nickname (leave empty to reset)")
async def admin_nickname(interaction: discord.Interaction, user: discord.Member, nickname: str = ""):
    old = user.display_name
    await user.edit(nick=nickname or None)
    if nickname:
        await interaction.response.send_message(f"✏️ Changed **{old}**'s nickname to **{nickname}**")
    else:
        await interaction.response.send_message(f"✏️ Reset **{old}**'s nickname")


@admin_group.command(name="giveaway", description="Start a giveaway in a channel")
@app_commands.describe(
    channel="Channel to post the giveaway",
    prize="What are you giving away?",
    minutes="How many minutes until it ends",
)
async def admin_giveaway(interaction: discord.Interaction, channel: discord.TextChannel, prize: str, minutes: int = 60):
    minutes = min(max(minutes, 1), 10080)
    end_time = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
    embed = discord.Embed(
        title="🎉 GIVEAWAY 🎉",
        description=f"**Prize:** {prize}\n\nReact with 🎉 to enter!\n\n**Ends:** <t:{int(end_time.timestamp())}:R>",
        color=discord.Color.gold(),
        timestamp=end_time,
    )
    embed.set_footer(text=f"Hosted by {interaction.user.display_name} • Ends at")
    msg = await channel.send(embed=embed)
    await msg.add_reaction("🎉")
    await interaction.response.send_message(f"✅ Giveaway started in {channel.mention}!", ephemeral=True)

    # Wait and pick a winner
    async def end_giveaway():
        await asyncio.sleep(minutes * 60)
        try:
            msg_updated = await channel.fetch_message(msg.id)
            reaction = discord.utils.get(msg_updated.reactions, emoji="🎉")
            if reaction:
                users = [u async for u in reaction.users() if not u.bot]
                if users:
                    winner = random.choice(users)
                    await channel.send(f"🎉 Congratulations {winner.mention}! You won **{prize}**!")
                else:
                    await channel.send(f"😢 No one entered the giveaway for **{prize}**.")
        except Exception as e:
            log.exception("Giveaway end error: %s", e)

    asyncio.create_task(end_giveaway())


@admin_group.command(name="embed", description="Send a custom embed message to a channel")
@app_commands.describe(
    channel="Target channel",
    title="Embed title",
    description="Embed body text",
    color="Color: red, green, blue, gold, purple, orange, pink (default: blue)",
    footer="Optional footer text",
)
async def admin_embed(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str,
    description: str,
    color: str = "blue",
    footer: str = "",
):
    colors = {
        "red": discord.Color.red(), "green": discord.Color.green(),
        "blue": discord.Color.blue(), "gold": discord.Color.gold(),
        "purple": discord.Color.purple(), "orange": discord.Color.orange(),
        "pink": discord.Color.magenta(),
    }
    embed = discord.Embed(
        title=title,
        description=description,
        color=colors.get(color.lower(), discord.Color.blue()),
    )
    if footer:
        embed.set_footer(text=footer)
    await channel.send(embed=embed)
    await interaction.response.send_message(f"✅ Embed sent to {channel.mention}!", ephemeral=True)


@admin_group.command(name="roastall", description="Make the AI roast everyone currently in the server 🔥")
async def admin_roastall(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    members = [m for m in interaction.guild.members if not m.bot][:10]
    names = ", ".join(m.display_name for m in members)
    prompt = (
        f"Give a short, hilarious group roast for these Discord server members: {names}. "
        "Keep it playful and funny — roast the whole group together in 3-5 sentences."
    )
    try:
        reply = await asyncio.get_event_loop().run_in_executor(None, ask_ai_once, prompt)
    except Exception:
        await interaction.followup.send("Couldn't come up with a roast!")
        return
    await interaction.followup.send(f"🔥 **Server Roast!**\n\n{reply}")


@admin_group.command(name="quiz", description="Drop an AI-generated quiz question in a channel")
@app_commands.describe(channel="Channel to send the quiz", topic="Topic for the quiz question (optional)")
async def admin_quiz(interaction: discord.Interaction, channel: discord.TextChannel, topic: str = "random"):
    await interaction.response.defer(ephemeral=True)
    prompt = (
        f"Create one fun multiple-choice quiz question about {topic}. "
        "Give 4 options labeled A B C D. Do NOT reveal the answer yet — "
        "just end with '||Answer: X||' using Discord spoiler tags so people have to click to see."
    )
    try:
        reply = await asyncio.get_event_loop().run_in_executor(None, ask_ai_once, prompt)
    except Exception:
        await interaction.followup.send("Couldn't generate a quiz!", ephemeral=True)
        return
    await channel.send(f"🧠 **Quiz Time!**\n\n{reply}")
    await interaction.followup.send(f"✅ Quiz posted in {channel.mention}!", ephemeral=True)


@admin_group.command(name="slowmode", description="Set slowmode in a channel")
@app_commands.describe(channel="Target channel", seconds="Delay in seconds (0 to disable, max 21600)")
async def admin_slowmode(interaction: discord.Interaction, channel: discord.TextChannel, seconds: int):
    seconds = min(max(seconds, 0), 21600)
    await channel.edit(slowmode_delay=seconds)
    if seconds == 0:
        await interaction.response.send_message(f"✅ Slowmode disabled in {channel.mention}")
    else:
        await interaction.response.send_message(f"🐢 Slowmode set to **{seconds}s** in {channel.mention}")


bot.tree.add_command(admin_group)


# ── Error handler for admin commands ─────────────────────────────────────────

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "🚫 You need **Administrator** permission to use this command.",
            ephemeral=True
        )
    else:
        log.exception("Slash command error: %s", error)
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)


# ── /chat ─────────────────────────────────────────────────────────────────────

@bot.tree.command(name="chat", description="Chat with the AI assistant")
@app_commands.describe(message="Your message to the AI")
async def slash_chat(interaction: discord.Interaction, message: str):
    await interaction.response.defer(thinking=True)
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, ask_ai, interaction.channel_id, message
        )
    except Exception as exc:
        log.exception("AI error: %s", exc)
        await interaction.followup.send("Something went wrong. Please try again.")
        return
    await send_long(interaction, reply or "Got an empty response.")


# ── /reset ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="reset", description="Clear AI conversation history in this channel")
async def slash_reset(interaction: discord.Interaction):
    conversation_history[interaction.channel_id].clear()
    await interaction.response.send_message("🧹 Conversation history cleared!")


# ── /ping ─────────────────────────────────────────────────────────────────────

@bot.tree.command(name="ping", description="Check the bot's latency")
async def slash_ping(interaction: discord.Interaction):
    ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! Latency: **{ms}ms**")


# ── /joke ─────────────────────────────────────────────────────────────────────

@bot.tree.command(name="joke", description="Get a funny joke from the AI")
@app_commands.describe(topic="Optional topic (e.g. cats, programming, food)")
async def slash_joke(interaction: discord.Interaction, topic: str = "anything"):
    await interaction.response.defer(thinking=True)
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, ask_ai_once, f"Tell me a single short, funny joke about {topic}. Just the joke, no intro."
        )
    except Exception:
        await interaction.followup.send("Couldn't think of a joke right now!")
        return
    await interaction.followup.send(f"😂 {reply}")


# ── /roast ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="roast", description="Roast someone (all in good fun!)")
@app_commands.describe(user="The user to roast")
async def slash_roast(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(thinking=True)
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, ask_ai_once,
            f"Give a short, playful, funny roast for someone named {user.display_name}. Light-hearted, max 2 sentences."
        )
    except Exception:
        await interaction.followup.send("Couldn't come up with a roast!")
        return
    await interaction.followup.send(f"🔥 {user.mention} {reply}")


# ── /compliment ───────────────────────────────────────────────────────────────

@bot.tree.command(name="compliment", description="Give someone a nice compliment")
@app_commands.describe(user="The user to compliment")
async def slash_compliment(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(thinking=True)
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, ask_ai_once,
            f"Give a genuine, warm, creative compliment for someone named {user.display_name}. Max 2 sentences."
        )
    except Exception:
        await interaction.followup.send("Couldn't come up with a compliment!")
        return
    await interaction.followup.send(f"💖 {user.mention} {reply}")


# ── /8ball ────────────────────────────────────────────────────────────────────

EIGHT_BALL = [
    "It is certain.", "It is decidedly so.", "Without a doubt.",
    "Yes, definitely.", "You may rely on it.", "As I see it, yes.",
    "Most likely.", "Outlook good.", "Yes.", "Signs point to yes.",
    "Reply hazy, try again.", "Ask again later.", "Better not tell you now.",
    "Cannot predict now.", "Concentrate and ask again.",
    "Don't count on it.", "My reply is no.", "My sources say no.",
    "Outlook not so good.", "Very doubtful.",
]

@bot.tree.command(name="8ball", description="Ask the magic 8-ball a question")
@app_commands.describe(question="Your yes/no question")
async def slash_8ball(interaction: discord.Interaction, question: str):
    await interaction.response.send_message(f"🎱 **{question}**\n> {random.choice(EIGHT_BALL)}")


# ── /roll ─────────────────────────────────────────────────────────────────────

@bot.tree.command(name="roll", description="Roll a dice")
@app_commands.describe(sides="Number of sides (default 6)", times="How many dice to roll (default 1)")
async def slash_roll(interaction: discord.Interaction, sides: int = 6, times: int = 1):
    times = min(max(times, 1), 20)
    sides = min(max(sides, 2), 1000)
    results = [random.randint(1, sides) for _ in range(times)]
    rolls_str = ", ".join(str(r) for r in results)
    msg = f"🎲 Rolling {times}d{sides}: **{rolls_str}**"
    if times > 1:
        msg += f"\nTotal: **{sum(results)}**"
    await interaction.response.send_message(msg)


# ── /ship ─────────────────────────────────────────────────────────────────────

@bot.tree.command(name="ship", description="Check love compatibility 💘")
@app_commands.describe(user1="First person", user2="Second person")
async def slash_ship(interaction: discord.Interaction, user1: discord.Member, user2: discord.Member):
    score = abs(hash(f"{min(user1.id, user2.id)}{max(user1.id, user2.id)}")) % 101
    bar = "💗" * round(score / 10) + "🖤" * (10 - round(score / 10))
    if score >= 80: verdict = "A match made in heaven! 🥰"
    elif score >= 60: verdict = "Pretty good vibes! 😊"
    elif score >= 40: verdict = "Could work with some effort! 🤔"
    elif score >= 20: verdict = "Hmm... it's complicated. 😬"
    else: verdict = "Yikes. Maybe just friends? 😅"
    await interaction.response.send_message(
        f"💘 **{user1.display_name} & {user2.display_name}**\n{bar} **{score}%**\n{verdict}"
    )


# ── /trivia ───────────────────────────────────────────────────────────────────

@bot.tree.command(name="trivia", description="Get a random trivia question")
@app_commands.describe(topic="Optional topic (e.g. science, history, sports)")
async def slash_trivia(interaction: discord.Interaction, topic: str = "random"):
    await interaction.response.defer(thinking=True)
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, ask_ai_once,
            f"Give me one trivia question about {topic} with 4 multiple choice options (A B C D) and the correct answer. Format it nicely."
        )
    except Exception:
        await interaction.followup.send("Couldn't fetch a trivia question!")
        return
    await send_long(interaction, f"🧠 **Trivia Time!**\n\n{reply}")


# ── /story ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="story", description="Generate a short fun story")
@app_commands.describe(topic="What should the story be about?")
async def slash_story(interaction: discord.Interaction, topic: str):
    await interaction.response.defer(thinking=True)
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, ask_ai_once,
            f"Write a short, fun, creative story (3-5 sentences) about: {topic}."
        )
    except Exception:
        await interaction.followup.send("Couldn't write a story right now!")
        return
    await send_long(interaction, f"📖 **Story Time!**\n\n{reply}")


# ── /wouldyourather ───────────────────────────────────────────────────────────

@bot.tree.command(name="wouldyourather", description="Get a fun 'Would You Rather' question")
async def slash_wyr(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, ask_ai_once,
            "Give me one creative 'Would You Rather' question. Format: Would you rather [A] OR [B]?"
        )
    except Exception:
        await interaction.followup.send("Couldn't think of a question!")
        return
    await interaction.followup.send(f"🤔 {reply}\n\nReact with 🅰️ or 🅱️!")


# ── /fact ─────────────────────────────────────────────────────────────────────

@bot.tree.command(name="fact", description="Get a random interesting fact")
@app_commands.describe(topic="Optional topic (e.g. space, animals, history)")
async def slash_fact(interaction: discord.Interaction, topic: str = "anything"):
    await interaction.response.defer(thinking=True)
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, ask_ai_once,
            f"Give me one fascinating, surprising fact about {topic}. Keep it to 2-3 sentences."
        )
    except Exception:
        await interaction.followup.send("Couldn't fetch a fact!")
        return
    await interaction.followup.send(f"💡 **Fun Fact!**\n{reply}")


# ── /advice ───────────────────────────────────────────────────────────────────

@bot.tree.command(name="advice", description="Get some life advice or wisdom")
@app_commands.describe(situation="What do you need advice about?")
async def slash_advice(interaction: discord.Interaction, situation: str = "life in general"):
    await interaction.response.defer(thinking=True)
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, ask_ai_once,
            f"Give thoughtful, practical advice about: {situation}. Keep it to 2-3 sentences."
        )
    except Exception:
        await interaction.followup.send("Couldn't come up with advice!")
        return
    await interaction.followup.send(f"🌟 **Advice**\n{reply}")


# ── /lyrics ───────────────────────────────────────────────────────────────────

def search_lyrics(song: str, artist: str | None) -> dict | None:
    """
    1. Search Genius API for the song → get title, artist, page URL.
    2. Fetch lyrics text from lyrics.ovh (no scraping / no Cloudflare).
    Returns dict with keys: title, artist, lyrics, genius_url  — or None.
    """
    query = f"{song} {artist}" if artist else song
    r = req.get(
        "https://api.genius.com/search",
        params={"q": query},
        headers=GENIUS_HEADERS,
        timeout=10,
    )
    r.raise_for_status()
    hits = r.json().get("response", {}).get("hits", [])
    if not hits:
        return None

    hit = hits[0]["result"]
    title = hit["title"]
    art = hit["primary_artist"]["name"]
    genius_url = hit["url"]
    thumbnail = hit.get("song_art_image_thumbnail_url", "")

    # 1st try: lyrics.ovh (fast, good for English songs)
    lyrics = None
    try:
        lr = req.get(
            f"https://api.lyrics.ovh/v1/{quote(art)}/{quote(title)}",
            timeout=10,
        )
        if lr.status_code == 200:
            lyrics = lr.json().get("lyrics", "").strip() or None
    except Exception:
        pass

    # 2nd try: scrape Genius directly (works for non-English songs)
    if not lyrics:
        try:
            scraper = cloudscraper.create_scraper()
            page = scraper.get(genius_url, timeout=15)
            if page.status_code == 200:
                soup = BeautifulSoup(page.text, "html.parser")
                # Genius stores lyrics in containers with data-lyrics-container attr
                containers = soup.find_all("div", attrs={"data-lyrics-container": "true"})
                if containers:
                    parts = []
                    for c in containers:
                        for br in c.find_all("br"):
                            br.replace_with("\n")
                        parts.append(c.get_text())
                    lyrics = "\n".join(parts).strip() or None
        except Exception as e:
            log.warning("Genius scrape fallback failed: %s", e)

    return {
        "title": title,
        "artist": art,
        "lyrics": lyrics,
        "genius_url": genius_url,
        "thumbnail": thumbnail,
    }


@bot.tree.command(name="lyrics", description="Find lyrics for a song using Genius")
@app_commands.describe(song="Song title", artist="Artist name (optional but helps accuracy)")
async def slash_lyrics(interaction: discord.Interaction, song: str, artist: str = ""):
    await interaction.response.defer(thinking=True)
    try:
        found = await asyncio.get_event_loop().run_in_executor(
            None, search_lyrics, song, artist or None
        )
    except Exception as exc:
        log.exception("Genius error: %s", exc)
        await interaction.followup.send("❌ Something went wrong contacting Genius. Try again!")
        return

    if not found:
        msg = f"❌ Couldn't find **{song}**"
        if artist:
            msg += f" by **{artist}**"
        await interaction.followup.send(msg + ". Try a different spelling!")
        return

    title = found["title"]
    art = found["artist"]
    genius_url = found["genius_url"]
    lyrics = found["lyrics"]
    thumbnail = found["thumbnail"]

    if not lyrics:
        await interaction.followup.send(
            f"🎵 **{title}** — *{art}*\n"
            f"Found the song on Genius but couldn't retrieve the lyrics text.\n"
            f"👉 Read them here: {genius_url}"
        )
        return

    # Send header first, then split lyrics into 1900-char chunks
    await interaction.followup.send(f"🎵 **{title}** — *{art}*\n🔗 <{genius_url}>")

    chunk_size = 1900
    chunks = [lyrics[i:i + chunk_size] for i in range(0, len(lyrics), chunk_size)]
    for i, chunk in enumerate(chunks):
        label = f"*Part {i+1}/{len(chunks)}*\n" if len(chunks) > 1 else ""
        await interaction.channel.send(f"{label}```{chunk}```")


# ── /image ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="image", description="Generate an AI image from a text prompt (free, no limits!)")
@app_commands.describe(prompt="Describe the image you want to generate")
async def slash_image(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer(thinking=True)
    try:
        safe_prompt = quote(prompt)
        seed = random.randint(1, 999999)
        url = (
            f"https://image.pollinations.ai/prompt/{safe_prompt}"
            f"?width=1024&height=1024&seed={seed}&nologo=true&enhance=true"
        )
        # Fetch the image bytes
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: req.get(url, timeout=60)
        )
        if response.status_code != 200:
            await interaction.followup.send("❌ Image generation failed. Try a different prompt!")
            return

        import io
        image_bytes = io.BytesIO(response.content)
        image_bytes.seek(0)
        file = discord.File(fp=image_bytes, filename="image.png")

        embed = discord.Embed(
            title="🎨 AI Image Generated",
            description=f"**Prompt:** {prompt}",
            color=discord.Color.purple()
        )
        embed.set_image(url="attachment://image.png")
        embed.set_footer(text="Powered by Pollinations AI • Free & unlimited")
        await interaction.followup.send(embed=embed, file=file)

    except Exception as exc:
        log.exception("Image generation error: %s", exc)
        await interaction.followup.send("❌ Something went wrong generating the image. Please try again!")


# ── /imagine ──────────────────────────────────────────────────────────────────

@bot.tree.command(name="imagine", description="Generate an AI portrait of a user based on their name/vibe")
@app_commands.describe(user="The user to imagine a portrait for")
async def slash_imagine(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(thinking=True)
    try:
        name = user.display_name
        prompt = (
            f"a stunning digital portrait of a person named '{name}', "
            "cinematic lighting, highly detailed face, fantasy art style, vivid colors, 4k"
        )
        safe_prompt = quote(prompt)
        seed = random.randint(1, 999999)
        url = (
            f"https://image.pollinations.ai/prompt/{safe_prompt}"
            f"?width=512&height=512&seed={seed}&nologo=true&enhance=true"
        )
        response = await asyncio.get_event_loop().run_in_executor(
            None, lambda: req.get(url, timeout=60)
        )
        if response.status_code != 200:
            await interaction.followup.send("❌ Couldn't generate the portrait. Try again!")
            return
        image_bytes = io.BytesIO(response.content)
        image_bytes.seek(0)
        file = discord.File(fp=image_bytes, filename="portrait.png")
        embed = discord.Embed(
            title=f"🎨 AI Portrait of {name}",
            description=f"Here's what I imagine {user.mention} looks like in a fantasy world!",
            color=discord.Color.orange()
        )
        embed.set_image(url="attachment://portrait.png")
        embed.set_footer(text="Powered by Pollinations AI • Free & unlimited")
        await interaction.followup.send(embed=embed, file=file)
    except Exception as exc:
        log.exception("Imagine error: %s", exc)
        await interaction.followup.send("❌ Something went wrong. Please try again!")


# ── /meme ─────────────────────────────────────────────────────────────────────

@bot.tree.command(name="meme", description="Generate a meme image with top and bottom text")
@app_commands.describe(top="Top text", bottom="Bottom text", background="Optional: describe the meme background image")
async def slash_meme(interaction: discord.Interaction, top: str, bottom: str, background: str = "funny meme template"):
    await interaction.response.defer(thinking=True)
    try:
        prompt = (
            f"{background}, meme format, funny, bold text at the top saying '{top}' "
            f"and at the bottom saying '{bottom}', impact font style, high contrast, meme image"
        )
        safe_prompt = quote(prompt)
        seed = random.randint(1, 999999)
        url = (
            f"https://image.pollinations.ai/prompt/{safe_prompt}"
            f"?width=800&height=600&seed={seed}&nologo=true"
        )
        response = await asyncio.get_event_loop().run_in_executor(
            None, lambda: req.get(url, timeout=60)
        )
        if response.status_code != 200:
            await interaction.followup.send("❌ Meme generation failed. Try again!")
            return
        image_bytes = io.BytesIO(response.content)
        image_bytes.seek(0)
        file = discord.File(fp=image_bytes, filename="meme.png")
        embed = discord.Embed(
            title="😂 Meme Generated!",
            description=f"**Top:** {top}\n**Bottom:** {bottom}",
            color=discord.Color.yellow()
        )
        embed.set_image(url="attachment://meme.png")
        embed.set_footer(text="Powered by Pollinations AI")
        await interaction.followup.send(embed=embed, file=file)
    except Exception as exc:
        log.exception("Meme error: %s", exc)
        await interaction.followup.send("❌ Something went wrong. Please try again!")


# ── /avatar ───────────────────────────────────────────────────────────────────

AVATAR_STYLES = ["anime", "pixel art", "oil painting", "watercolor", "cyberpunk", "cartoon", "sketch", "3D render"]

@bot.tree.command(name="avatar", description="Reimagine your avatar in a fun art style")
@app_commands.describe(style="Art style: anime, pixel art, oil painting, watercolor, cyberpunk, cartoon, sketch, 3D render")
async def slash_avatar(interaction: discord.Interaction, style: str = "anime"):
    await interaction.response.defer(thinking=True)
    try:
        valid = [s for s in AVATAR_STYLES if style.lower() in s.lower()]
        chosen_style = valid[0] if valid else style
        name = interaction.user.display_name
        prompt = (
            f"a {chosen_style} style avatar/portrait of a person named '{name}', "
            f"high quality {chosen_style} art, detailed, colorful, profile picture style"
        )
        safe_prompt = quote(prompt)
        seed = random.randint(1, 999999)
        url = (
            f"https://image.pollinations.ai/prompt/{safe_prompt}"
            f"?width=512&height=512&seed={seed}&nologo=true&enhance=true"
        )
        response = await asyncio.get_event_loop().run_in_executor(
            None, lambda: req.get(url, timeout=60)
        )
        if response.status_code != 200:
            await interaction.followup.send("❌ Avatar generation failed. Try again!")
            return
        image_bytes = io.BytesIO(response.content)
        image_bytes.seek(0)
        file = discord.File(fp=image_bytes, filename="avatar.png")
        embed = discord.Embed(
            title=f"✨ {interaction.user.display_name}'s {chosen_style.title()} Avatar",
            description=f"Here's your profile picture reimagined in **{chosen_style}** style!",
            color=discord.Color.teal()
        )
        embed.set_image(url="attachment://avatar.png")
        embed.set_footer(text=f"Style: {chosen_style} • Powered by Pollinations AI")
        await interaction.followup.send(embed=embed, file=file)
    except Exception as exc:
        log.exception("Avatar error: %s", exc)
        await interaction.followup.send("❌ Something went wrong. Please try again!")


# ── /daily ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="daily", description="Claim your daily coins! 💰")
async def slash_daily(interaction: discord.Interaction):
    uid = interaction.user.id
    data = eco_load()
    key = str(uid)
    now = time.time()
    user_data = data.get(key, {"coins": 0, "last_daily": 0, "badges": []})
    last = user_data.get("last_daily", 0)
    cooldown = 86400  # 24 hours
    elapsed = now - last
    if elapsed < cooldown:
        remaining = cooldown - elapsed
        hours, rem = divmod(int(remaining), 3600)
        minutes = rem // 60
        await interaction.response.send_message(
            f"⏳ You already claimed today! Come back in **{hours}h {minutes}m**.",
            ephemeral=True
        )
        return
    bonus = random.randint(80, 150)
    user_data["coins"] = user_data.get("coins", 0) + bonus
    user_data["last_daily"] = now
    if key not in data:
        user_data["badges"] = []
    data[key] = user_data
    eco_save(data)
    embed = discord.Embed(
        title="💰 Daily Coins Claimed!",
        description=f"**+{bonus} coins** added to your wallet!\nYou now have **{user_data['coins']} coins** 🪙",
        color=discord.Color.gold()
    )
    embed.set_footer(text="Come back in 24 hours for more!")
    await interaction.response.send_message(embed=embed)


# ── /balance ──────────────────────────────────────────────────────────────────

@bot.tree.command(name="balance", description="Check your coin balance (or someone else's)")
@app_commands.describe(user="User to check (leave blank for yourself)")
async def slash_balance(interaction: discord.Interaction, user: discord.Member | None = None):
    target = user or interaction.user
    profile = eco_get(target.id)
    coins = profile.get("coins", 0)
    badges = profile.get("badges", [])
    badge_str = " ".join(badges) if badges else "None yet"
    embed = discord.Embed(
        title=f"💼 {target.display_name}'s Wallet",
        color=discord.Color.gold()
    )
    embed.add_field(name="🪙 Coins", value=f"**{coins}**", inline=True)
    embed.add_field(name="🏅 Badges", value=badge_str, inline=True)
    embed.set_thumbnail(url=target.display_avatar.url)
    await interaction.response.send_message(embed=embed)


# ── /gamble ───────────────────────────────────────────────────────────────────

SLOT_EMOJIS = ["🍒", "🍋", "🍊", "🍇", "⭐", "💎", "7️⃣"]

@bot.tree.command(name="gamble", description="Spin the slots and gamble your coins! 🎰")
@app_commands.describe(amount="How many coins to bet")
async def slash_gamble(interaction: discord.Interaction, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❌ Bet at least 1 coin!", ephemeral=True)
        return
    profile = eco_get(interaction.user.id)
    coins = profile.get("coins", 0)
    if amount > coins:
        await interaction.response.send_message(
            f"❌ You only have **{coins} coins**. You can't bet more than you have!",
            ephemeral=True
        )
        return
    slots = [random.choice(SLOT_EMOJIS) for _ in range(3)]
    slot_display = " | ".join(slots)
    if slots[0] == slots[1] == slots[2]:
        if slots[0] == "💎":
            mult, result, color = 10, "JACKPOT!! 💎💎💎", discord.Color.blue()
        elif slots[0] == "7️⃣":
            mult, result, color = 5, "BIG WIN! 7️⃣7️⃣7️⃣", discord.Color.gold()
        else:
            mult, result, color = 3, "Winner! 🎉", discord.Color.green()
        winnings = amount * mult
        new_bal = eco_add(interaction.user.id, winnings - amount)
        desc = f"[ {slot_display} ]\n\n**{result}**\nYou won **+{winnings} coins**! (×{mult})\nBalance: **{new_bal} coins**"
    elif slots[0] == slots[1] or slots[1] == slots[2] or slots[0] == slots[2]:
        winnings = amount
        new_bal = eco_add(interaction.user.id, 0)  # break even
        result, color = "Almost! You break even.", discord.Color.orange()
        desc = f"[ {slot_display} ]\n\n**{result}**\nBalance: **{coins} coins**"
    else:
        new_bal = eco_add(interaction.user.id, -amount)
        result, color = "You lost 😢", discord.Color.red()
        desc = f"[ {slot_display} ]\n\n**{result}**\nYou lost **{amount} coins**.\nBalance: **{new_bal} coins**"
    embed = discord.Embed(title="🎰 Slot Machine", description=desc, color=color)
    await interaction.response.send_message(embed=embed)


# ── /leaderboard ──────────────────────────────────────────────────────────────

@bot.tree.command(name="leaderboard", description="See the richest users in the economy 💰")
async def slash_leaderboard(interaction: discord.Interaction):
    data = eco_load()
    if not data:
        await interaction.response.send_message("No economy data yet! Use `/daily` to get started.", ephemeral=True)
        return
    sorted_users = sorted(data.items(), key=lambda x: x[1].get("coins", 0), reverse=True)[:10]
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = []
    for i, (uid, udata) in enumerate(sorted_users):
        try:
            member = interaction.guild.get_member(int(uid))
            name = member.display_name if member else f"User#{uid[-4:]}"
        except Exception:
            name = f"User#{uid[-4:]}"
        coins = udata.get("coins", 0)
        lines.append(f"{medals[i]} **{name}** — {coins} 🪙")
    embed = discord.Embed(
        title="🏆 Coin Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold()
    )
    embed.set_footer(text="Use /daily and /gamble to earn coins!")
    await interaction.response.send_message(embed=embed)


# ── /shop ─────────────────────────────────────────────────────────────────────

@bot.tree.command(name="shop", description="Buy special roles with your coins! 🛒")
@app_commands.describe(item="Item to buy (leave blank to browse the shop)")
async def slash_shop(interaction: discord.Interaction, item: str | None = None):
    if item is None:
        embed = discord.Embed(title="🛒 Coin Shop", color=discord.Color.blurple())
        embed.description = "Use `/shop item:<name>` to buy an item!\n\n"
        for name, (cost, emoji, desc) in SHOP_ITEMS.items():
            embed.add_field(name=f"{emoji} {name} — {cost} 🪙", value=desc, inline=False)
        embed.set_footer(text="Earn coins with /daily and /gamble!")
        await interaction.response.send_message(embed=embed)
        return
    match = next((k for k in SHOP_ITEMS if k.lower() == item.lower()), None)
    if not match:
        items_list = ", ".join(SHOP_ITEMS.keys())
        await interaction.response.send_message(
            f"❌ Item not found! Available items: {items_list}", ephemeral=True
        )
        return
    cost, emoji, desc = SHOP_ITEMS[match]
    profile = eco_get(interaction.user.id)
    coins = profile.get("coins", 0)
    if coins < cost:
        await interaction.response.send_message(
            f"❌ You need **{cost} coins** but only have **{coins}**. Keep grinding! 💪",
            ephemeral=True
        )
        return
    # Try to give/create the role
    guild = interaction.guild
    role = discord.utils.get(guild.roles, name=match)
    if role is None:
        try:
            colors_map = {"VIP": 0xFFD700, "Diamond": 0x00BFFF, "King": 0x9B59B6, "Legend": 0xFF4500}
            role = await guild.create_role(
                name=match,
                color=discord.Color(colors_map.get(match, 0xFFFFFF)),
                reason="Shop purchase"
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to create/manage roles. Ask an admin to give me that permission!",
                ephemeral=True
            )
            return
    try:
        await interaction.user.add_roles(role, reason="Shop purchase")
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ I don't have permission to assign roles. Ask an admin to move my role higher!",
            ephemeral=True
        )
        return
    eco_add(interaction.user.id, -cost)
    data = eco_load()
    uid = str(interaction.user.id)
    if "badges" not in data[uid]:
        data[uid]["badges"] = []
    if emoji not in data[uid]["badges"]:
        data[uid]["badges"].append(emoji)
    eco_save(data)
    embed = discord.Embed(
        title="✅ Purchase Successful!",
        description=f"You bought **{emoji} {match}** for **{cost} coins**!\nYou now have the role {role.mention}.",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed)


# ── /remindme ─────────────────────────────────────────────────────────────────

@bot.tree.command(name="remindme", description="Set a reminder — bot will DM you! ⏰")
@app_commands.describe(
    duration="When to remind you (e.g. 30m, 2h, 1d)",
    message="What to remind you about"
)
async def slash_remindme(interaction: discord.Interaction, duration: str, message: str):
    secs = parse_duration(duration)
    if secs is None or secs <= 0:
        await interaction.response.send_message(
            "❌ Invalid duration! Use formats like `30m`, `2h`, `1d`.",
            ephemeral=True
        )
        return
    if secs > 86400 * 7:
        await interaction.response.send_message("❌ Maximum reminder time is 7 days.", ephemeral=True)
        return
    user = interaction.user
    await interaction.response.send_message(
        f"⏰ Got it! I'll DM you in **{duration}** about: *{message}*", ephemeral=True
    )
    async def send_reminder():
        await asyncio.sleep(secs)
        try:
            embed = discord.Embed(
                title="⏰ Reminder!",
                description=message,
                color=discord.Color.blurple(),
                timestamp=datetime.datetime.utcnow()
            )
            embed.set_footer(text=f"You asked me to remind you {duration} ago")
            await user.send(embed=embed)
        except Exception:
            pass
    asyncio.create_task(send_reminder())


# ── /poll (quick yes/no) ──────────────────────────────────────────────────────

@bot.tree.command(name="poll", description="Create a quick yes/no poll 📊")
@app_commands.describe(question="The poll question")
async def slash_poll(interaction: discord.Interaction, question: str):
    embed = discord.Embed(
        title="📊 Poll",
        description=f"**{question}**",
        color=discord.Color.blurple(),
        timestamp=datetime.datetime.utcnow()
    )
    embed.set_footer(text=f"Poll by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    await msg.add_reaction("✅")
    await msg.add_reaction("❌")


# ── /translate ────────────────────────────────────────────────────────────────

@bot.tree.command(name="translate", description="Translate text to any language using AI 🌐")
@app_commands.describe(text="Text to translate", language="Target language (e.g. Spanish, Japanese, French)")
async def slash_translate(interaction: discord.Interaction, text: str, language: str = "English"):
    await interaction.response.defer(thinking=True)
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: ask_ai_once(
                f"Translate the following text to {language}. "
                f"Reply with ONLY the translated text, no explanations:\n\n{text}"
            )
        )
        embed = discord.Embed(
            title=f"🌐 Translated to {language}",
            color=discord.Color.blue()
        )
        embed.add_field(name="Original", value=text[:1000], inline=False)
        embed.add_field(name="Translation", value=result[:1000], inline=False)
        await interaction.followup.send(embed=embed)
    except Exception as exc:
        log.exception("Translate error: %s", exc)
        await interaction.followup.send("❌ Translation failed. Please try again!")


# ── /summarize ────────────────────────────────────────────────────────────────

@bot.tree.command(name="summarize", description="AI summarizes any long text 📝")
@app_commands.describe(text="The text you want summarized")
async def slash_summarize(interaction: discord.Interaction, text: str):
    await interaction.response.defer(thinking=True)
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: ask_ai_once(
                f"Summarize the following text in 3-5 bullet points. Be concise and clear:\n\n{text}"
            )
        )
        embed = discord.Embed(
            title="📝 Summary",
            description=result[:4000],
            color=discord.Color.green()
        )
        embed.set_footer(text=f"Summarized {len(text)} characters → {len(result)} characters")
        await interaction.followup.send(embed=embed)
    except Exception as exc:
        log.exception("Summarize error: %s", exc)
        await interaction.followup.send("❌ Summarization failed. Please try again!")


# ── More admin commands ───────────────────────────────────────────────────────

@admin_group.command(name="kick", description="Kick a member from the server")
@app_commands.describe(member="Member to kick", reason="Reason for kick")
async def admin_kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"👢 **{member.display_name}** has been kicked. Reason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to kick this member.", ephemeral=True)

@admin_group.command(name="ban", description="Ban a member from the server")
@app_commands.describe(member="Member to ban", reason="Reason for ban", delete_days="Days of messages to delete (0-7)")
async def admin_ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_days: int = 0):
    try:
        await member.ban(reason=reason, delete_message_days=min(max(delete_days, 0), 7))
        await interaction.response.send_message(f"🔨 **{member.display_name}** has been banned. Reason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to ban this member.", ephemeral=True)

@admin_group.command(name="unban", description="Unban a user by their ID")
@app_commands.describe(user_id="The Discord user ID to unban")
async def admin_unban(interaction: discord.Interaction, user_id: str):
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user)
        await interaction.response.send_message(f"✅ **{user.name}** has been unbanned.")
    except Exception:
        await interaction.response.send_message("❌ Could not unban that user. Check the ID.", ephemeral=True)

@admin_group.command(name="serverinfo", description="Show detailed server information")
async def admin_serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    bots = sum(1 for m in g.members if m.bot)
    humans = g.member_count - bots
    embed = discord.Embed(title=f"📊 {g.name}", color=discord.Color.blurple(), timestamp=datetime.datetime.utcnow())
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="👑 Owner", value=f"<@{g.owner_id}>", inline=True)
    embed.add_field(name="📅 Created", value=g.created_at.strftime("%b %d, %Y"), inline=True)
    embed.add_field(name="🆔 Server ID", value=str(g.id), inline=True)
    embed.add_field(name="👥 Members", value=f"{humans} humans, {bots} bots", inline=True)
    embed.add_field(name="💬 Channels", value=f"{len(g.text_channels)} text, {len(g.voice_channels)} voice", inline=True)
    embed.add_field(name="🎭 Roles", value=str(len(g.roles)), inline=True)
    embed.add_field(name="🚀 Boost Level", value=f"Level {g.premium_tier} ({g.premium_subscription_count} boosts)", inline=True)
    embed.add_field(name="😀 Emojis", value=str(len(g.emojis)), inline=True)
    await interaction.response.send_message(embed=embed)

@admin_group.command(name="coinsgive", description="Give coins to a user (admin cheat)")
@app_commands.describe(user="Target user", amount="Coins to give")
async def admin_coinsgive(interaction: discord.Interaction, user: discord.Member, amount: int):
    new_bal = eco_add(user.id, amount)
    await interaction.response.send_message(
        f"✅ Gave **{amount} coins** to {user.mention}. New balance: **{new_bal} coins**"
    )

@admin_group.command(name="coinsreset", description="Reset a user's coin balance to 0")
@app_commands.describe(user="Target user")
async def admin_coinsreset(interaction: discord.Interaction, user: discord.Member):
    eco_update(user.id, coins=0)
    await interaction.response.send_message(f"✅ Reset {user.mention}'s balance to **0 coins**.")


# ── /owner (bot owner only) ───────────────────────────────────────────────────

owner_group = app_commands.Group(name="owner", description="Bot owner only commands")

def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == BOT_OWNER_ID

@owner_group.command(name="settings", description="View current economy settings")
async def owner_settings(interaction: discord.Interaction):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    s = eco_settings()
    embed = discord.Embed(title="⚙️ Economy Settings", color=discord.Color.orange())
    for k, v in s.items():
        embed.add_field(name=k, value=str(v), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@owner_group.command(name="setdaily", description="Set the daily coins range")
@app_commands.describe(min_coins="Minimum daily coins", max_coins="Maximum daily coins")
async def owner_setdaily(interaction: discord.Interaction, min_coins: int, max_coins: int):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    s = eco_settings()
    s["daily_min"] = min_coins
    s["daily_max"] = max_coins
    eco_save_settings(s)
    await interaction.response.send_message(f"✅ Daily range set to **{min_coins}–{max_coins} coins**.", ephemeral=True)

@owner_group.command(name="setwork", description="Set the work coins range and cooldown")
@app_commands.describe(min_coins="Min work coins", max_coins="Max work coins", cooldown_mins="Cooldown in minutes")
async def owner_setwork(interaction: discord.Interaction, min_coins: int, max_coins: int, cooldown_mins: int = 60):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    s = eco_settings()
    s["work_min"] = min_coins
    s["work_max"] = max_coins
    s["work_cooldown"] = cooldown_mins * 60
    eco_save_settings(s)
    await interaction.response.send_message(
        f"✅ Work: **{min_coins}–{max_coins} coins**, cooldown **{cooldown_mins}m**.", ephemeral=True)

@owner_group.command(name="setrob", description="Set rob success chance and fine %")
@app_commands.describe(chance="Success chance 0-100", fine_pct="Fine % of stolen if caught (0-100)")
async def owner_setrob(interaction: discord.Interaction, chance: int, fine_pct: int):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    s = eco_settings()
    s["rob_chance"] = max(0, min(100, chance))
    s["rob_fine"] = max(0, min(100, fine_pct))
    eco_save_settings(s)
    await interaction.response.send_message(
        f"✅ Rob chance: **{s['rob_chance']}%**, fine: **{s['rob_fine']}%**.", ephemeral=True)

@owner_group.command(name="give", description="Give coins to any user")
@app_commands.describe(user="Target user", amount="Amount of coins")
async def owner_give(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    new_bal = eco_add(user.id, amount)
    await interaction.response.send_message(f"✅ Gave **{amount}** coins to {user.mention}. Balance: **{new_bal}**", ephemeral=True)

@owner_group.command(name="setbalance", description="Set a user's exact coin balance")
@app_commands.describe(user="Target user", amount="Exact balance to set")
async def owner_setbalance(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    eco_update(user.id, coins=max(0, amount))
    await interaction.response.send_message(f"✅ Set {user.mention}'s balance to **{amount} coins**.", ephemeral=True)

@owner_group.command(name="reset", description="Reset a user's entire economy profile")
@app_commands.describe(user="Target user")
async def owner_reset(interaction: discord.Interaction, user: discord.Member):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    eco_update(user.id, **_ECO_USER_DEFAULT)
    await interaction.response.send_message(f"✅ Reset {user.mention}'s economy profile.", ephemeral=True)

@owner_group.command(name="resetall", description="⚠️ Wipe ALL economy data")
async def owner_resetall(interaction: discord.Interaction):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    settings = eco_settings()
    eco_save({"__settings__": settings})
    await interaction.response.send_message("✅ All economy data wiped (settings preserved).", ephemeral=True)

bot.tree.add_command(owner_group)


# ── /give ─────────────────────────────────────────────────────────────────────

@bot.tree.command(name="give", description="Give some of your coins to another user 🎁")
@app_commands.describe(user="Who to give coins to", amount="How many coins to give")
async def slash_give(interaction: discord.Interaction, user: discord.Member, amount: int):
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ You can't give coins to yourself!", ephemeral=True); return
    if user.bot:
        await interaction.response.send_message("❌ You can't give coins to a bot!", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive!", ephemeral=True); return
    profile = eco_get(interaction.user.id)
    if profile["coins"] < amount:
        await interaction.response.send_message(
            f"❌ You only have **{profile['coins']} coins**!", ephemeral=True); return
    eco_add(interaction.user.id, -amount)
    eco_add(user.id, amount)
    embed = discord.Embed(
        title="🎁 Coins Gifted!",
        description=f"{interaction.user.mention} gave **{amount} coins** to {user.mention}!",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed)


# ── /work ─────────────────────────────────────────────────────────────────────

WORK_JOBS = [
    "delivered pizzas 🍕", "walked dogs 🐕", "coded a website 💻",
    "fixed a bug 🐛", "mowed lawns 🌿", "washed cars 🚗",
    "babysat kids 👶", "wrote an essay ✍️", "sold lemonade 🍋",
    "streamed on Twitch 🎮", "drove Uber 🚕", "tutored students 📚",
]

@bot.tree.command(name="work", description="Work to earn coins! Has a cooldown ⏱️")
async def slash_work(interaction: discord.Interaction):
    s = eco_settings()
    profile = eco_get(interaction.user.id)
    now = time.time()
    elapsed = now - profile.get("last_work", 0)
    cooldown = s["work_cooldown"]
    if elapsed < cooldown:
        remaining = cooldown - elapsed
        mins = int(remaining // 60)
        secs = int(remaining % 60)
        await interaction.response.send_message(
            f"⏳ You're tired! Rest for **{mins}m {secs}s** before working again.", ephemeral=True); return
    earned = random.randint(s["work_min"], s["work_max"])
    job = random.choice(WORK_JOBS)
    new_bal = eco_add(interaction.user.id, earned)
    eco_update(interaction.user.id, last_work=now)
    embed = discord.Embed(
        title="💼 Work Complete!",
        description=f"You {job} and earned **+{earned} coins**!\nBalance: **{new_bal} coins** 🪙",
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Come back in {cooldown // 60} minutes to work again!")
    await interaction.response.send_message(embed=embed)


# ── /rob ──────────────────────────────────────────────────────────────────────

@bot.tree.command(name="rob", description="Try to rob someone's coins! Risky 🦹")
@app_commands.describe(user="Who to rob")
async def slash_rob(interaction: discord.Interaction, user: discord.Member):
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ You can't rob yourself!", ephemeral=True); return
    if user.bot:
        await interaction.response.send_message("❌ You can't rob a bot!", ephemeral=True); return
    s = eco_settings()
    robber_profile = eco_get(interaction.user.id)
    victim_profile = eco_get(user.id)
    now = time.time()
    elapsed = now - robber_profile.get("last_rob", 0)
    if elapsed < 3600:
        mins = int((3600 - elapsed) // 60)
        await interaction.response.send_message(
            f"⏳ You're lying low after your last job. Wait **{mins}m**.", ephemeral=True); return
    if victim_profile["coins"] < 50:
        await interaction.response.send_message(
            f"❌ {user.display_name} is broke (< 50 coins). Not worth the risk!", ephemeral=True); return
    eco_update(interaction.user.id, last_rob=now)
    if random.randint(1, 100) <= s["rob_chance"]:
        steal = random.randint(10, min(200, victim_profile["coins"] // 2 or 10))
        eco_add(user.id, -steal)
        eco_add(interaction.user.id, steal)
        embed = discord.Embed(
            title="🦹 Robbery Success!",
            description=f"You sneaked into {user.mention}'s wallet and stole **{steal} coins**! 💰",
            color=discord.Color.green()
        )
    else:
        fine = int(robber_profile["coins"] * s["rob_fine"] / 100)
        fine = max(fine, 0)
        eco_add(interaction.user.id, -fine)
        embed = discord.Embed(
            title="👮 Caught Red-Handed!",
            description=f"You got caught trying to rob {user.mention}!\nYou paid **{fine} coins** as a fine. 😬",
            color=discord.Color.red()
        )
    await interaction.response.send_message(embed=embed)


# ── /coinflip ─────────────────────────────────────────────────────────────────

@bot.tree.command(name="coinflip", description="Bet coins on a coin flip 🪙")
@app_commands.describe(amount="Amount to bet", choice="heads or tails")
@app_commands.choices(choice=[
    app_commands.Choice(name="Heads", value="heads"),
    app_commands.Choice(name="Tails", value="tails"),
])
async def slash_coinflip(interaction: discord.Interaction, amount: int, choice: str):
    if amount <= 0:
        await interaction.response.send_message("❌ Bet at least 1 coin!", ephemeral=True); return
    profile = eco_get(interaction.user.id)
    if profile["coins"] < amount:
        await interaction.response.send_message(
            f"❌ You only have **{profile['coins']} coins**!", ephemeral=True); return
    result = random.choice(["heads", "tails"])
    flip_emoji = "🪙 HEADS" if result == "heads" else "🪙 TAILS"
    if result == choice:
        new_bal = eco_add(interaction.user.id, amount)
        embed = discord.Embed(
            title=f"🎉 {flip_emoji} — You WIN!",
            description=f"You bet on **{choice}** and won **+{amount} coins**!\nBalance: **{new_bal} coins**",
            color=discord.Color.green()
        )
    else:
        new_bal = eco_add(interaction.user.id, -amount)
        embed = discord.Embed(
            title=f"😢 {flip_emoji} — You LOSE!",
            description=f"You bet on **{choice}** but it was **{result}**.\nYou lost **{amount} coins**.\nBalance: **{new_bal} coins**",
            color=discord.Color.red()
        )
    await interaction.response.send_message(embed=embed)


# ── /profile ──────────────────────────────────────────────────────────────────

@bot.tree.command(name="profile", description="View your full economy profile 📊")
@app_commands.describe(user="User to view (leave blank for yourself)")
async def slash_profile(interaction: discord.Interaction, user: discord.Member | None = None):
    target = user or interaction.user
    p = eco_get(target.id)
    data = eco_load()
    all_users = [(uid, d.get("coins", 0)) for uid, d in data.items() if not uid.startswith("__")]
    all_users.sort(key=lambda x: x[1], reverse=True)
    rank = next((i + 1 for i, (uid, _) in enumerate(all_users) if uid == str(target.id)), "?")
    badges = " ".join(p.get("badges", [])) or "None"
    embed = discord.Embed(title=f"📊 {target.display_name}'s Profile", color=discord.Color.blurple())
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="🪙 Balance", value=f"**{p['coins']} coins**", inline=True)
    embed.add_field(name="🏆 Rank", value=f"**#{rank}**", inline=True)
    embed.add_field(name="🏅 Badges", value=badges, inline=True)
    embed.add_field(name="📈 Total Earned", value=f"{p.get('total_earned', 0)} coins", inline=True)
    embed.add_field(name="📉 Total Lost", value=f"{p.get('total_lost', 0)} coins", inline=True)
    await interaction.response.send_message(embed=embed)


# ── Prefix commands (b <command>) ─────────────────────────────────────────────

def _fmt_cooldown(remaining: float) -> str:
    h, rem = divmod(int(remaining), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

@bot.command(name="daily", aliases=["d"])
async def cmd_daily(ctx: commands.Context):
    uid = ctx.author.id
    data = eco_load()
    key = str(uid)
    now = time.time()
    s = eco_settings()
    user_data = data.get(key, dict(_ECO_USER_DEFAULT))
    elapsed = now - user_data.get("last_daily", 0)
    if elapsed < 86400:
        return await ctx.send(f"⏳ Already claimed! Come back in **{_fmt_cooldown(86400 - elapsed)}**.")
    bonus = random.randint(s["daily_min"], s["daily_max"])
    user_data["coins"] = user_data.get("coins", 0) + bonus
    user_data["last_daily"] = now
    user_data.setdefault("badges", [])
    data[key] = user_data
    eco_save(data)
    await ctx.send(f"💰 **Daily claimed!** +**{bonus} coins** → **{user_data['coins']} coins** 🪙")

@bot.command(name="gamble", aliases=["g", "bet", "slots"])
async def cmd_gamble(ctx: commands.Context, amount: int = 0):
    if amount <= 0:
        return await ctx.send("❌ Usage: `b g <amount>`")
    profile = eco_get(ctx.author.id)
    if amount > profile["coins"]:
        return await ctx.send(f"❌ You only have **{profile['coins']} coins**!")
    slots = [random.choice(SLOT_EMOJIS) for _ in range(3)]
    slot_display = " | ".join(slots)
    if slots[0] == slots[1] == slots[2]:
        if slots[0] == "💎":
            mult, result = 10, "JACKPOT!! 💎💎💎"
        elif slots[0] == "7️⃣":
            mult, result = 5, "BIG WIN! 7️⃣7️⃣7️⃣"
        else:
            mult, result = 3, "Winner! 🎉"
        winnings = amount * mult
        new_bal = eco_add(ctx.author.id, winnings - amount)
        await ctx.send(f"🎰 [ {slot_display} ]\n**{result}** You won **+{winnings} coins**! (×{mult}) → **{new_bal} coins**")
    elif slots[0] == slots[1] or slots[1] == slots[2] or slots[0] == slots[2]:
        await ctx.send(f"🎰 [ {slot_display} ]\nAlmost! You **break even**. → **{profile['coins']} coins**")
    else:
        new_bal = eco_add(ctx.author.id, -amount)
        await ctx.send(f"🎰 [ {slot_display} ]\nYou lost **{amount} coins** 😢 → **{new_bal} coins**")

@bot.command(name="balance", aliases=["bal", "wallet", "b"])
async def cmd_balance(ctx: commands.Context, user: discord.Member | None = None):
    target = user or ctx.author
    p = eco_get(target.id)
    badges = " ".join(p.get("badges", [])) or "—"
    await ctx.send(f"💼 **{target.display_name}** | 🪙 **{p['coins']} coins** | Badges: {badges}")

@bot.command(name="leaderboard", aliases=["lb", "top", "rich"])
async def cmd_leaderboard(ctx: commands.Context):
    data = eco_load()
    if not data:
        return await ctx.send("No economy data yet! Use `b daily` to start.")
    sorted_users = sorted(
        [(uid, d) for uid, d in data.items() if not uid.startswith("__")],
        key=lambda x: x[1].get("coins", 0), reverse=True
    )[:10]
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = []
    for i, (uid, udata) in enumerate(sorted_users):
        member = ctx.guild.get_member(int(uid))
        name = member.display_name if member else f"User#{uid[-4:]}"
        lines.append(f"{medals[i]} **{name}** — {udata.get('coins', 0)} 🪙")
    await ctx.send("🏆 **Coin Leaderboard**\n" + "\n".join(lines))

@bot.command(name="give", aliases=["pay", "transfer"])
async def cmd_give(ctx: commands.Context, user: discord.Member | None = None, amount: int = 0):
    if not user or amount <= 0:
        return await ctx.send("❌ Usage: `b give @user <amount>`")
    if user.id == ctx.author.id:
        return await ctx.send("❌ You can't give coins to yourself!")
    profile = eco_get(ctx.author.id)
    if profile["coins"] < amount:
        return await ctx.send(f"❌ You only have **{profile['coins']} coins**!")
    eco_add(ctx.author.id, -amount)
    eco_add(user.id, amount)
    await ctx.send(f"🎁 **{ctx.author.display_name}** gave **{amount} coins** to {user.mention}!")

@bot.command(name="work", aliases=["w"])
async def cmd_work(ctx: commands.Context):
    s = eco_settings()
    profile = eco_get(ctx.author.id)
    now = time.time()
    elapsed = now - profile.get("last_work", 0)
    cooldown = s["work_cooldown"]
    if elapsed < cooldown:
        return await ctx.send(f"⏳ Rest for **{_fmt_cooldown(cooldown - elapsed)}** before working again.")
    earned = random.randint(s["work_min"], s["work_max"])
    job = random.choice(WORK_JOBS)
    new_bal = eco_add(ctx.author.id, earned)
    eco_update(ctx.author.id, last_work=now)
    await ctx.send(f"💼 You {job} and earned **+{earned} coins**! Balance: **{new_bal} coins** 🪙")

@bot.command(name="rob", aliases=["steal"])
async def cmd_rob(ctx: commands.Context, user: discord.Member | None = None):
    if not user:
        return await ctx.send("❌ Usage: `b rob @user`")
    if user.id == ctx.author.id:
        return await ctx.send("❌ You can't rob yourself!")
    s = eco_settings()
    robber = eco_get(ctx.author.id)
    victim = eco_get(user.id)
    now = time.time()
    elapsed = now - robber.get("last_rob", 0)
    if elapsed < 3600:
        return await ctx.send(f"⏳ Lay low for **{_fmt_cooldown(3600 - elapsed)}** first.")
    if victim["coins"] < 50:
        return await ctx.send(f"❌ {user.display_name} is broke! Not worth it.")
    eco_update(ctx.author.id, last_rob=now)
    if random.randint(1, 100) <= s["rob_chance"]:
        steal = random.randint(10, min(200, victim["coins"] // 2 or 10))
        eco_add(user.id, -steal)
        eco_add(ctx.author.id, steal)
        await ctx.send(f"🦹 **Heist success!** You stole **{steal} coins** from {user.mention}!")
    else:
        fine = int(robber["coins"] * s["rob_fine"] / 100)
        eco_add(ctx.author.id, -fine)
        await ctx.send(f"👮 **Caught!** You paid **{fine} coins** as a fine. 😬")

@bot.command(name="coinflip", aliases=["cf", "flip"])
async def cmd_coinflip(ctx: commands.Context, choice: str = "", amount: int = 0):
    if choice.lower() not in ("heads", "tails", "h", "t") or amount <= 0:
        return await ctx.send("❌ Usage: `b cf heads 100` or `b cf tails 50`")
    choice_norm = "heads" if choice.lower() in ("heads", "h") else "tails"
    profile = eco_get(ctx.author.id)
    if profile["coins"] < amount:
        return await ctx.send(f"❌ You only have **{profile['coins']} coins**!")
    result = random.choice(["heads", "tails"])
    if result == choice_norm:
        new_bal = eco_add(ctx.author.id, amount)
        await ctx.send(f"🪙 **{result.upper()}!** You guessed right and won **+{amount} coins**! → **{new_bal} coins**")
    else:
        new_bal = eco_add(ctx.author.id, -amount)
        await ctx.send(f"🪙 **{result.upper()}!** Wrong guess — lost **{amount} coins** 😢 → **{new_bal} coins**")

@bot.command(name="profile", aliases=["p", "stats"])
async def cmd_profile(ctx: commands.Context, user: discord.Member | None = None):
    target = user or ctx.author
    p = eco_get(target.id)
    data = eco_load()
    sorted_users = sorted(
        [(uid, d.get("coins", 0)) for uid, d in data.items() if not uid.startswith("__")],
        key=lambda x: x[1], reverse=True
    )
    rank = next((i + 1 for i, (uid, _) in enumerate(sorted_users) if uid == str(target.id)), "?")
    badges = " ".join(p.get("badges", [])) or "—"
    await ctx.send(
        f"📊 **{target.display_name}** | 🪙 **{p['coins']} coins** | 🏆 Rank **#{rank}** | "
        f"📈 Earned: {p.get('total_earned', 0)} | 📉 Lost: {p.get('total_lost', 0)} | Badges: {badges}"
    )

@bot.command(name="shop", aliases=["store"])
async def cmd_shop(ctx: commands.Context, *, item: str | None = None):
    if item is None:
        lines = [f"{emoji} **{name}** — {cost} 🪙 | {desc}" for name, (cost, emoji, desc) in SHOP_ITEMS.items()]
        return await ctx.send("🛒 **Shop** — use `b shop <item>` to buy\n" + "\n".join(lines))
    match = next((k for k in SHOP_ITEMS if k.lower() == item.lower()), None)
    if not match:
        return await ctx.send(f"❌ Item not found. Available: {', '.join(SHOP_ITEMS.keys())}")
    cost, emoji, _ = SHOP_ITEMS[match]
    profile = eco_get(ctx.author.id)
    if profile["coins"] < cost:
        return await ctx.send(f"❌ Need **{cost} coins**. You have **{profile['coins']}**. Keep grinding!")
    role = discord.utils.get(ctx.guild.roles, name=match)
    if role is None:
        colors_map = {"VIP": 0xFFD700, "Diamond": 0x00BFFF, "King": 0x9B59B6, "Legend": 0xFF4500}
        try:
            role = await ctx.guild.create_role(name=match, color=discord.Color(colors_map.get(match, 0xFFFFFF)))
        except discord.Forbidden:
            return await ctx.send("❌ Missing Manage Roles permission.")
    try:
        await ctx.author.add_roles(role)
    except discord.Forbidden:
        return await ctx.send("❌ Can't assign role — move my role higher in server settings.")
    eco_add(ctx.author.id, -cost)
    await ctx.send(f"✅ Bought **{emoji} {match}** for **{cost} coins**! You now have the {role.mention} role.")

@bot.command(name="bhelp", aliases=["commands", "cmds"])
async def cmd_help(ctx: commands.Context):
    lines = [
        "**🪙 Bird Bot Prefix Commands** (`b <command>`)\n",
        "`b daily` / `b d` — Claim daily coins",
        "`b g <amount>` — Gamble (slots)",
        "`b cf <heads/tails> <amount>` — Coin flip",
        "`b work` / `b w` — Work for coins",
        "`b rob @user` — Rob someone 🦹",
        "`b bal [@user]` — Check balance",
        "`b give @user <amount>` — Give coins",
        "`b profile [@user]` — Full stats",
        "`b lb` — Leaderboard",
        "`b shop [item]` — Browse/buy shop",
        "\n*All commands also available as slash commands!*",
    ]
    await ctx.send("\n".join(lines))


# ── /help ─────────────────────────────────────────────────────────────────────

@bot.tree.command(name="help", description="See all available commands")
async def slash_help(interaction: discord.Interaction):
    label = RAGE_LABELS[RAGE_LEVEL]
    bar = "🟥" * RAGE_LEVEL + "⬛" * (10 - RAGE_LEVEL)
    embed = discord.Embed(
        title="🤖 Bird Bot — Command List",
        description=f"Current vibe: {bar} **{RAGE_LEVEL}/10 {label}**",
        color=discord.Color.blurple()
    )
    embed.add_field(name="💬 Chat", value=(
        "`/chat` — Chat with AI\n"
        "`/reset` — Clear conversation history\n"
        "`/ping` — Check bot latency"
    ), inline=False)
    embed.add_field(name="😂 Fun", value=(
        "`/joke [topic]` — Get a funny joke\n"
        "`/roast @user` — Roast someone\n"
        "`/compliment @user` — Compliment someone\n"
        "`/ship @user1 @user2` — Love compatibility\n"
        "`/wouldyourather` — Would you rather"
    ), inline=False)
    embed.add_field(name="🎲 Games", value=(
        "`/8ball <question>` — Magic 8-ball\n"
        "`/roll [sides] [times]` — Roll dice\n"
        "`/trivia [topic]` — Trivia question"
    ), inline=False)
    embed.add_field(name="🧠 Learn", value=(
        "`/fact [topic]` — Random fact\n"
        "`/story <topic>` — Short story\n"
        "`/advice [situation]` — Life advice"
    ), inline=False)
    embed.add_field(name="🎨 Image", value=(
        "`/image <prompt>` — Generate an AI image\n"
        "`/imagine @user` — AI portrait of a user\n"
        "`/meme <top> <bottom>` — Generate a meme\n"
        "`/avatar [style]` — Reimagine your avatar"
    ), inline=False)
    embed.add_field(name="🎵 Music", value=(
        "`/lyrics <song> [artist]` — Find song lyrics via Genius"
    ), inline=False)
    embed.add_field(name="💰 Economy (slash)", value=(
        "`/daily` · `/work` · `/rob @user`\n"
        "`/balance` · `/profile` · `/give @user`\n"
        "`/gamble` · `/coinflip` · `/leaderboard` · `/shop`"
    ), inline=False)
    embed.add_field(name="💰 Economy (prefix `b`)", value=(
        "`b daily` / `b d` — Claim daily\n"
        "`b g <amt>` — Slots · `b cf <h/t> <amt>` — Coinflip\n"
        "`b work` · `b rob @user` · `b give @user <amt>`\n"
        "`b bal` · `b profile` · `b lb` · `b shop [item]`\n"
        "`b bhelp` — Full prefix command list"
    ), inline=False)
    embed.add_field(name="📊 Tools", value=(
        "`/poll <question>` — Quick yes/no poll\n"
        "`/remindme <time> <msg>` — DM reminder\n"
        "`/translate <text> [lang]` — Translate\n"
        "`/summarize <text>` — AI summary"
    ), inline=False)
    embed.add_field(name="🎮 Đoán nhân vật", value=(
        "`/topic` — Chọn chủ đề & bắt đầu game\n"
        "`/ask <câu hỏi>` — Hỏi YES/NO về nhân vật\n"
        "`/guess <tên>` — Đoán nhân vật\n"
        "`/giveup` — Xem đáp án & bỏ cuộc"
    ), inline=False)
    embed.set_footer(text="Admin commands are hidden. You can also @mention me or DM me!")
    await interaction.response.send_message(embed=embed)


# ── Guessing Game ─────────────────────────────────────────────────────────────

GAME_TOPICS: dict[str, list[str]] = {
    "One Piece":       ["Luffy", "Zoro", "Nami", "Sanji", "Chopper", "Robin", "Franky", "Brook", "Shanks", "Blackbeard", "Ace", "Law", "Hancock", "Whitebeard", "Kaido"],
    "Naruto":          ["Naruto", "Sasuke", "Sakura", "Kakashi", "Itachi", "Gaara", "Hinata", "Jiraiya", "Tsunade", "Orochimaru", "Minato", "Obito", "Pain"],
    "Dragon Ball":     ["Goku", "Vegeta", "Gohan", "Piccolo", "Frieza", "Cell", "Beerus", "Broly", "Krillin", "Trunks", "Bulma", "Android 18"],
    "Demon Slayer":    ["Tanjiro", "Nezuko", "Zenitsu", "Inosuke", "Rengoku", "Muzan", "Shinobu", "Gyomei", "Sanemi", "Doma"],
    "Attack on Titan": ["Eren", "Mikasa", "Armin", "Levi", "Historia", "Zeke", "Reiner", "Annie", "Hange", "Erwin"],
}

# channel_id -> { topic, character, questions, active }
game_sessions: dict[int, dict] = {}


class TopicSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        for topic in GAME_TOPICS:
            self.add_item(TopicButton(topic))


class TopicButton(discord.ui.Button):
    def __init__(self, topic: str):
        super().__init__(label=topic, style=discord.ButtonStyle.primary, emoji="🎮")
        self.topic = topic

    async def callback(self, interaction: discord.Interaction):
        ch = interaction.channel_id
        character = random.choice(GAME_TOPICS[self.topic])
        game_sessions[ch] = {
            "topic": self.topic,
            "character": character,
            "questions": 0,
            "active": True,
        }
        embed = discord.Embed(
            title=f"🎮 Game bắt đầu! Chủ đề: {self.topic}",
            description=(
                "🤫 Tôi đã chọn một nhân vật bí mật!\n\n"
                "**Cách chơi:**\n"
                "`/ask <câu hỏi>` — Tôi trả lời YES/NO hoặc ngắn gọn\n"
                "`/guess <tên>` — Đoán tên nhân vật\n\n"
                "*Ví dụ hỏi: Nhân vật này có ăn trái ác quỷ không?*"
            ),
            color=discord.Color.purple()
        )
        embed.set_footer(text="Hỏi thật nhiều trước khi đoán nhé!")
        await interaction.response.edit_message(content=None, embed=embed, view=None)


@bot.tree.command(name="topic", description="Bắt đầu game đoán nhân vật anime! 🎮")
async def slash_topic(interaction: discord.Interaction):
    # End existing session if any
    if interaction.channel_id in game_sessions:
        game_sessions.pop(interaction.channel_id)
    embed = discord.Embed(
        title="🎮 Game Đoán Nhân Vật Anime",
        description="Chọn chủ đề bên dưới để bắt đầu!\nBot sẽ nghĩ đến một nhân vật, bạn hỏi và đoán.",
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed, view=TopicSelectView())


@bot.tree.command(name="ask", description="Hỏi về nhân vật bí mật (YES/NO hoặc gợi ý ngắn)")
@app_commands.describe(question="Câu hỏi của bạn về nhân vật")
async def slash_ask(interaction: discord.Interaction, question: str):
    ch = interaction.channel_id
    state = game_sessions.get(ch)
    if not state or not state.get("active"):
        await interaction.response.send_message(
            "❌ Chưa có game nào đang chạy! Dùng `/topic` để bắt đầu.", ephemeral=True
        )
        return

    state["questions"] += 1
    character = state["character"]
    topic = state["topic"]

    await interaction.response.defer(thinking=True)

    prompt = (
        f"Bạn đang chơi game đoán nhân vật. Nhân vật bí mật là '{character}' từ '{topic}'.\n"
        f"Người chơi hỏi: '{question}'\n\n"
        f"Quy tắc trả lời:\n"
        f"- Câu hỏi có/không → chỉ trả lời 'Có' hoặc 'Không'\n"
        f"- Câu hỏi về đặc điểm (tóc, mắt, vai trò...) → trả lời 1-4 từ (VD: 'Đen', 'Thuyền trưởng', 'Rất mạnh')\n"
        f"- TUYỆT ĐỐI không tiết lộ tên nhân vật\n"
        f"- Trả lời chính xác theo thông tin thật của nhân vật\n"
        f"- Chỉ trả lời ngắn gọn, không giải thích"
    )

    try:
        reply = await asyncio.get_event_loop().run_in_executor(None, ask_ai_once, prompt)
    except Exception:
        await interaction.followup.send("❌ Lỗi AI! Thử lại nhé.")
        return

    reply = reply.strip().split("\n")[0]  # chỉ lấy dòng đầu

    embed = discord.Embed(color=discord.Color.blue())
    embed.add_field(name=f"❓ Câu #{state['questions']}", value=f"*{question}*", inline=False)
    embed.add_field(name="💬 Trả lời", value=f"**{reply}**", inline=False)
    embed.set_footer(text=f"Topic: {topic} | Dùng /guess <tên> nếu đã biết!")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="guess", description="Đoán tên nhân vật bí mật! 🕵️")
@app_commands.describe(character="Tên nhân vật bạn đoán")
async def slash_guess(interaction: discord.Interaction, character: str):
    ch = interaction.channel_id
    state = game_sessions.get(ch)
    if not state or not state.get("active"):
        await interaction.response.send_message(
            "❌ Chưa có game nào đang chạy! Dùng `/topic` để bắt đầu.", ephemeral=True
        )
        return

    secret = state["character"]
    topic = state["topic"]

    if character.strip().lower() == secret.lower():
        game_sessions[ch]["active"] = False
        questions_used = state["questions"]
        if questions_used <= 5:
            reward = 200
        elif questions_used <= 10:
            reward = 100
        elif questions_used <= 20:
            reward = 50
        else:
            reward = 20
        new_bal = eco_add(interaction.user.id, reward)
        embed = discord.Embed(
            title="🎉 CHÍNH XÁC!!!",
            description=(
                f"{interaction.user.mention} đã đoán đúng!\n\n"
                f"Nhân vật bí mật là **{secret}** từ **{topic}**!\n"
                f"Số câu hỏi đã dùng: **{questions_used}** câu\n\n"
                f"💰 Phần thưởng: **+{reward} coins** → **{new_bal} coins**"
            ),
            color=discord.Color.green()
        )
        embed.set_footer(text="Dùng /topic để chơi ván mới!")
        await interaction.response.send_message(embed=embed)
    else:
        embed = discord.Embed(
            title="❌ Sai rồi!",
            description=f"**{character}** không phải nhân vật bí mật.\nHỏi thêm rồi thử lại!",
            color=discord.Color.red()
        )
        embed.set_footer(text=f"Đã đoán sai | Câu hỏi đã dùng: {state['questions']}")
        await interaction.response.send_message(embed=embed)

@bot.tree.command(name="giveup", description="Bỏ cuộc và xem đáp án 🏳️")
async def slash_giveup(interaction: discord.Interaction):
    ch = interaction.channel_id
    state = game_sessions.get(ch)
    if not state or not state.get("active"):
        await interaction.response.send_message("❌ Không có game nào đang chạy!", ephemeral=True)
        return
    secret = state["character"]
    topic = state["topic"]
    game_sessions.pop(ch)
    embed = discord.Embed(
        title="🏳️ Bỏ cuộc!",
        description=f"Nhân vật bí mật là **{secret}** từ **{topic}**.\nDùng `/topic` để chơi lại!",
        color=discord.Color.orange()
    )
    await interaction.response.send_message(embed=embed)

if __name__ == "__main__":
    
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)
