import os
import asyncio
import logging
import random
from collections import defaultdict
from openai import OpenAI
import discord
from discord import app_commands
from discord.ext import commands

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

conversation_history: dict[int, list[dict]] = defaultdict(list)

SYSTEM_PROMPT = (
    "You are a helpful, friendly, and knowledgeable AI assistant on Discord. "
    "Keep your replies concise and well-formatted for chat. "
    "Use markdown sparingly (Discord supports bold, italics, code blocks)."
)


def trim_history(channel_id: int) -> None:
    history = conversation_history[channel_id]
    if len(history) > MAX_HISTORY * 2:
        conversation_history[channel_id] = history[-(MAX_HISTORY * 2):]


def ask_ai(channel_id: int, user_message: str, system: str = SYSTEM_PROMPT) -> str:
    history = conversation_history[channel_id]
    history.append({"role": "user", "content": user_message})
    trim_history(channel_id)

    messages = [{"role": "system", "content": system}] + history

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


def ask_ai_once(prompt: str, system: str = SYSTEM_PROMPT) -> str:
    """Single-shot AI call with no history."""
    response = client_ai.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
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


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    for guild in bot.guilds:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    log.info("Logged in as %s (ID: %s) — slash commands synced to %d guild(s)!", bot.user, bot.user.id, len(bot.guilds))


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)

    mentioned = bot.user in message.mentions
    is_dm = isinstance(message.channel, discord.DMChannel)
    if not (mentioned or is_dm):
        return

    content = message.content
    for m in message.mentions:
        content = content.replace(f"<@{m.id}>", "").replace(f"<@!{m.id}>", "")
    content = content.strip()

    if not content:
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

    if len(reply) > 2000:
        for i in range(0, len(reply), 2000):
            await message.channel.send(reply[i:i + 2000])
    else:
        await message.reply(reply)


# ── /chat ────────────────────────────────────────────────────────────────────

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


# ── /reset ───────────────────────────────────────────────────────────────────

@bot.tree.command(name="reset", description="Clear AI conversation history in this channel")
async def slash_reset(interaction: discord.Interaction):
    conversation_history[interaction.channel_id].clear()
    await interaction.response.send_message("🧹 Conversation history cleared!")


# ── /ping ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="ping", description="Check the bot's latency")
async def slash_ping(interaction: discord.Interaction):
    ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! Latency: **{ms}ms**")


# ── /joke ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="joke", description="Get a funny joke from the AI")
@app_commands.describe(topic="Optional topic for the joke (e.g. cats, programming, food)")
async def slash_joke(interaction: discord.Interaction, topic: str = "anything"):
    await interaction.response.defer(thinking=True)
    prompt = f"Tell me a single short, funny joke about {topic}. Just the joke, no intro."
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, ask_ai_once, prompt
        )
    except Exception:
        await interaction.followup.send("Couldn't think of a joke right now!")
        return
    await interaction.followup.send(f"😂 {reply}")


# ── /roast ───────────────────────────────────────────────────────────────────

@bot.tree.command(name="roast", description="Roast someone (all in good fun!)")
@app_commands.describe(user="The user to roast")
async def slash_roast(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(thinking=True)
    prompt = (
        f"Give a short, playful, funny roast for someone named {user.display_name}. "
        "Keep it light-hearted and not genuinely mean. Max 2 sentences."
    )
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, ask_ai_once, prompt
        )
    except Exception:
        await interaction.followup.send("Couldn't come up with a roast!")
        return
    await interaction.followup.send(f"🔥 {user.mention} {reply}")


# ── /compliment ──────────────────────────────────────────────────────────────

@bot.tree.command(name="compliment", description="Give someone a nice compliment")
@app_commands.describe(user="The user to compliment")
async def slash_compliment(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(thinking=True)
    prompt = (
        f"Give a genuine, warm, creative compliment for someone named {user.display_name}. "
        "Max 2 sentences. Be sincere and uplifting."
    )
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, ask_ai_once, prompt
        )
    except Exception:
        await interaction.followup.send("Couldn't come up with a compliment!")
        return
    await interaction.followup.send(f"💖 {user.mention} {reply}")


# ── /8ball ───────────────────────────────────────────────────────────────────

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
    answer = random.choice(EIGHT_BALL)
    await interaction.response.send_message(
        f"🎱 **{question}**\n> {answer}"
    )


# ── /roll ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="roll", description="Roll a dice")
@app_commands.describe(sides="Number of sides (default 6)", times="How many dice to roll (default 1)")
async def slash_roll(interaction: discord.Interaction, sides: int = 6, times: int = 1):
    times = min(times, 20)
    sides = max(2, min(sides, 1000))
    results = [random.randint(1, sides) for _ in range(times)]
    total = sum(results)
    rolls_str = ", ".join(str(r) for r in results)
    msg = f"🎲 Rolling {times}d{sides}: **{rolls_str}**"
    if times > 1:
        msg += f"\nTotal: **{total}**"
    await interaction.response.send_message(msg)


# ── /ship ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="ship", description="Check the love compatibility between two users 💘")
@app_commands.describe(user1="First person", user2="Second person")
async def slash_ship(interaction: discord.Interaction, user1: discord.Member, user2: discord.Member):
    seed = abs(hash(f"{min(user1.id, user2.id)}{max(user1.id, user2.id)}")) % 101
    bar_filled = round(seed / 10)
    bar = "💗" * bar_filled + "🖤" * (10 - bar_filled)
    if seed >= 80:
        verdict = "A match made in heaven! 🥰"
    elif seed >= 60:
        verdict = "Pretty good vibes! 😊"
    elif seed >= 40:
        verdict = "Could work with some effort! 🤔"
    elif seed >= 20:
        verdict = "Hmm... it's complicated. 😬"
    else:
        verdict = "Yikes. Maybe just friends? 😅"
    await interaction.response.send_message(
        f"💘 **Shipping {user1.display_name} & {user2.display_name}**\n"
        f"{bar} **{seed}%**\n{verdict}"
    )


# ── /trivia ──────────────────────────────────────────────────────────────────

@bot.tree.command(name="trivia", description="Get a random trivia question")
@app_commands.describe(topic="Optional topic (e.g. science, history, sports)")
async def slash_trivia(interaction: discord.Interaction, topic: str = "random"):
    await interaction.response.defer(thinking=True)
    prompt = (
        f"Give me one interesting trivia question about {topic} with 4 multiple choice options (A, B, C, D) "
        "and tell me which one is correct at the end. Format it nicely."
    )
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, ask_ai_once, prompt
        )
    except Exception:
        await interaction.followup.send("Couldn't fetch a trivia question right now!")
        return
    await send_long(interaction, f"🧠 **Trivia Time!**\n\n{reply}")


# ── /story ───────────────────────────────────────────────────────────────────

@bot.tree.command(name="story", description="Generate a short fun story")
@app_commands.describe(topic="What should the story be about?")
async def slash_story(interaction: discord.Interaction, topic: str):
    await interaction.response.defer(thinking=True)
    prompt = (
        f"Write a short, fun, creative story (3-5 sentences) about: {topic}. "
        "Make it entertaining and imaginative."
    )
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, ask_ai_once, prompt
        )
    except Exception:
        await interaction.followup.send("Couldn't write a story right now!")
        return
    await send_long(interaction, f"📖 **Story Time!**\n\n{reply}")


# ── /wouldyourather ──────────────────────────────────────────────────────────

@bot.tree.command(name="wouldyourather", description="Get a fun 'Would You Rather' question")
async def slash_wyr(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    prompt = (
        "Give me one creative and fun 'Would You Rather' question with two interesting options. "
        "Format it as: Would you rather [option A] OR [option B]?"
    )
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, ask_ai_once, prompt
        )
    except Exception:
        await interaction.followup.send("Couldn't think of a question!")
        return
    await interaction.followup.send(f"🤔 {reply}\n\nReact with 🅰️ or 🅱️!")


# ── /fact ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="fact", description="Get a random interesting fact")
@app_commands.describe(topic="Optional topic (e.g. space, animals, history)")
async def slash_fact(interaction: discord.Interaction, topic: str = "anything"):
    await interaction.response.defer(thinking=True)
    prompt = f"Give me one fascinating, surprising fact about {topic}. Keep it to 2-3 sentences."
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, ask_ai_once, prompt
        )
    except Exception:
        await interaction.followup.send("Couldn't fetch a fact right now!")
        return
    await interaction.followup.send(f"💡 **Fun Fact!**\n{reply}")


# ── /advice ──────────────────────────────────────────────────────────────────

@bot.tree.command(name="advice", description="Get some life advice or wisdom")
@app_commands.describe(situation="Optional situation you want advice about")
async def slash_advice(interaction: discord.Interaction, situation: str = "life in general"):
    await interaction.response.defer(thinking=True)
    prompt = f"Give thoughtful, practical advice about: {situation}. Keep it to 2-3 sentences."
    try:
        reply = await asyncio.get_event_loop().run_in_executor(
            None, ask_ai_once, prompt
        )
    except Exception:
        await interaction.followup.send("Couldn't come up with advice right now!")
        return
    await interaction.followup.send(f"🌟 **Advice**\n{reply}")


# ── /help ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="help", description="See all available commands")
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 Bird Bot — Command List",
        description="Here's everything I can do! All commands start with `/`",
        color=discord.Color.blurple()
    )
    embed.add_field(name="💬 Chat", value=(
        "`/chat` — Chat with AI (remembers context)\n"
        "`/reset` — Clear conversation history\n"
        "`/ping` — Check bot latency"
    ), inline=False)
    embed.add_field(name="😂 Fun", value=(
        "`/joke [topic]` — Get a funny joke\n"
        "`/roast @user` — Playfully roast someone\n"
        "`/compliment @user` — Give someone a compliment\n"
        "`/ship @user1 @user2` — Check love compatibility\n"
        "`/wouldyourather` — Would you rather question"
    ), inline=False)
    embed.add_field(name="🎲 Games", value=(
        "`/8ball <question>` — Ask the magic 8-ball\n"
        "`/roll [sides] [times]` — Roll dice\n"
        "`/trivia [topic]` — Random trivia question"
    ), inline=False)
    embed.add_field(name="🧠 Learn", value=(
        "`/fact [topic]` — Random interesting fact\n"
        "`/story <topic>` — Generate a short story\n"
        "`/advice [situation]` — Get life advice"
    ), inline=False)
    embed.set_footer(text="You can also @mention me or DM me to chat!")
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)
