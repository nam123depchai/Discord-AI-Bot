import os
import asyncio
import logging
from collections import defaultdict
from openai import OpenAI
import discord
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

# Per-channel conversation history
conversation_history: dict[int, list[dict]] = defaultdict(list)

SYSTEM_PROMPT = (
    "You are a helpful, friendly, and knowledgeable AI assistant on Discord. "
    "Keep your replies concise and well-formatted for chat. "
    "Use markdown sparingly (Discord supports bold, italics, code blocks)."
)


def trim_history(channel_id: int) -> None:
    history = conversation_history[channel_id]
    if len(history) > MAX_HISTORY * 2:
        conversation_history[channel_id] = history[-(MAX_HISTORY * 2) :]


def ask_ai(channel_id: int, user_message: str) -> str:
    history = conversation_history[channel_id]

    history.append({"role": "user", "content": user_message})
    trim_history(channel_id)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    response = client_ai.chat.completions.create(
        model=MODEL,
        messages=messages,
        extra_body={"reasoning": {"disabled": True}},
    )

    assistant_msg = response.choices[0].message
    reply_text = assistant_msg.content or ""

    history.append(
        {
            "role": "assistant",
            "content": assistant_msg.content,
            "reasoning_details": assistant_msg.reasoning_details,
        }
    )

    return reply_text


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    log.info("Bot is ready and online!")


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
    for mention in message.mentions:
        content = content.replace(f"<@{mention.id}>", "").replace(
            f"<@!{mention.id}>", ""
        )
    content = content.strip()

    if not content:
        await message.reply("Hey! How can I help you?")
        return

    channel_id = message.channel.id
    async with message.channel.typing():
        try:
            reply = await asyncio.get_event_loop().run_in_executor(
                None, ask_ai, channel_id, content
            )
        except Exception as exc:
            log.exception("Error calling OpenRouter: %s", exc)
            await message.reply(
                "Sorry, I ran into an error. Please try again in a moment."
            )
            return

    if not reply:
        await message.reply("I got an empty response from the AI. Please try again.")
        return

    if len(reply) > 2000:
        for i in range(0, len(reply), 2000):
            await message.channel.send(reply[i : i + 2000])
    else:
        await message.reply(reply)


@bot.command(name="chat")
async def chat_command(ctx: commands.Context, *, message: str):
    """Chat with the AI using the !chat command."""
    channel_id = ctx.channel.id
    async with ctx.typing():
        try:
            reply = await asyncio.get_event_loop().run_in_executor(
                None, ask_ai, channel_id, message
            )
        except Exception as exc:
            log.exception("Error calling OpenRouter: %s", exc)
            await ctx.reply("Sorry, I ran into an error. Please try again in a moment.")
            return

    if not reply:
        await ctx.reply("I got an empty response from the AI. Please try again.")
        return

    if len(reply) > 2000:
        for i in range(0, len(reply), 2000):
            await ctx.send(reply[i : i + 2000])
    else:
        await ctx.reply(reply)


@bot.command(name="reset")
async def reset_command(ctx: commands.Context):
    """Clear conversation history for this channel."""
    conversation_history[ctx.channel.id].clear()
    await ctx.reply("Conversation history cleared!")


@bot.command(name="ping")
async def ping_command(ctx: commands.Context):
    """Check if the bot is alive."""
    await ctx.reply(f"Pong! Latency: {round(bot.latency * 1000)}ms")


if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)
