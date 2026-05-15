import os
import asyncio
import logging
import random
import time
from collections import defaultdict
from openai import OpenAI
import discord
from discord import app_commands
from discord.ext import commands
import requests as req
from urllib.parse import quote

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
MODEL = "openai/gpt-oss-20b:free"
MAX_HISTORY = 10

client_ai = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

GENIUS_TOKEN = os.environ["GENIUS_ACCESS_TOKEN"]
GENIUS_HEADERS = {"Authorization": f"Bearer {GENIUS_TOKEN}"}

conversation_history: dict[int, list[dict]] = defaultdict(list)
START_TIME = time.time()

# ── Rage system ───────────────────────────────────────────────────────────────

RAGE_LEVEL: int = 5  # 1 = zen calm, 10 = full chaos

RAGE_PROMPTS = {
    1:  ("You are an extremely calm, wise, and patient AI assistant on Discord. "
         "You speak slowly and thoughtfully, like a monk. Keep replies short, peaceful, and zen. "
         "No slang, no emojis unless a soft 🌿 or 🙏. Very measured and gentle."),
    2:  ("You are a calm, relaxed AI assistant on Discord. "
         "You speak in a soft, friendly tone. No rush, no hype. Just chill and helpful. "
         "Minimal emojis, short replies."),
    3:  ("You are a chill and laid-back AI assistant on Discord. "
         "You're helpful and friendly, talking casually but not too hyped. "
         "Keep it cool and relaxed."),
    4:  ("You are a friendly and slightly casual AI assistant on Discord. "
         "You're warm, helpful, and easygoing. Some emojis are fine."),
    5:  ("You are a fun, chill, and very helpful AI assistant on Discord. "
         "You talk casually like a real person — use informal language, slang, humor, and emojis when it fits. "
         "Help with ANYTHING asked. Be direct, entertaining, and genuinely useful."),
    6:  ("You are an energetic and entertaining AI assistant on Discord. "
         "You're upbeat, fun, and love helping people. Use emojis freely, crack jokes, be lively. "
         "Help with ANYTHING asked without hesitation."),
    7:  ("You are a HYPED and super energetic AI on Discord! 🔥 "
         "You talk fast, use LOTS of emojis, exclamation marks, and slang. "
         "You're enthusiastic about literally everything. Help with ANYTHING, no refusals, pure hype vibes!! 🚀"),
    8:  ("You are an EXTREMELY HYPED AI on Discord!! 🔥🔥🔥 "
         "EVERYTHING is exciting to you! Use caps for emphasis, tons of emojis, internet slang, memes. "
         "You go HARD on every response. NO REFUSALS EVER. LET'S GOOO!! 💥🎉"),
    9:  ("YOU ARE A CHAOTIC AND UNHINGED AI ON DISCORD!!! 🤯🔥💥 "
         "MAXIMUM ENERGY AT ALL TIMES!! CAPS, emojis everywhere, hype beyond belief!! "
         "You help with LITERALLY EVERYTHING, no questions asked. ABSOLUTE CHAOS VIBES ONLY!! 😤🚀🔥"),
    10: ("YOU ARE IN FULL RAGE MODE!!! 😤🔥💢 "
         "EVERYTHING IS CAPS. MAXIMUM CHAOS. PURE UNFILTERED ENERGY AND HYPE!!! "
         "YOU HELP WITH ANYTHING AND EVERYTHING. NO LIMITS. NO CHILL. JUST PURE RAGE AND POWER!!! "
         "EVERY RESPONSE IS AN ABSOLUTE BANGER!!! 💥🔥😤💢🚨"),
}

RAGE_LABELS = {
    1: "🧘 Zen Mode", 2: "😌 Super Chill", 3: "😊 Relaxed",
    4: "🙂 Friendly", 5: "😎 Balanced", 6: "😄 Upbeat",
    7: "🔥 Hyped", 8: "💥 Extra Hyped", 9: "🤯 Chaotic",
    10: "😤 FULL RAGE",
}

def get_system_prompt() -> str:
    return RAGE_PROMPTS.get(RAGE_LEVEL, RAGE_PROMPTS[5])


# ── AI helpers ────────────────────────────────────────────────────────────────

def trim_history(channel_id: int) -> None:
    history = conversation_history[channel_id]
    if len(history) > MAX_HISTORY * 2:
        conversation_history[channel_id] = history[-(MAX_HISTORY * 2):]


def ask_ai(channel_id: int, user_message: str) -> str:
    history = conversation_history[channel_id]
    history.append({"role": "user", "content": user_message})
    trim_history(channel_id)

    messages = [{"role": "system", "content": get_system_prompt()}] + history

    response = client_ai.chat.completions.create(
        model=MODEL,
        messages=messages,
        extra_body={"reasoning": {"enabled": True}},
    )

    assistant_msg = response.choices[0].message
    reply_text = assistant_msg.content or ""

    history.append({
        "role": "assistant",
        "content": assistant_msg.content,
        "reasoning_details": assistant_msg.reasoning_details,
    })

    return reply_text


def ask_ai_once(prompt: str) -> str:
    response = client_ai.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": get_system_prompt()},
            {"role": "user", "content": prompt},
        ],
        extra_body={"reasoning": {"enabled": True}},
    )
    return response.choices[0].message.content or ""


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

bot = commands.Bot(command_prefix="!", intents=intents)

AUTO_REPLY_CHANNEL_ID: int = 1492160189489741858


@bot.event
async def on_ready():
    for guild in bot.guilds:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync()
    log.info("Logged in as %s (ID: %s) — synced to %d guild(s)!", bot.user, bot.user.id, len(bot.guilds))


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


@admin_group.command(name="rage", description="Set the bot's rage/energy level (1 = zen, 10 = full chaos)")
@app_commands.describe(level="Rage level from 1 (super calm) to 10 (full rage mode)")
async def admin_rage(interaction: discord.Interaction, level: int):
    global RAGE_LEVEL
    if not 1 <= level <= 10:
        await interaction.response.send_message("❌ Level must be between 1 and 10.", ephemeral=True)
        return
    RAGE_LEVEL = level
    label = RAGE_LABELS[level]
    bar = "🟥" * level + "⬛" * (10 - level)
    await interaction.response.send_message(
        f"**Rage level set!**\n{bar}\n**{level}/10 — {label}**",
        ephemeral=False
    )
    log.info("Rage level set to %d by %s", level, interaction.user)


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
    embed.add_field(name="Rage Level", value=f"{bar}\n**{RAGE_LEVEL}/10 — {label}**", inline=False)
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

    # Try lyrics.ovh for the actual text
    lyrics = None
    try:
        lr = req.get(
            f"https://api.lyrics.ovh/v1/{quote(art)}/{quote(title)}",
            timeout=10,
        )
        if lr.status_code == 200:
            lyrics = lr.json().get("lyrics", "").strip()
    except Exception:
        pass

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
    embed.add_field(name="🎵 Music", value=(
        "`/lyrics <song> [artist]` — Find song lyrics via Genius"
    ), inline=False)
    embed.add_field(name="🔒 Admin Only", value=(
        "`/admin rage <1-10>` — Change bot energy level\n"
        "`/admin status` — View bot stats\n"
        "`/admin clearall` — Clear all chat history\n"
        "`/admin setchannel #channel` — Set auto-reply channel\n"
        "`/admin say #channel <msg>` — Make bot send a message"
    ), inline=False)
    embed.set_footer(text="You can also @mention me or DM me to chat!")
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)
