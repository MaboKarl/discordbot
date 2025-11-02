import os
import re
import random
import asyncio
import logging
import json
from dotenv import load_dotenv

import discord
from discord.ext import commands
import yt_dlp
import aiohttp

# -------------------- ENVIRONMENT --------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")  # OpenRouter key

# -------------------- LOGGING SETUP --------------------
if not os.path.exists("discord.log"):
    with open("discord.log", "w", encoding="utf-8"):
        pass

logger = logging.getLogger("binky")
logger.setLevel(logging.DEBUG)

file_handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="a")
file_handler.setLevel(logging.DEBUG)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

formatter = logging.Formatter("[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(console_handler)

# -------------------- BOT SETUP --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="*", intents=intents)

# -------------------- GLOBALS --------------------
queues = {}            # guild_id -> list of songs
loop_mode = {}         # guild_id -> bool

# conversation_memory structure:
# { "channel_id": { "user_id": { "history": [ {"role":"user"/"assistant","content": "..."} , ... ],
#                                "persona": "some persona string" } , ... }, ... }
conversation_memory = {}
MEMORY_FILE = "memory.json"

# Controls
HISTORY_MESSAGE_LIMIT = 200  # per user per channel keep last N messages (user+assistant entries count separately)

# -------------------- MEMORY HELPERS --------------------
def save_memory():
    """Save conversation memory to a JSON file."""
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(conversation_memory, f, ensure_ascii=False, indent=2)
        logger.debug("Memory saved to %s", MEMORY_FILE)
    except Exception as e:
        logger.exception("Failed to save memory: %s", e)

def load_memory():
    """Load saved conversations from disk (if present)."""
    global conversation_memory
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Basic validation: ensure dict structure
                if isinstance(data, dict):
                    conversation_memory = data
                    logger.info("Memory loaded: %d channel keys", len(conversation_memory))
                else:
                    logger.warning("memory.json malformed: root is not a dict. Starting fresh.")
                    conversation_memory = {}
        except Exception as e:
            logger.exception("Failed to load memory: %s", e)
            conversation_memory = {}
    else:
        logger.info("No memory file found; starting with empty memory.")
        conversation_memory = {}

async def autosave():
    """Auto-save loop to persist memory periodically."""
    while True:
        save_memory()
        await asyncio.sleep(300)  # every 5 minutes

def ensure_user_channel_slot(channel_id: str, user_id: str):
    """Ensure nested dicts exist for channel and user with default persona."""
    if channel_id not in conversation_memory:
        conversation_memory[channel_id] = {}
    if user_id not in conversation_memory[channel_id]:
        conversation_memory[channel_id][user_id] = {"history": [], "persona": ""}

def trim_history(channel_id: str, user_id: str):
    """Trim history to the allowed limit per user/channel."""
    hist = conversation_memory[channel_id][user_id]["history"]
    if len(hist) > HISTORY_MESSAGE_LIMIT:
        # keep the most recent items
        conversation_memory[channel_id][user_id]["history"] = hist[-HISTORY_MESSAGE_LIMIT:]

# -------------------- HELPERS --------------------
def clean_openrouter_output(text: str) -> str:
    """Strip common wrappers and whitespace."""
    if not text:
        return ""
    cleaned = re.sub(r"<s>\s*\[OUT\]\s*|\s*\[/OUT\]\s*</s>", "", text)
    return cleaned.strip()

# -------------------- AI CALL --------------------
async def call_ai_api(messages, timeout_seconds: int = 30):
    """
    messages: list of {"role": "...", "content": "..."} compatible with OpenRouter
    Returns: string (reply) or an error note string.
    """
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "openai/gpt-oss-20b:free",  # change model if needed
        "messages": messages
    }

    try:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=data) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.warning("AI API returned non-200 status %s: %s", resp.status, text[:400])
                    return f"⚠️ API Error {resp.status}: {text[:200]}"
                # parse json
                try:
                    res = await resp.json()
                except Exception as e:
                    logger.exception("Failed to parse AI response JSON: %s", e)
                    return f"⚠️ API returned invalid JSON: {text[:200]}"

                # defensive extraction
                try:
                    content = res.get("choices", [{}])[0].get("message", {}).get("content", "")
                except Exception:
                    content = ""

                content = clean_openrouter_output(content or "")
                if not content:
                    logger.warning("AI returned empty content. Full raw response (truncated): %s", str(res)[:400])
                    return "⚠️ The AI didn’t return a response."
                return content
    except asyncio.TimeoutError:
        logger.exception("AI API call timed out.")
        return "⚠️ The AI request timed out; try again."
    except Exception as e:
        logger.exception("Unexpected error calling AI API: %s", e)
        return f"⚠️ Error contacting AI: {e}"

# -------------------- EVENTS --------------------
@bot.event
async def on_ready():
    logger.info("%s is now online! ✅", bot.user)
    # attach discord internal logs to our file/console handlers
    discord_logger = logging.getLogger("discord")
    discord_logger.setLevel(logging.DEBUG)
    discord_logger.addHandler(file_handler)
    discord_logger.addHandler(console_handler)
    # start autosave
    bot.loop.create_task(autosave())

@bot.event
async def on_message(message):
    # Basic moderation & prefix shortcuts
    if message.author == bot.user:
        return

    content = message.content or ""
    lower = content.lower()

    banned_words = ["faggot", "bitch", "nigger", "nigga", "idiot", "tangina", "gago", "bobo"]
    if any(re.search(rf"\b{re.escape(word)}\b", lower, re.IGNORECASE) for word in banned_words):
        try:
            await message.delete()
            await message.channel.send(f"{message.author.mention}, {random.choice(['you bad bad boy!', 'You dirty boy!', 'tsk tsk, hindi pwede yan!', 'huwag ganun, please!'])}")
            logger.info("Deleted banned message from %s: %s", message.author, content[:100])
        except discord.Forbidden:
            logger.warning("Missing permission to delete message from %s", message.author)
        except Exception:
            logger.exception("Failed to delete or notify about banned message.")
        return

    # simple text triggers
    if lower.startswith("*hello") or lower.startswith("*hey"):
        await message.channel.send(f"Sup {message.author.mention}!")
        return
    if lower.startswith("*bye"):
        await message.channel.send("Bye 👋")
        return

    await bot.process_commands(message)

# -------------------- PIN COMMANDS --------------------
@bot.command(name="pin")
async def pin_message(ctx):
    pinned = False
    async for msg in ctx.channel.history(limit=50):
        if msg.id != ctx.message.id and not msg.author.bot and not (msg.content and msg.content.startswith(bot.command_prefix)):
            try:
                await msg.pin()
                await ctx.send(f"Pinned the message above, {ctx.author.mention}! 📌")
                pinned = True
            except discord.Forbidden:
                await ctx.send("I don't have permission to pin messages.")
            except discord.HTTPException:
                await ctx.send("Something went wrong while trying to pin.")
            break
    if not pinned:
        await ctx.send("No suitable user messages found to pin!")

@bot.command(name="pinlist", aliases=["pins"])
async def list_pins(ctx):
    pinned_messages = await ctx.channel.pins()
    if not pinned_messages:
        await ctx.send("No pinned messages in this channel.")
        return

    response = "**📌 Pinned Messages:**\n"
    for i, msg in enumerate(pinned_messages, start=1):
        preview = (msg.content[:50] + "...") if msg.content and len(msg.content) > 50 else (msg.content or "[embed/attachment]")
        response += f"{i}️⃣ {preview}\n"
    await ctx.send(response)

@bot.command(name="unpin")
async def unpin_message(ctx, number: int = None):
    pinned_messages = await ctx.channel.pins()
    if not pinned_messages:
        await ctx.send("No pinned messages to unpin.")
        return

    if number is None or number < 1 or number > len(pinned_messages):
        await ctx.send("Please specify a valid message number to unpin (e.g., `*unpin 2`).")
        return

    message_to_unpin = pinned_messages[number - 1]
    try:
        await message_to_unpin.unpin()
        await ctx.send(f"Unpinned message #{number}, {ctx.author.mention}.")
    except discord.Forbidden:
        await ctx.send("I don't have permission to unpin messages.")
    except discord.HTTPException:
        await ctx.send("Something went wrong while trying to unpin.")

# -------------------- MUSIC BOT --------------------
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "geo_bypass": True,
    "force_ipv4": True,
    "nocheckcertificate": True
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn"
}

@bot.command(name="play")
async def play(ctx, *, search: str):
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("You must be in a voice channel to play music!")
        return

    voice_channel = ctx.author.voice.channel
    if ctx.voice_client is None:
        await voice_channel.connect()
    elif ctx.voice_client.channel != voice_channel:
        await ctx.voice_client.move_to(voice_channel)

    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(
            None,
            lambda: yt_dlp.YoutubeDL(YTDL_OPTIONS).extract_info(f"ytsearch:{search}", download=False)['entries'][0]
        )
    except Exception as e:
        logger.exception("yt-dlp failed: %s", e)
        await ctx.send("❌ Could not find that song.")
        return

    url = data.get('url')
    title = data.get('title', 'Unknown title')

    guild_id = ctx.guild.id
    queues.setdefault(guild_id, []).append({"title": title, "url": url})

    if not ctx.voice_client.is_playing() and not ctx.voice_client.is_paused() and len(queues[guild_id]) == 1:
        await play_next(ctx)

    await ctx.send(f"🎶 Added to queue: {title}")

async def play_next(ctx):
    guild_id = ctx.guild.id
    if guild_id not in queues or not queues[guild_id]:
        if ctx.voice_client:
            try:
                await ctx.voice_client.disconnect()
            except Exception:
                logger.exception("Error disconnecting voice client.")
        queues.pop(guild_id, None)
        loop_mode.pop(guild_id, None)
        return

    song = queues[guild_id][0]
    source = discord.FFmpegPCMAudio(song['url'], **FFMPEG_OPTIONS)
    ctx.voice_client.play(
        source,
        after=lambda e: asyncio.run_coroutine_threadsafe(after_song(ctx), bot.loop)
    )
    embed = discord.Embed(title="🎵 Now playing:", description=song['title'], color=0x1DB954)
    await ctx.send(embed=embed)

async def after_song(ctx):
    guild_id = ctx.guild.id
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        return

    if not loop_mode.get(guild_id):
        if queues.get(guild_id):
            queues[guild_id].pop(0)
    await play_next(ctx)

@bot.command(name="queue")
async def show_queue(ctx):
    guild_id = ctx.guild.id
    if not queues.get(guild_id):
        await ctx.send("🎶 The queue is empty!")
        return
    response = "**🎵 Current Queue:**\n"
    for i, song in enumerate(queues[guild_id], start=1):
        response += f"{i}. 🎶 {song['title']}\n"
    await ctx.send(response)

@bot.command()
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸ Music paused.")
    else:
        await ctx.send("Nothing is playing right now!")

@bot.command()
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶ Music resumed.")
    else:
        await ctx.send("Music isn’t paused!")

@bot.command(name="loop")
async def toggle_loop(ctx):
    guild_id = ctx.guild.id
    loop_mode[guild_id] = not loop_mode.get(guild_id, False)
    status = "🔁 Loop enabled." if loop_mode[guild_id] else "➡️ Loop disabled."
    await ctx.send(status)

@bot.command(name="skip")
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭ Skipped the current song!")
    else:
        await ctx.send("No music is playing to skip!")

@bot.command(name="stop")
async def stop(ctx):
    guild_id = ctx.guild.id
    if ctx.voice_client:
        queues[guild_id] = []
        ctx.voice_client.stop()
        try:
            await ctx.voice_client.disconnect()
        except Exception:
            logger.exception("Error disconnecting after stop.")
        await ctx.send("🛑 Stopped music and cleared the queue.")
        queues.pop(guild_id, None)
        loop_mode.pop(guild_id, None)
    else:
        await ctx.send("No music is playing to stop!")

# -------------------- CHOOSE COMMAND --------------------
@bot.command(name="choose")
async def choose_option(ctx, *, choices: str = None):
    if not choices:
        await ctx.send("❓ Please ask in this format: `*choose option1 or option2`")
        return
    choices = choices.replace(",", " or ")
    parts = [part.strip() for part in choices.split(" or ") if part.strip()]
    if len(parts) < 2:
        await ctx.send("❗ You need to provide at least two options (e.g., `*choose cat or dog`).")
        return
    choice = random.choice(parts)
    await ctx.send(f"🎲 I choose **{choice}**!")

# -------------------- Rprompt (Persona) --------------------
@bot.command(name="Rprompt")
async def set_persona(ctx, *, prompt: str = None):
    if not prompt:
        await ctx.send("🧠 Please provide a persona! Example: `*Rprompt you are a flirty knight from medieval times`")
        return
    channel_key = str(ctx.channel.id)
    user_key = str(ctx.author.id)
    ensure_user_channel_slot(channel_key, user_key)
    conversation_memory[channel_key][user_key]["persona"] = prompt
    save_memory()
    await ctx.send(f"✅ Persona set for {ctx.author.name} in this channel: `{prompt}`")
    logger.info("Persona set for %s in channel %s: %s", ctx.author, channel_key, prompt)

# -------------------- ASK (general AI) --------------------
@bot.command(name="ask")
async def ask_ai(ctx, *, prompt: str = None):
    if not prompt:
        await ctx.send("🧠 Ask me something! Example: `*ask how do computers think?`")
        return

    channel_key = str(ctx.channel.id)
    user_key = str(ctx.author.id)
    ensure_user_channel_slot(channel_key, user_key)

    persona = conversation_memory[channel_key][user_key].get("persona") or f"{ctx.author.name}'s assistant"
    history = conversation_memory[channel_key][user_key].get("history", [])

    # Build messages: system persona + entire history (to provide continuous memory) + user message
    messages = []
    if persona:
        messages.append({"role": "system", "content": f"You are {persona}. Respond in that style."})
    # include the entire stored history (may be trimmed periodically)
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    async with ctx.typing():
        reply = await call_ai_api(messages)

    if not reply or not reply.strip():
        reply = "⚠️ The AI returned an empty response."

    # update and persist history
    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": reply})
    conversation_memory[channel_key][user_key]["history"] = history[-HISTORY_MESSAGE_LIMIT:]
    save_memory()

    await ctx.send(reply[:2000])

# -------------------- ROLEPLAY --------------------
@bot.command(name="roleplay")
async def roleplay(ctx, *, message: str = None):
    if not message:
        await ctx.send("🎭 Say something to start roleplaying! Example: `*roleplay Hello there!`")
        return

    channel_key = str(ctx.channel.id)
    user_key = str(ctx.author.id)
    ensure_user_channel_slot(channel_key, user_key)

    persona = conversation_memory[channel_key][user_key].get("persona") or "a friendly assistant"
    history = conversation_memory[channel_key][user_key].get("history", [])

    messages = [{"role": "system", "content": f"You are {persona}. Stay fully in character and follow the persona's tone and behavior."}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})

    async with ctx.typing():
        reply = await call_ai_api(messages)

    if not reply or not reply.strip():
        reply = "⚠️ The AI didn’t respond properly."

    # update history and save
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": reply})
    conversation_memory[channel_key][user_key]["history"] = history[-HISTORY_MESSAGE_LIMIT:]
    save_memory()

    await ctx.send(reply[:2000])

# -------------------- ADMIN / UTILITY MEMORY COMMANDS --------------------
@bot.command(name="forget")
async def forget_memory(ctx, target: str = None):
    """
    Usage:
      *forget           -> clear your own memory in this channel
      *forget @user    -> clear mentioned user's memory (needs Manage Messages)
      *forget all      -> clear entire channel memory (needs Manage Guild)
    """
    channel_key = str(ctx.channel.id)

    # clear everyone's memory in this channel (admin)
    if target and target.lower() == "all":
        if not ctx.author.guild_permissions.manage_guild:
            await ctx.send("You need Manage Guild permission to clear all channel memories.")
            return
        conversation_memory.pop(channel_key, None)
        save_memory()
        await ctx.send("🗑️ Cleared all memory for this channel.")
        return

    # if a member was mentioned: clear that user's memory (admin)
    if ctx.message.mentions:
        target_user = ctx.message.mentions[0]
        if not ctx.author.guild_permissions.manage_messages:
            await ctx.send("You need Manage Messages permission to clear another user's memory.")
            return
        ensure_user_channel_slot(channel_key, str(target_user.id))
        conversation_memory[channel_key].pop(str(target_user.id), None)
        save_memory()
        await ctx.send(f"🗑️ Cleared memory for {target_user.mention} in this channel.")
        return

    # otherwise clear caller's memory in this channel
    user_key = str(ctx.author.id)
    ensure_user_channel_slot(channel_key, user_key)
    conversation_memory[channel_key].pop(user_key, None)
    save_memory()
    await ctx.send("🗑️ I have forgotten your conversation in this channel.")

@bot.command(name="recall")
async def recall_memory(ctx, limit: int = 10):
    """Show a short summary of what the bot remembers for you in this channel."""
    channel_key = str(ctx.channel.id)
    user_key = str(ctx.author.id)
    ensure_user_channel_slot(channel_key, user_key)
    history = conversation_memory[channel_key][user_key].get("history", [])
    if not history:
        await ctx.send("🧠 I don't remember anything for you in this channel yet.")
        return

    # show last N messages (roles + truncated content)
    recent = history[-limit:]
    formatted = []
    for m in recent:
        role = m.get("role", "unknown").capitalize()
        content = m.get("content", "")
        snippet = content.replace("\n", " ")[:180]
        formatted.append(f"**{role}:** {snippet}")
    await ctx.send("🧾 Recent memory:\n" + "\n".join(formatted))

# -------------------- RUN --------------------
if __name__ == "__main__":
    load_memory()
    try:
        bot.run(DISCORD_TOKEN, log_level=logging.DEBUG)
    except Exception as e:
        logger.exception("Bot failed to start: %s", e)
