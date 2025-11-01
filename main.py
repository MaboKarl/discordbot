import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import asyncio
import yt_dlp
import random
import re

# -------------------- ENVIRONMENT --------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# -------------------- LOGGING --------------------
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')

# -------------------- BOT SETUP --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="*", intents=intents)

# -------------------- GENERAL EVENTS --------------------
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    content = message.content.lower()
    banned_words = ["faggot", "bitch", "nigger", "nigga", "idiot", "tangina", "gago", "bobo"]

    responses = [
        "you bad bad boy!",
        "You dirty boy!",
        "tsk tsk, hindi pwede yan!",
        "huwag ganun, please!"
    ]


    if any(re.search(rf"\b{word}\b", content, re.IGNORECASE) for word in banned_words):
        try:
            await message.delete()
        except discord.Forbidden:
            print(f"Can't delete message from {message.author}")
        await message.channel.send(f"{message.author.mention}, {random.choice(responses)}")
        return

    # Simple text reactions
    if content.startswith("*hello") or content.startswith("*hey"):
        await message.channel.send(f"Sup {message.author.mention}!")
        return
    elif content.startswith("*bye"):
        await message.channel.send("Bye 👋")
        return

    await bot.process_commands(message)

# -------------------- PIN COMMANDS --------------------
@bot.command(name="pin")
async def pin_message(ctx):
    pinned = False
    async for msg in ctx.channel.history(limit=50):
        if msg.id != ctx.message.id and not msg.author.bot and not msg.content.startswith(bot.command_prefix):
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

@bot.command(name="pinlist", aliases=["pin list"])
async def list_pins(ctx):
    pinned_messages = await ctx.channel.pins()
    if not pinned_messages:
        await ctx.send("No pinned messages in this channel.")
        return

    response = "**📌 Pinned Messages:**\n"
    for i, msg in enumerate(pinned_messages, start=1):
        preview = (msg.content[:50] + "...") if len(msg.content) > 50 else msg.content
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

queues = {}
loop_mode = {}

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
    data = await loop.run_in_executor(
        None,
        lambda: yt_dlp.YoutubeDL(YTDL_OPTIONS).extract_info(f"ytsearch:{search}", download=False)['entries'][0]
    )
    url = data['url']
    title = data.get('title', 'Unknown title')

    guild_id = ctx.guild.id
    if guild_id not in queues:
        queues[guild_id] = []
    queues[guild_id].append({"title": title, "url": url})

  
    if not ctx.voice_client.is_playing() and not ctx.voice_client.is_paused() and len(queues[guild_id]) == 1:
        await play_next(ctx)

    await ctx.send(f"🎶 Added to queue: {title}")

async def play_next(ctx):
    guild_id = ctx.guild.id
    if guild_id not in queues or len(queues[guild_id]) == 0:
        await ctx.voice_client.disconnect()
      
        del queues[guild_id]
        loop_mode.pop(guild_id, None)
        return

    song = queues[guild_id][0]  # don’t pop yet for loop mode
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

    if loop_mode.get(guild_id):
        pass  # replay same song
    else:
        if guild_id in queues and len(queues[guild_id]) > 0:
            queues[guild_id].pop(0)
    await play_next(ctx)

# -------------------- MUSIC CONTROLS --------------------
@bot.command(name="queue")
async def show_queue(ctx):
    guild_id = ctx.guild.id
    if guild_id not in queues or len(queues[guild_id]) == 0:
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
        await ctx.voice_client.disconnect()
        await ctx.send("🛑 Stopped music and cleared the queue.")
       
        del queues[guild_id]
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

# -------------------- ON READY --------------------
@bot.event
async def on_ready():
    print(f"{bot.user} is now online! ✅")

# -------------------- RUN BOT --------------------
bot.run(DISCORD_TOKEN, log_handler=handler, log_level=logging.DEBUG)
