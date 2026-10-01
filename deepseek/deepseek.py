"""DeepSeek — talk to the DeepSeek API from Discord.

Two ways to use it:

* The ``ai`` command:   ``!ai what is the capital of France?``
* Natural language:     mention the bot, reply to one of its messages, or use a
  configured AI channel where no prefix is needed.

The API key is read from Red's shared API tokens (service name ``deepseek``)::

    !set api deepseek api_key <your key>

Models: ``deepseek-flash`` (fast) and ``deepseek-v4-pro`` (reasoning).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import pagify

log = logging.getLogger("red.cogs.deepseek")

API_URL = "https://api.deepseek.com/chat/completions"
MODELS = ("deepseek-flash", "deepseek-v4-pro")
DEFAULT_SYSTEM = (
    "You are Nebula, a helpful, friendly Discord assistant. Be concise. "
    "Prefer short paragraphs and Discord markdown. If you are unsure, say so."
)
MAX_PROMPT_CHARS = 4000
COOLDOWN_SECONDS = 3


class MissingKey(Exception):
    """Raised when no DeepSeek API key has been configured."""


class ApiError(Exception):
    """Raised when the DeepSeek API returns a non-200 response."""

    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class DeepSeek(commands.Cog):
    """Chat with DeepSeek via ``!ai`` or by mentioning the bot."""

    __author__ = ["Riley"]
    __version__ = "1.0.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x4E454255, force_registration=True)
        self.config.register_guild(
            model="deepseek-flash",
            system=DEFAULT_SYSTEM,
            max_history=10,
            max_tokens=800,
            thinking=False,
            ai_channel=None,
            respond_to_mentions=True,
        )
        self.session: Optional[aiohttp.ClientSession] = None
        self.api_key: Optional[str] = None
        # (guild_id, channel_id, user_id) -> list[{"role", "content"}]
        self._history: dict[tuple[int, int, int], list[dict[str, str]]] = {}
        self._last_used: dict[int, float] = {}
        self._last_warned: dict[int, float] = {}

    # ---------------------------------------------------------------- lifecycle

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()
        await self._refresh_key()

    async def cog_unload(self) -> None:
        if self.session is not None:
            await self.session.close()

    async def _refresh_key(self) -> None:
        tokens = await self.bot.get_shared_api_tokens("deepseek")
        self.api_key = tokens.get("api_key") or None

    @commands.Cog.listener()
    async def on_red_api_tokens_update(self, service_name: str, api_tokens: dict) -> None:
        if service_name == "deepseek":
            self.api_key = api_tokens.get("api_key") or None

    # ------------------------------------------------------------- AI plumbing

    async def _call(self, guild: discord.Guild, messages: list[dict[str, str]]) -> str:
        if not self.api_key:
            raise MissingKey

        conf = self.config.guild(guild)
        model = await conf.model()
        max_tokens = await conf.max_tokens()
        thinking = await conf.thinking()

        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        if not thinking:
            # temperature is ignored in thinking mode, so only send it otherwise.
            payload["temperature"] = 0.7

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=120)
        async with self.session.post(
            API_URL, json=payload, headers=headers, timeout=timeout
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001 - any decode failure is a bad response
                raise ApiError(resp.status, "unreadable response") from None

            if resp.status != 200:
                detail = ""
                if isinstance(data, dict):
                    err = data.get("error")
                    if isinstance(err, dict):
                        detail = err.get("message") or ""
                    detail = detail or data.get("message") or ""
                raise ApiError(resp.status, detail or resp.reason or "request failed")

            try:
                return str(data["choices"][0]["message"]["content"]).strip()
            except (KeyError, IndexError, TypeError):
                raise ApiError(resp.status, "unexpected response shape") from None

    def _cooldown_ok(self, user_id: int) -> bool:
        now = time.monotonic()
        if now - self._last_used.get(user_id, 0.0) < COOLDOWN_SECONDS:
            return False
        self._last_used[user_id] = now
        return True

    def _warn_ok(self, user_id: int) -> bool:
        """Rate-limit the 'no API key' notice so it can't be spammed."""
        now = time.monotonic()
        if now - self._last_warned.get(user_id, 0.0) < 60:
            return False
        self._last_warned[user_id] = now
        return True

    async def _answer(self, guild: discord.Guild, channel_id: int, user_id: int, prompt: str) -> str:
        """Return the text to send back: a reply or a human-readable error."""
        conf = self.config.guild(guild)
        system = await conf.system()
        max_history = await conf.max_history()

        key = (guild.id, channel_id, user_id)
        history = self._history.setdefault(key, [])
        messages = [{"role": "system", "content": system}]
        messages.extend(history[-max_history:])
        messages.append({"role": "user", "content": prompt})

        try:
            reply = await self._call(guild, messages)
        except MissingKey:
            return (
                "I don't have a DeepSeek API key yet. An admin can add one with "
                "`!set api deepseek api_key <key>`."
            )
        except ApiError as exc:
            log.warning("DeepSeek API error %s: %s", exc.status, exc.message)
            if exc.status == 401:
                return "My DeepSeek key was rejected (401). An admin should check it."
            if exc.status == 402:
                return "DeepSeek reports insufficient balance (402)."
            if exc.status == 429:
                return "DeepSeek is rate-limiting me (429). Try again shortly."
            if exc.status == 400:
                return "DeepSeek rejected the request (400). Check the model name."
            return f"DeepSeek returned an error ({exc.status})."
        except (aiohttp.ClientError, asyncio.TimeoutError):
            log.warning("DeepSeek request failed", exc_info=True)
            return "I couldn't reach DeepSeek just now. Try again in a moment."

        history.append({"role": "user", "content": prompt})
        history.append({"role": "assistant", "content": reply})
        if len(history) > max_history:
            del history[: len(history) - max_history]
        return reply

    async def _send(self, send, text: str) -> None:
        for page in pagify(text, page_length=1900):
            await send(page)

    # ------------------------------------------------------------------ commands

    @commands.command(name="ai", aliases=["ask"])
    @commands.guild_only()
    async def ai(self, ctx: commands.Context, *, prompt: str = None):
        """Ask DeepSeek. Context is kept per channel and user."""
        if prompt is None:
            return await ctx.send_help()
        if not self._cooldown_ok(ctx.author.id):
            return await ctx.reply("Slow down a moment.", mention_author=False)
        prompt = prompt.strip()[:MAX_PROMPT_CHARS]
        if not prompt:
            return await ctx.send_help()

        async with ctx.typing():
            text = await self._answer(ctx.guild, ctx.channel.id, ctx.author.id, prompt)
        await self._send(lambda content: ctx.reply(content, mention_author=False), text)

    @commands.command(name="aiclear")
    @commands.guild_only()
    async def aiclear(self, ctx: commands.Context):
        """Forget our conversation in this channel."""
        self._history.pop((ctx.guild.id, ctx.channel.id, ctx.author.id), None)
        await ctx.tick()

    @commands.group(name="aiset", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def aiset(self, ctx: commands.Context):
        """Configure the DeepSeek assistant."""
        await ctx.send_help()

    @aiset.command(name="model")
    async def aiset_model(self, ctx: commands.Context, model: str):
        """Set the model: deepseek-flash (fast) or deepseek-v4-pro (reasoning)."""
        model = model.lower()
        if model not in MODELS:
            return await ctx.send(f"Choose one of: {', '.join(MODELS)}")
        await self.config.guild(ctx.guild).model.set(model)
        await ctx.tick()

    @aiset.command(name="system")
    async def aiset_system(self, ctx: commands.Context, *, prompt: str = None):
        """Set (or view) the system prompt / persona."""
        conf = self.config.guild(ctx.guild)
        if prompt is None:
            current = await conf.system()
            return await ctx.send(f"Current system prompt:\n>>> {current}")
        await conf.system.set(prompt.strip())
        await ctx.tick()

    @aiset.command(name="history")
    async def aiset_history(self, ctx: commands.Context, messages: commands.Range[int, 0, 100]):
        """Set how many previous messages are kept as context (0-100)."""
        await self.config.guild(ctx.guild).max_history.set(messages)
        await ctx.tick()

    @aiset.command(name="maxtokens")
    async def aiset_maxtokens(self, ctx: commands.Context, tokens: commands.Range[int, 64, 8192]):
        """Set the maximum tokens generated per reply (64-8192)."""
        await self.config.guild(ctx.guild).max_tokens.set(tokens)
        await ctx.tick()

    @aiset.command(name="thinking")
    async def aiset_thinking(self, ctx: commands.Context, enabled: bool):
        """Enable/disable DeepSeek thinking mode (slower, better at reasoning)."""
        await self.config.guild(ctx.guild).thinking.set(enabled)
        await ctx.tick()

    @aiset.command(name="channel")
    async def aiset_channel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Set an AI channel (no prefix needed), or omit the channel to clear it."""
        conf = self.config.guild(ctx.guild)
        if channel is None:
            await conf.ai_channel.set(None)
            return await ctx.send("AI channel cleared.")
        await conf.ai_channel.set(channel.id)
        await ctx.send(
            f"{channel.mention} is now an AI channel - every message there gets a reply."
        )

    @aiset.command(name="mentions")
    async def aiset_mentions(self, ctx: commands.Context, enabled: bool):
        """Enable/disable replying when the bot is mentioned."""
        await self.config.guild(ctx.guild).respond_to_mentions.set(enabled)
        await ctx.tick()

    @aiset.command(name="clear")
    async def aiset_clear(self, ctx: commands.Context):
        """Clear all stored conversation context for this server."""
        for key in [k for k in self._history if k[0] == ctx.guild.id]:
            del self._history[key]
        await ctx.tick()

    @aiset.command(name="showsettings")
    async def aiset_showsettings(self, ctx: commands.Context):
        """Show the current DeepSeek settings."""
        settings = await self.config.guild(ctx.guild).all()
        channel = f"<#{settings['ai_channel']}>" if settings["ai_channel"] else "None"
        await ctx.send(
            f"Model: {settings['model']}\n"
            f"History: {settings['max_history']} messages\n"
            f"Max tokens: {settings['max_tokens']}\n"
            f"Thinking: {settings['thinking']}\n"
            f"AI channel: {channel}\n"
            f"Respond to mentions: {settings['respond_to_mentions']}\n"
            f"API key set: {'yes' if self.api_key else 'no'}"
        )

    # ------------------------------------------------------------------- natural

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or not message.content:
            return

        conf = self.config.guild(message.guild)
        mentioned = self.bot.user in message.mentions
        ai_channel = await conf.ai_channel()
        in_ai_channel = ai_channel is not None and message.channel.id == ai_channel
        mention_ok = mentioned and await conf.respond_to_mentions()

        replied_to_bot = False
        reference = message.reference
        if reference is not None:
            resolved = reference.resolved
            if isinstance(resolved, discord.Message) and resolved.author.id == self.bot.user.id:
                replied_to_bot = True

        if not (in_ai_channel or mention_ok or replied_to_bot):
            return

        # Never hijack a command.
        prefixes = await self.bot.get_valid_prefixes(message.guild)
        if any(message.content.startswith(prefix) for prefix in prefixes):
            return

        prompt = message.content
        for token in (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"):
            prompt = prompt.replace(token, "")
        prompt = prompt.strip()[:MAX_PROMPT_CHARS]
        if not prompt:
            return

        if self.api_key is None:
            # Answer when directly addressed so it's never silently ignored,
            # but rate-limit so a keyless bot can't spam a channel.
            if (mention_ok or replied_to_bot) and self._warn_ok(message.author.id):
                try:
                    await message.reply(
                        "I'm not connected to DeepSeek yet - set an API key with "
                        "`!set api deepseek api_key <key>`.",
                        mention_author=False,
                    )
                except discord.HTTPException:
                    pass
            return

        if not self._cooldown_ok(message.author.id):
            return

        try:
            async with message.channel.typing():
                text = await self._answer(
                    message.guild, message.channel.id, message.author.id, prompt
                )
        except (discord.Forbidden, discord.HTTPException):
            return

        async def send(content: str) -> None:
            if in_ai_channel and not (mentioned or replied_to_bot):
                await message.channel.send(content)
            else:
                await message.reply(content, mention_author=False)

        try:
            await self._send(send, text)
        except (discord.Forbidden, discord.HTTPException):
            return
