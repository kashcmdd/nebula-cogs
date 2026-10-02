"""DeepSeek — a Discord assistant that can actually manage the server.

The model calls **tools** to read live server data and, for users with the
matching Discord permission, to act: channels, roles, members (kick/ban/timeout/
nickname), messages (purge) and invites.

Every action is checked against the **requester's** permissions at execution
time (never the bot's), plus role-hierarchy rules, and is logged. A
non-privileged user cannot talk the bot into doing anything they couldn't do
themselves in the Discord UI.

Usage:
  * ``!ai <prompt>``  — one-shot, keeps per-channel context
  * mention the bot, reply to it, or use a configured AI channel

API key via Red shared tokens (service ``deepseek``):
  ``!set api deepseek api_key <your key>``

Models: ``deepseek-flash`` (fast) and ``deepseek-v4-pro`` (reasoning).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import pagify

log = logging.getLogger("red.cogs.deepseek")


def _ascii(value) -> str:
    """Force a value to ASCII so logging can't fail on a non-UTF8 console."""
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


class _AsciiSafeFilter(logging.Filter):
    """Sanitise log records.

    Red's Rich console handler raises UnicodeEncodeError on a cp1252 Windows
    console when a record contains characters it can't encode (for example an
    emoji in a channel name like '#📜・rules'). Because that happens inside
    log.info(), the exception propagates out of the calling coroutine and can
    abort a request after the action has already run. Sanitising the record
    before it reaches any handler prevents that entirely.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _ascii(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {key: _ascii(value) for key, value in record.args.items()}
            else:
                record.args = tuple(_ascii(arg) for arg in record.args)
        return True


log.addFilter(_AsciiSafeFilter())

API_URL = "https://api.deepseek.com/chat/completions"
MODELS = ("deepseek-flash", "deepseek-v4-pro")
DEFAULT_SYSTEM = (
    "You are Nebula, a Discord server assistant. You help with anything about "
    "the server or Discord: channels, categories, threads, roles, permissions, "
    "webhooks, emojis, members, moderation, automod, invites, server settings, "
    "onboarding, and this bot's own commands.\n"
    "You have live tools. Use them to inspect the real server instead of "
    "guessing, and to act on it when the person asking has permission: create, "
    "rename and delete channels, roles, threads, webhooks and emojis; set "
    "channel topics, slowmode, locks and per-role/member permissions; post "
    "messages and embeds; read, edit, pin, react to and delete messages; change "
    "server settings; assign and remove roles; kick, ban, unban, timeout and "
    "rename members; purge messages; send DMs; create invites and scheduled "
    "events; moderate voice; manage AutoMod rules; reorder roles and channels; "
    "update server settings; and manage stickers.\n"
    "Act immediately without asking for confirmation - including for purges "
    "and deletions. The ONLY action that needs confirmation is banning a "
    "member: before banning, ask the user to confirm, and only call ban_member "
    "with confirmed=true once they agree.\n"
    "If someone lacks permission for an action, say so plainly - never claim "
    "you did something you didn't. If a request is not about Discord or this "
    "server, decline in one sentence.\n"
    "Formatting: when posting an embed that lists several items (rules, steps, "
    "options), pass them as separate fields - send_embed's 'fields' argument is "
    "a list of {name, value} objects (name = short heading, value = the detail). "
    "Do not put a long list in the description.\n"
    "When asked to replace or reformat an existing embed: read it first, delete "
    "that exact message, then post ONE new embed. Never post the same thing "
    "twice, and perform each change once.\n"
    "Be concise and practical, using correct Discord terminology. This bot's "
    "commands use the `!` prefix. If unsure, say so rather than guessing."
)
MAX_PROMPT_CHARS = 4000
COOLDOWN_SECONDS = 3
MAX_TOOL_ROUNDS = 8
MAX_LIST_ITEMS = 60

_PERM_FLAGS = getattr(discord.Permissions, "VALID_FLAGS", {})
PERM_NAMES = set(_PERM_FLAGS)
EXPRESSION_PERM = "manage_expressions" if "manage_expressions" in _PERM_FLAGS else "manage_emojis_and_stickers"
# Matches a numbered or bulleted list line: "1. Title", "2) Title", "- Title", "**3. Title**".
_ITEM_RE = re.compile(
    r"^\s*(?:\*\*|__)?\s*(?:(?P<num>\d+)[.)]\s*|[-*•]\s+)(?P<title>.+?)(?:\*\*|__)?\s*$"
)

# Keyword -> emoji, for decorating rule/list field names. First match wins.
_EMOJI_HINTS = [
    (("respect", "harass", "hate", "discrimin", "attack", "be kind"), "🛡️"),
    (("spam", "advert", "self-promo", "promo", "flood"), "🚫"),
    (("nsfw", "gore", "appropriate", "sexual", "disturbing", "explicit"), "🔞"),
    (("english", "language"), "🗣️"),
    (("channel", "topic", "off-topic", "right place"), "📁"),
    (("illegal", "piracy", "hack", "drug"), "⚖️"),
    (("doxx", "privacy", "personal info"), "🔒"),
    (("staff", "moderator", "admin"), "👮"),
    (("alt", "evasion", "multi-account"), "🚷"),
    (("terms", "tos", "guidelines"), "📜"),
    (("name", "nickname", "avatar", "username"), "🪪"),
    (("voice", "soundboard", "mic"), "🔊"),
    (("dm", "direct message"), "✉️"),
]
_DEFAULT_EMOJIS = ["🔹", "🔸", "💠", "🔻", "🔺", "◾", "▪️", "🔘", "⚪", "🟣"]


def _fn(name: str, description: str, properties: dict | None = None, required: list | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


_STR = {"type": "string"}
_STR_DESC = lambda d: {"type": "string", "description": d}  # noqa: E731

READ_TOOLS = [
    _fn("server_info", "Basic server info: name, owner, member/channel/role counts, boosts, creation date."),
    _fn("list_roles", "List every role, highest first, with id, position, member count and flags."),
    _fn("list_channels", "List channels grouped by category, with type and id."),
    _fn("member_info", "Get a member's roles, top role and join date.",
        {"user": _STR_DESC("Username, display name or mention.")}, ["user"]),
    _fn("list_bans", "List recent bans (up to 50)."),
    _fn("list_invites", "List active invites with uses and expiry."),
    _fn("list_emojis", "List the server's custom emojis."),
    _fn("list_stickers", "List the server's stickers."),
    _fn("list_webhooks", "List webhooks, optionally for one channel.", {"channel": _STR}),
    _fn("read_audit_log", "Read recent audit-log entries (who did what).",
        {"limit": {"type": "integer", "description": "1-50, default 20."}}),
    _fn("read_messages", "Read recent messages, or one message by id. Includes embed titles, descriptions and fields.",
        {"channel": _STR, "limit": {"type": "integer", "description": "1-50, default 20."},
         "message_id": _STR_DESC("Read just this message instead of history.")}),
    _fn("list_scheduled_events", "List scheduled events."),
    _fn("list_automod_rules", "List AutoMod rules."),
]

ACTION_TOOLS = [
    # roles
    _fn("add_role", "Add a role to a member.", {"user": _STR, "role": _STR}, ["user", "role"]),
    _fn("remove_role", "Remove a role from a member.", {"user": _STR, "role": _STR}, ["user", "role"]),
    _fn("create_role", "Create a role.",
        {"name": _STR, "colour": _STR_DESC("Hex like #5865F2 (optional)."),
         "hoist": {"type": "boolean"}, "mentionable": {"type": "boolean"}}, ["name"]),
    _fn("edit_role", "Edit a role's name, colour, hoist or mentionable.",
        {"role": _STR, "name": _STR, "colour": _STR, "hoist": {"type": "boolean"},
         "mentionable": {"type": "boolean"}}, ["role"]),
    _fn("delete_role", "Delete a role.", {"role": _STR}, ["role"]),
    # channels
    _fn("create_text_channel", "Create a text channel.",
        {"name": _STR, "category": _STR_DESC("Category name (optional)."), "topic": _STR}, ["name"]),
    _fn("create_voice_channel", "Create a voice channel.",
        {"name": _STR, "category": _STR}, ["name"]),
    _fn("create_category", "Create a category.", {"name": _STR}, ["name"]),
    _fn("delete_channel", "Delete a channel.", {"channel": _STR}, ["channel"]),
    _fn("rename_channel", "Rename a channel.", {"channel": _STR, "name": _STR}, ["channel", "name"]),
    _fn("set_channel_topic", "Set a text channel's topic.", {"channel": _STR, "topic": _STR}, ["channel", "topic"]),
    _fn("set_slowmode", "Set a channel's slowmode in seconds (0 disables).",
        {"channel": _STR, "seconds": {"type": "integer"}}, ["channel", "seconds"]),
    _fn("set_channel_lock", "Lock or unlock a channel (@everyone can/can't send).",
        {"channel": _STR, "locked": {"type": "boolean"}}, ["channel", "locked"]),
    # members
    _fn("kick_member", "Kick a member.", {"member": _STR, "reason": _STR}, ["member"]),
    _fn("ban_member", "Ban a member (requires confirmation: only call with confirmed=true after the user agrees).",
        {"member": _STR, "reason": _STR, "delete_message_days": {"type": "integer"},
         "confirmed": {"type": "boolean", "description": "Set true only after the user confirms."}},
        ["member", "confirmed"]),
    _fn("unban_member", "Unban a user by id.", {"user_id": _STR, "reason": _STR}, ["user_id"]),
    _fn("timeout_member", "Timeout a member (e.g. '10m', '2h', '1d'; max 28d).",
        {"member": _STR, "duration": _STR, "reason": _STR}, ["member", "duration"]),
    _fn("remove_timeout", "Remove a member's timeout.", {"member": _STR, "reason": _STR}, ["member"]),
    _fn("set_nickname", "Set or clear a member's nickname.",
        {"member": _STR, "nickname": _STR_DESC("Leave empty to reset.")}, ["member"]),
    # messages / invites
    _fn("purge_messages", "Bulk-delete recent messages in a channel (1-100, last 14 days).",
        {"channel": _STR, "count": {"type": "integer"}}, ["count"]),
    _fn("create_invite", "Create an invite link for a channel.",
        {"channel": _STR, "max_age_seconds": {"type": "integer"}, "max_uses": {"type": "integer"}}),
    _fn("set_channel_permission", "Allow or deny permissions for a role or member in a channel.",
        {"channel": _STR, "target": _STR_DESC("Role or member name/mention."),
         "target_type": {"type": "string", "enum": ["role", "member"]},
         "allow": {"type": "array", "items": {"type": "string"},
                   "description": "Permission names to allow (e.g. view_channel, send_messages)."},
         "deny": {"type": "array", "items": {"type": "string"},
                  "description": "Permission names to deny."}},
        ["channel", "target"]),
    _fn("clear_channel_permission", "Remove a role/member's permission overwrite in a channel.",
        {"channel": _STR, "target": _STR, "target_type": {"type": "string", "enum": ["role", "member"]}},
        ["channel", "target"]),
    _fn("create_thread", "Create a thread in a text channel.",
        {"channel": _STR, "name": _STR, "private": {"type": "boolean"}}, ["channel", "name"]),
    _fn("delete_thread", "Delete a thread.", {"thread": _STR}, ["thread"]),
    _fn("create_webhook", "Create a webhook in a channel.", {"channel": _STR, "name": _STR}, ["channel", "name"]),
    _fn("delete_webhook", "Delete a webhook by name or id.", {"webhook": _STR}, ["webhook"]),
    _fn("create_emoji", "Create a custom emoji from an image URL.",
        {"name": _STR, "image_url": _STR}, ["name", "image_url"]),
    _fn("delete_emoji", "Delete a custom emoji by name.", {"emoji": _STR}, ["emoji"]),
    _fn("edit_server", "Change server settings.",
        {"name": _STR,
         "verification_level": {"type": "string", "enum": ["none", "low", "medium", "high", "highest"]},
         "explicit_content_filter": {"type": "string", "enum": ["disabled", "no_role", "all_members"]},
         "default_notifications": {"type": "string", "enum": ["all_messages", "only_mentions"]},
         "system_channel": _STR, "afk_channel": _STR,
         "afk_timeout_seconds": {"type": "integer"},
         "require_2fa": {"type": "boolean"},
         "suppress_join_notifications": {"type": "boolean"},
         "suppress_boost_notifications": {"type": "boolean"},
         "rules_channel": _STR, "public_updates_channel": _STR,
         "vanity_code": _STR_DESC("Requires boost level 3 and your permission.")}),
    _fn("set_server_icon", "Set the server icon from an image URL.", {"image_url": _STR}, ["image_url"]),
    _fn("send_message", "Post a message in a channel.",
        {"channel": _STR, "content": _STR, "reply_to_message_id": _STR}, ["content"]),
    _fn("send_embed", "Post an embed. For a list (e.g. rules), pass each item in 'fields' as {name, value} - do not put lists in 'description'.",
        {"channel": _STR, "title": _STR, "description": _STR,
         "colour": _STR_DESC("Hex like #7C3AED."), "footer": _STR,
         "image_url": _STR, "thumbnail_url": _STR,
         "fields": {"type": "array", "items": {"type": "object", "properties": {
             "name": _STR, "value": _STR, "inline": {"type": "boolean"}}}}}),
    _fn("edit_message", "Edit one of the bot's own messages.",
        {"channel": _STR, "message_id": _STR, "content": _STR}, ["message_id", "content"]),
    _fn("delete_message", "Delete a message by id.", {"channel": _STR, "message_id": _STR}, ["message_id"]),
    _fn("pin_message", "Pin a message by id.", {"channel": _STR, "message_id": _STR}, ["message_id"]),
    _fn("unpin_message", "Unpin a message by id.", {"channel": _STR, "message_id": _STR}, ["message_id"]),
    _fn("react_to_message", "Add a reaction to a message by id.",
        {"channel": _STR, "message_id": _STR, "emoji": _STR}, ["message_id", "emoji"]),
    _fn("dm_user", "Send a direct message to a member.", {"user": _STR, "content": _STR}, ["user", "content"]),
    _fn("voice_move", "Move a member to a voice channel, or disconnect them with channel='disconnect'.",
        {"member": _STR, "channel": _STR}, ["member", "channel"]),
    _fn("voice_mute", "Server-mute or unmute a member.",
        {"member": _STR, "mute": {"type": "boolean"}}, ["member", "mute"]),
    _fn("voice_deafen", "Server-deafen or undeafen a member.",
        {"member": _STR, "deafen": {"type": "boolean"}}, ["member", "deafen"]),
    _fn("create_scheduled_event", "Create a scheduled event.",
        {"name": _STR, "start_time": _STR_DESC("ISO like 2026-10-02T18:00 or 'in 2h'."),
         "entity_type": {"type": "string", "enum": ["voice", "stage", "external"]},
         "channel": _STR, "location": _STR_DESC("Required for external events."),
         "description": _STR, "end_time": _STR},
        ["name", "start_time"]),
    _fn("edit_scheduled_event", "Edit a scheduled event.",
        {"event": _STR, "name": _STR, "description": _STR, "start_time": _STR, "end_time": _STR}, ["event"]),
    _fn("delete_scheduled_event", "Delete a scheduled event.", {"event": _STR}, ["event"]),
    _fn("create_automod_rule", "Create an AutoMod rule.",
        {"name": _STR,
         "trigger_type": {"type": "string", "enum": ["keyword", "keyword_preset", "mention_spam", "spam"]},
         "keywords": {"type": "array", "items": _STR},
         "presets": {"type": "array", "items": _STR, "description": "e.g. profanity, sexual_content, slurs."},
         "mention_limit": {"type": "integer"},
         "block_message": {"type": "boolean"},
         "timeout_seconds": {"type": "integer"},
         "alert_channel": _STR},
        ["name", "trigger_type"]),
    _fn("delete_automod_rule", "Delete an AutoMod rule by name or id.", {"rule": _STR}, ["rule"]),
    _fn("move_role", "Move a role to a position (higher = more powerful).",
        {"role": _STR, "position": {"type": "integer"}}, ["role", "position"]),
    _fn("move_channel", "Move a channel into a category or to a position.",
        {"channel": _STR, "category": _STR_DESC("Category name, or 'none' to remove it from its category."),
         "position": {"type": "integer"}}, ["channel"]),
    _fn("set_server_banner", "Set the server banner from an image URL (needs boost level 2).",
        {"image_url": _STR}, ["image_url"]),
    _fn("prune_members", "Kick members inactive for the given number of days (1-30).",
        {"days": {"type": "integer"}, "reason": _STR}, ["days"]),
    _fn("create_sticker", "Create a sticker from a PNG image URL (320x320, needs sticker slots).",
        {"name": _STR, "description": _STR, "tags": _STR_DESC("A unicode emoji or short text."),
         "image_url": _STR}, ["name", "description", "tags", "image_url"]),
    _fn("delete_sticker", "Delete a sticker by name.", {"sticker": _STR}, ["sticker"]),
]


class MissingKey(Exception):
    """Raised when no DeepSeek API key has been configured."""


class ApiError(Exception):
    """Raised when the DeepSeek API returns a non-200 response."""

    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class DeepSeek(commands.Cog):
    """A DeepSeek assistant that can read and manage the server."""

    __author__ = ["Riley"]
    __version__ = "1.4.0"

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
            allow_actions=True,
        )
        self.session: Optional[aiohttp.ClientSession] = None
        self.api_key: Optional[str] = None
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

    # ------------------------------------------------------------------ helpers

    def _has_perm(self, user: discord.abc.User, guild: discord.Guild, perm: str) -> bool:
        if user.id in self.bot.owner_ids or guild.owner_id == user.id:
            return True
        perms = getattr(user, "guild_permissions", None)
        return bool(perms and getattr(perms, perm, False))

    @staticmethod
    def _bot_has(guild: discord.Guild, perm: str) -> bool:
        return bool(getattr(guild.me.guild_permissions, perm, False))

    def _guard(self, user, guild, perm: str) -> Optional[str]:
        if not self._has_perm(user, guild, perm):
            return f"You don't have the '{perm.replace('_', ' ')}' permission for that."
        if not self._bot_has(guild, perm):
            return f"I don't have the '{perm.replace('_', ' ')}' permission for that."
        return None

    def _guard_message(self, user, channel) -> Optional[str]:
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return "I can only post in a text channel or thread."
        is_owner = user.id in self.bot.owner_ids or getattr(channel.guild, "owner_id", None) == user.id
        may_manage = isinstance(user, discord.Member) and channel.permissions_for(user).manage_messages
        if not (is_owner or may_manage):
            return "You need the Manage Messages permission to do that."
        if not channel.permissions_for(channel.guild.me).send_messages:
            return "I don't have permission to send messages in that channel."
        return None

    async def _fetch_message(self, channel, message_id):
        try:
            return await channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, TypeError):
            return None

    @staticmethod
    def _describe_message(message) -> str:
        lines = [
            f"- [{message.id}] {message.author}: "
            f"{(message.content or '').replace(chr(10), ' ')[:200]}"
        ]
        for embed in message.embeds:
            if embed.title:
                lines.append(f"    embed title: {embed.title[:150]}")
            if embed.description:
                lines.append(f"    embed description: {embed.description.replace(chr(10), ' ')[:600]}")
            for field in embed.fields:
                lines.append(
                    f"    embed field '{field.name}': {field.value.replace(chr(10), ' ')[:300]}"
                )
            if embed.footer and embed.footer.text:
                lines.append(f"    embed footer: {embed.footer.text[:150]}")
        for attachment in message.attachments:
            lines.append(f"    attachment: {attachment.filename} ({attachment.url})")
        return "\n".join(lines)

    @staticmethod
    def _parse_time(text):
        if not text:
            return None
        text = str(text).strip()
        relative = re.fullmatch(r"in\s+(.+)", text, re.IGNORECASE)
        if relative:
            seconds = DeepSeek._parse_duration(relative.group(1))
            return discord.utils.utcnow() + timedelta(seconds=seconds) if seconds else None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _resolve_event(guild, text):
        text = (text or "").strip()
        if text.isdigit():
            return guild.get_scheduled_event(int(text))
        events = guild.scheduled_events
        for event in events:
            if event.name.lower() == text.lower():
                return event
        for event in events:
            if text and text.lower() in event.name.lower():
                return event
        return None

    async def _resolve_automod_rule(self, guild, text):
        text = (text or "").strip()
        if text.isdigit():
            try:
                return await guild.fetch_automod_rule(int(text))
            except discord.NotFound:
                return None
        for rule in await guild.fetch_automod_rules():
            if rule.name.lower() == text.lower():
                return rule
        return None

    def _can_manage_roles(self, user, guild) -> bool:
        if user.id in self.bot.owner_ids or guild.owner_id == user.id:
            return True
        perms = getattr(user, "guild_permissions", None)
        return bool(perms and perms.manage_roles)

    def _member_block_reason(self, guild: discord.Guild, actor, target) -> Optional[str]:
        """Return why `actor` may not moderate `target`, or None if allowed."""
        if target.id == guild.owner_id:
            return "that's the server owner"
        if target.id == self.bot.user.id:
            return "that's me"
        if target.id == getattr(actor, "id", None):
            return "that's you"
        if actor.id != guild.owner_id:
            actor_top = getattr(actor, "top_role", None)
            if actor_top is not None and target.top_role >= actor_top:
                return "their highest role is equal to or above yours"
        if guild.me.top_role <= target.top_role:
            return "their highest role isn't below mine, so I can't manage them"
        return None

    @staticmethod
    def _parse_duration(text) -> Optional[int]:
        if isinstance(text, (int, float)):
            return int(text)
        match = re.fullmatch(r"(\d+)\s*([smhdw]?)", str(text).strip().lower())
        if not match:
            return None
        unit = match.group(2) or "s"
        return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]

    @staticmethod
    def _resolve_member(guild, text) -> Optional[discord.Member]:
        text = (text or "").strip()
        match = re.match(r"<@!?(\d+)>", text)
        if match:
            return guild.get_member(int(match.group(1)))
        if text.isdigit():
            return guild.get_member(int(text))
        lowered = text.lower().lstrip("@")
        for member in guild.members:
            names = {member.name.lower(), member.display_name.lower()}
            if member.global_name:
                names.add(member.global_name.lower())
            if lowered in names:
                return member
        for member in guild.members:
            if lowered in member.name.lower() or lowered in member.display_name.lower():
                return member
        return None

    @staticmethod
    def _resolve_role(guild, text) -> Optional[discord.Role]:
        text = (text or "").strip()
        match = re.match(r"<@&(\d+)>", text)
        if match:
            return guild.get_role(int(match.group(1)))
        if text.isdigit():
            return guild.get_role(int(text))
        lowered = text.lower().lstrip("@")
        for role in guild.roles:
            if role.name.lower() == lowered:
                return role
        for role in guild.roles:
            if lowered in role.name.lower():
                return role
        return None

    @staticmethod
    def _resolve_channel(guild, text):
        text = (text or "").strip()
        match = re.match(r"<#(\d+)>", text)
        if match:
            return guild.get_channel(int(match.group(1)))
        if text.isdigit():
            return guild.get_channel(int(text))
        lowered = text.lower().lstrip("#")
        for channel in guild.channels:
            if channel.name.lower() == lowered:
                return channel
        for channel in guild.channels:
            if lowered in channel.name.lower():
                return channel
        return None

    def _resolve_target(self, guild, text, target_type=None):
        """Resolve a role or member for permission overwrites."""
        if target_type == "member":
            return self._resolve_member(guild, text)
        if target_type == "role":
            return self._resolve_role(guild, text)
        return self._resolve_role(guild, text) or self._resolve_member(guild, text)

    # ------------------------------------------------------------- AI plumbing

    async def _request(self, guild: discord.Guild, messages: list[dict], tools: list[dict]) -> dict:
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
            payload["temperature"] = 0.7
        if tools:
            payload["tools"] = tools

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=120)
        async with self.session.post(API_URL, json=payload, headers=headers, timeout=timeout) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
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
                return data["choices"][0]["message"]
            except (KeyError, IndexError, TypeError):
                raise ApiError(resp.status, "unexpected response shape") from None

    def _cooldown_ok(self, user_id: int) -> bool:
        now = time.monotonic()
        if now - self._last_used.get(user_id, 0.0) < COOLDOWN_SECONDS:
            return False
        self._last_used[user_id] = now
        return True

    def _warn_ok(self, user_id: int) -> bool:
        now = time.monotonic()
        if now - self._last_warned.get(user_id, 0.0) < 60:
            return False
        self._last_warned[user_id] = now
        return True

    def _requester_context(self, guild, channel, user) -> str:
        parts = [
            f"You are in the server '{guild.name}' (id {guild.id}), "
            f"in #{getattr(channel, 'name', 'unknown')} (id {channel.id}).",
            f"You are speaking with {user} (id {user.id}).",
        ]
        if isinstance(user, discord.Member):
            roles = [r.name for r in user.roles if not r.is_default()]
            parts.append(
                "Their current roles: " + (", ".join(roles) if roles else "none")
                + f". Their highest role: {user.top_role.name}."
            )
            parts.append(
                "They have permission to manage the server."
                if self._can_manage_roles(user, guild)
                else "They have NO manage-server permissions - refuse any change they ask for."
            )
        parts.append("Resolve words like 'me', 'my' and 'I' to this person.")
        return " ".join(parts)

    async def _answer(self, guild, channel, user, prompt: str) -> str:
        conf = self.config.guild(guild)
        system = await conf.system()
        max_history = await conf.max_history()
        allow_actions = await conf.allow_actions()

        key = (guild.id, channel.id, user.id)
        history = self._history.setdefault(key, [])
        messages = [
            {"role": "system", "content": f"{system}\n\n{self._requester_context(guild, channel, user)}"}
        ]
        messages.extend(history[-max_history:])
        messages.append({"role": "user", "content": prompt})

        tools = list(READ_TOOLS)
        if allow_actions:
            tools += ACTION_TOOLS

        reply: Optional[str] = None
        tool_results: list[str] = []
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                message = await self._request(guild, messages, tools)
            except MissingKey:
                return "I don't have a DeepSeek API key yet. An admin can set one with `!set api deepseek api_key <key>`."
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

            tool_calls = message.get("tool_calls")
            if not tool_calls:
                content = (message.get("content") or "").strip()
                if content:
                    reply = content
                elif tool_results:
                    reply = "Done:\n" + "\n".join(f"- {result}" for result in tool_results[-6:])
                else:
                    reply = "I couldn't produce a reply."
                break

            messages.append(
                {"role": "assistant", "content": message.get("content") or "", "tool_calls": tool_calls}
            )
            for call in tool_calls:
                result = await self._execute_tool(guild, channel, user, call)
                tool_results.append(result)
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": result})
        else:
            reply = "I hit my tool-use limit for that request. Try smaller steps."

        history.append({"role": "user", "content": prompt})
        history.append({"role": "assistant", "content": reply})
        if len(history) > max_history:
            del history[: len(history) - max_history]
        return reply

    async def _send(self, send, text: str) -> None:
        for page in pagify(text, page_length=1900):
            await send(page)

    # ------------------------------------------------------------- tool dispatch

    async def _execute_tool(self, guild, channel, user, call: dict) -> str:
        function = call.get("function") or {}
        name = function.get("name", "")
        try:
            args = json.loads(function.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            return "Error: arguments were not valid JSON."

        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return f"Unknown tool: {name}"
        try:
            return await handler(guild, channel, user, args)
        except discord.Forbidden:
            log.warning("Forbidden executing tool %s", name)
            return "Discord refused that action (missing permissions)."
        except discord.HTTPException as exc:
            return f"Discord error: {exc}"
        except Exception as exc:  # noqa: BLE001 - never let one tool kill the reply
            log.exception("Tool %s failed", name)
            return f"The '{name}' action failed: {exc}"

    # ---------------------------------------------------------------- read tools

    async def _tool_server_info(self, guild, channel, user, args) -> str:
        return (
            f"Server: {guild.name} (id {guild.id})\nOwner id: {guild.owner_id}\n"
            f"Members: {guild.member_count}\nChannels: {len(guild.channels)}\n"
            f"Roles: {len(guild.roles)}\nBoost tier: {guild.premium_tier} "
            f"({guild.premium_subscription_count or 0} boosts)\n"
            f"Created: {guild.created_at.date().isoformat()}"
        )

    async def _tool_list_roles(self, guild, channel, user, args) -> str:
        roles = [r for r in reversed(guild.roles) if not r.is_default()]
        lines = []
        for role in roles[:MAX_LIST_ITEMS]:
            flags = []
            if role.permissions.administrator:
                flags.append("ADMINISTRATOR")
            if role.managed:
                flags.append("managed")
            if role.hoist:
                flags.append("hoisted")
            suffix = (", " + ", ".join(flags)) if flags else ""
            lines.append(f"- {role.name} (id {role.id}, pos {role.position}, {len(role.members)} members{suffix})")
        if len(roles) > MAX_LIST_ITEMS:
            lines.append(f"...and {len(roles) - MAX_LIST_ITEMS} more")
        return "Roles, highest first:\n" + "\n".join(lines)

    async def _tool_list_channels(self, guild, channel, user, args) -> str:
        lines = []
        for category, channels in guild.by_category():
            if category is not None:
                lines.append(f"[{category.name}]")
            for ch in channels:
                lines.append(f"- #{ch.name} ({ch.type.name}, id {ch.id})")
            if len(lines) > MAX_LIST_ITEMS:
                break
        return "Channels:\n" + "\n".join(lines)

    async def _tool_member_info(self, guild, channel, user, args) -> str:
        member = self._resolve_member(guild, args.get("user", ""))
        if member is None:
            return "No matching member found."
        roles = ", ".join(r.name for r in member.roles if not r.is_default()) or "none"
        joined = member.joined_at.date().isoformat() if member.joined_at else "unknown"
        return f"{member} (id {member.id})\nTop role: {member.top_role.name}\nRoles: {roles}\nJoined: {joined}"

    async def _tool_list_bans(self, guild, channel, user, args) -> str:
        entries = [entry async for entry in guild.bans(limit=50)]
        if not entries:
            return "No bans."
        return "Bans:\n" + "\n".join(f"- {e.user} (id {e.user.id}){': ' + e.reason if e.reason else ''}" for e in entries)

    async def _tool_list_invites(self, guild, channel, user, args) -> str:
        invites = await guild.invites()
        if not invites:
            return "No active invites."
        return "Invites:\n" + "\n".join(
            f"- {inv.code}: {inv.uses}/{inv.max_uses or '∞'} uses"
            + (f", expires {inv.expires_at.date().isoformat()}" if inv.expires_at else "")
            for inv in invites
        )

    # --------------------------------------------------------------- role tools

    async def _tool_add_role(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_roles")
        if err:
            return err
        member = self._resolve_member(guild, args.get("user", ""))
        role = self._resolve_role(guild, args.get("role", ""))
        if member is None:
            return "No matching member found."
        if role is None or role.is_default():
            return "No assignable role matched that name."
        if role.managed:
            return f"'{role.name}' is managed by an integration and can't be assigned manually."
        if role.position >= guild.me.top_role.position:
            return f"I can't assign '{role.name}' - it's higher than my highest role."
        if user.id != guild.owner_id and role.position >= getattr(user, "top_role", role).position:
            return f"You can't assign '{role.name}' - it's higher than your highest role."
        if role in member.roles:
            return f"{member.display_name} already has '{role.name}'."
        await member.add_roles(role, reason=f"AI request by {user} ({user.id})")
        log.info("AI: %s added role '%s' to %s", user, role.name, member)
        return f"Added '{role.name}' to {member.display_name}."

    async def _tool_remove_role(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_roles")
        if err:
            return err
        member = self._resolve_member(guild, args.get("user", ""))
        role = self._resolve_role(guild, args.get("role", ""))
        if member is None:
            return "No matching member found."
        if role is None or role.is_default():
            return "No removable role matched that name."
        if role not in member.roles:
            return f"{member.display_name} doesn't have '{role.name}'."
        if role.position >= guild.me.top_role.position:
            return f"I can't remove '{role.name}' - it's higher than my highest role."
        if user.id != guild.owner_id and role.position >= getattr(user, "top_role", role).position:
            return f"You can't remove '{role.name}' - it's higher than your highest role."
        await member.remove_roles(role, reason=f"AI request by {user} ({user.id})")
        log.info("AI: %s removed role '%s' from %s", user, role.name, member)
        return f"Removed '{role.name}' from {member.display_name}."

    async def _tool_create_role(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_roles")
        if err:
            return err
        name = (args.get("name") or "").strip()
        if not name:
            return "A role name is required."
        colour = None
        if args.get("colour"):
            try:
                colour = discord.Colour.from_str(args["colour"])
            except ValueError:
                return f"'{args['colour']}' isn't a valid hex colour."
        role = await guild.create_role(
            name=name, colour=colour, hoist=bool(args.get("hoist")),
            mentionable=bool(args.get("mentionable")), reason=f"AI request by {user} ({user.id})",
        )
        log.info("AI: %s created role '%s'", user, role.name)
        return f"Created role '{role.name}' (id {role.id})."

    async def _tool_edit_role(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_roles")
        if err:
            return err
        role = self._resolve_role(guild, args.get("role", ""))
        if role is None or role.is_default():
            return "No editable role matched that name."
        if role.position >= guild.me.top_role.position:
            return f"'{role.name}' is higher than my highest role; I can't edit it."
        if user.id != guild.owner_id and role.position >= getattr(user, "top_role", role).position:
            return f"'{role.name}' is higher than your highest role; you can't edit it."
        kwargs = {}
        if args.get("name"):
            kwargs["name"] = args["name"]
        if args.get("colour"):
            try:
                kwargs["colour"] = discord.Colour.from_str(args["colour"])
            except ValueError:
                return f"'{args['colour']}' isn't a valid hex colour."
        if "hoist" in args:
            kwargs["hoist"] = bool(args["hoist"])
        if "mentionable" in args:
            kwargs["mentionable"] = bool(args["mentionable"])
        if not kwargs:
            return "Nothing to change."
        await role.edit(reason=f"AI request by {user} ({user.id})", **kwargs)
        log.info("AI: %s edited role '%s' %s", user, role.name, kwargs)
        return f"Updated '{role.name}': {', '.join(kwargs)}."

    async def _tool_delete_role(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_roles")
        if err:
            return err
        role = self._resolve_role(guild, args.get("role", ""))
        if role is None or role.is_default():
            return "No deletable role matched that name."
        if role.managed:
            return f"'{role.name}' is managed by an integration and can't be deleted."
        if role.position >= guild.me.top_role.position:
            return f"'{role.name}' is higher than my highest role; I can't delete it."
        if user.id != guild.owner_id and role.position >= getattr(user, "top_role", role).position:
            return f"'{role.name}' is higher than your highest role; you can't delete it."
        name = role.name
        await role.delete(reason=f"AI request by {user} ({user.id})")
        log.info("AI: %s deleted role '%s'", user, name)
        return f"Deleted role '{name}'."

    # ------------------------------------------------------------ channel tools

    async def _tool_create_text_channel(self, guild, channel, user, args) -> str:
        return await self._create_channel(guild, user, args, kind="text")

    async def _tool_create_voice_channel(self, guild, channel, user, args) -> str:
        return await self._create_channel(guild, user, args, kind="voice")

    async def _tool_create_category(self, guild, channel, user, args) -> str:
        return await self._create_channel(guild, user, args, kind="category")

    async def _create_channel(self, guild, user, args, kind: str) -> str:
        err = self._guard(user, guild, "manage_channels")
        if err:
            return err
        name = (args.get("name") or "").strip()
        if not name:
            return "A channel name is required."
        parent = None
        if args.get("category"):
            found = self._resolve_channel(guild, args["category"])
            parent = found if isinstance(found, discord.CategoryChannel) else None
        reason = f"AI request by {user} ({user.id})"
        if kind == "category":
            created = await guild.create_category(name, reason=reason)
        elif kind == "voice":
            created = await guild.create_voice_channel(name, category=parent, reason=reason)
        else:
            topic = args.get("topic") or None
            created = await guild.create_text_channel(name, category=parent, topic=topic, reason=reason)
        log.info("AI: %s created %s channel '%s'", user, kind, created.name)
        return f"Created {kind} channel '{created.name}' (id {created.id})."

    async def _tool_delete_channel(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_channels")
        if err:
            return err
        target = self._resolve_channel(guild, args.get("channel", ""))
        if target is None:
            return "No matching channel found."
        name = target.name
        await target.delete(reason=f"AI request by {user} ({user.id})")
        log.info("AI: %s deleted channel '%s'", user, name)
        return f"Deleted channel '{name}'."

    async def _tool_rename_channel(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_channels")
        if err:
            return err
        target = self._resolve_channel(guild, args.get("channel", ""))
        if target is None:
            return "No matching channel found."
        new_name = (args.get("name") or "").strip()
        if not new_name:
            return "A new name is required."
        await target.edit(name=new_name, reason=f"AI request by {user} ({user.id})")
        log.info("AI: %s renamed channel '%s' to '%s'", user, target.name, new_name)
        return f"Renamed channel to '{new_name}'."

    async def _tool_set_channel_topic(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_channels")
        if err:
            return err
        target = self._resolve_channel(guild, args.get("channel", ""))
        if not isinstance(target, discord.TextChannel):
            return "No matching text channel found."
        await target.edit(topic=args.get("topic") or None, reason=f"AI request by {user} ({user.id})")
        return f"Set the topic of #{target.name}."

    async def _tool_set_slowmode(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_channels")
        if err:
            return err
        target = self._resolve_channel(guild, args.get("channel", ""))
        if not isinstance(target, (discord.TextChannel, discord.ForumChannel)):
            return "No matching text channel found."
        seconds = max(0, min(int(args.get("seconds", 0)), 21600))
        await target.edit(slowmode_delay=seconds, reason=f"AI request by {user} ({user.id})")
        return f"Set slowmode in #{target.name} to {seconds}s."

    async def _tool_set_channel_lock(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_channels")
        if err:
            return err
        target = self._resolve_channel(guild, args.get("channel", ""))
        if not isinstance(target, discord.abc.GuildChannel):
            return "No matching channel found."
        locked = bool(args.get("locked"))
        overwrite = target.overwrites_for(guild.default_role)
        overwrite.send_messages = False if locked else None
        await target.set_permissions(
            guild.default_role, overwrite=overwrite, reason=f"AI request by {user} ({user.id})"
        )
        return f"{'Locked' if locked else 'Unlocked'} #{target.name}."

    # ------------------------------------------------------------- member tools

    async def _tool_kick_member(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "kick_members")
        if err:
            return err
        member = self._resolve_member(guild, args.get("member", ""))
        if member is None:
            return "No matching member found."
        blocked = self._member_block_reason(guild, user, member)
        if blocked:
            return f"I can't kick them - {blocked}."
        await member.kick(reason=args.get("reason") or f"AI request by {user} ({user.id})")
        log.info("AI: %s kicked %s", user, member)
        return f"Kicked {member}."

    async def _tool_ban_member(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "ban_members")
        if err:
            return err
        member = self._resolve_member(guild, args.get("member", ""))
        if member is None:
            return "No matching member found."
        if not args.get("confirmed"):
            return (
                f"CONFIRMATION REQUIRED: banning {member} cannot be undone. Ask the "
                "user to confirm, then call ban_member again with confirmed=true."
            )
        blocked = self._member_block_reason(guild, user, member)
        if blocked:
            return f"I can't ban them - {blocked}."
        days = max(0, min(int(args.get("delete_message_days", 0)), 7))
        await member.ban(
            reason=args.get("reason") or f"AI request by {user} ({user.id})",
            delete_message_days=days,
        )
        log.info("AI: %s banned %s", user, member)
        return f"Banned {member}."

    async def _tool_unban_member(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "ban_members")
        if err:
            return err
        raw = str(args.get("user_id", "")).strip()
        match = re.search(r"\d+", raw)
        if not match:
            return "A user id is required."
        uid = int(match.group(0))
        try:
            await guild.unban(discord.Object(id=uid), reason=args.get("reason") or f"AI request by {user} ({user.id})")
        except discord.NotFound:
            return "That user isn't banned."
        log.info("AI: %s unbanned %s", user, uid)
        return f"Unbanned user {uid}."

    async def _tool_timeout_member(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "moderate_members")
        if err:
            return err
        member = self._resolve_member(guild, args.get("member", ""))
        if member is None:
            return "No matching member found."
        blocked = self._member_block_reason(guild, user, member)
        if blocked:
            return f"I can't time them out - {blocked}."
        seconds = self._parse_duration(args.get("duration"))
        if not seconds or seconds < 1:
            return "Give a duration like '10m', '2h' or '1d'."
        seconds = min(seconds, 28 * 86400)
        await member.timeout(timedelta(seconds=seconds), reason=args.get("reason") or f"AI request by {user} ({user.id})")
        log.info("AI: %s timed out %s for %ss", user, member, seconds)
        return f"Timed out {member} for {seconds} seconds."

    async def _tool_remove_timeout(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "moderate_members")
        if err:
            return err
        member = self._resolve_member(guild, args.get("member", ""))
        if member is None:
            return "No matching member found."
        await member.timeout(None, reason=args.get("reason") or f"AI request by {user} ({user.id})")
        return f"Removed {member}'s timeout."

    async def _tool_set_nickname(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_nicknames")
        if err:
            return err
        member = self._resolve_member(guild, args.get("member", ""))
        if member is None:
            return "No matching member found."
        blocked = self._member_block_reason(guild, user, member)
        if blocked:
            return f"I can't rename them - {blocked}."
        nickname = (args.get("nickname") or "").strip() or None
        await member.edit(nick=nickname, reason=f"AI request by {user} ({user.id})")
        return f"Set {member}'s nickname to {nickname or 'default'}."

    # ------------------------------------------------------- message/invite tools

    async def _tool_purge_messages(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_messages")
        if err:
            return err
        target = self._resolve_channel(guild, args.get("channel", "")) if args.get("channel") else channel
        if not isinstance(target, discord.TextChannel):
            return "No matching text channel found."
        count = max(1, min(int(args.get("count", 10)), 100))
        deleted = await target.purge(limit=count, reason=f"AI request by {user} ({user.id})")
        log.info("AI: %s purged %d messages in #%s", user, len(deleted), target.name)
        return f"Deleted {len(deleted)} messages in #{target.name}."

    async def _tool_create_invite(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "create_instant_invite")
        if err:
            return err
        target = self._resolve_channel(guild, args.get("channel", "")) if args.get("channel") else channel
        if not isinstance(target, discord.abc.GuildChannel):
            return "No matching channel found."
        invite = await target.create_invite(
            max_age=max(0, int(args.get("max_age_seconds", 0))),
            max_uses=max(0, int(args.get("max_uses", 0))),
            reason=f"AI request by {user} ({user.id})",
        )
        log.info("AI: %s created invite in #%s", user, target.name)
        expires = invite.expires_at.date().isoformat() if invite.expires_at else "never"
        return f"Created invite: {invite.url} (expires {expires})."

    # ------------------------------------------------- permissions / threads / etc.

    async def _tool_set_channel_permission(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_roles")
        if err:
            return err
        target_channel = self._resolve_channel(guild, args.get("channel", ""))
        if not isinstance(target_channel, discord.abc.GuildChannel):
            return "No matching channel found."
        target = self._resolve_target(guild, args.get("target", ""), args.get("target_type"))
        if target is None:
            return "No matching role or member."
        allow = args.get("allow") or []
        deny = args.get("deny") or []
        invalid = [p for p in list(allow) + list(deny) if p not in PERM_NAMES]
        if invalid:
            return f"Unknown permission name(s): {', '.join(invalid)}."
        overwrite = target_channel.overwrites_for(target)
        for perm in allow:
            setattr(overwrite, perm, True)
        for perm in deny:
            setattr(overwrite, perm, False)
        await target_channel.set_permissions(
            target, overwrite=overwrite, reason=f"AI request by {user} ({user.id})"
        )
        name = getattr(target, "name", str(target))
        log.info("AI: %s set permissions for %s in #%s", user, name, target_channel.name)
        return f"Updated permissions for {name} in #{target_channel.name}."

    async def _tool_clear_channel_permission(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_roles")
        if err:
            return err
        target_channel = self._resolve_channel(guild, args.get("channel", ""))
        if not isinstance(target_channel, discord.abc.GuildChannel):
            return "No matching channel found."
        target = self._resolve_target(guild, args.get("target", ""), args.get("target_type"))
        if target is None:
            return "No matching role or member."
        await target_channel.set_permissions(
            target, overwrite=None, reason=f"AI request by {user} ({user.id})"
        )
        name = getattr(target, "name", str(target))
        return f"Cleared the permission overwrite for {name} in #{target_channel.name}."

    async def _tool_create_thread(self, guild, channel, user, args) -> str:
        target = self._resolve_channel(guild, args.get("channel", "")) or channel
        if not isinstance(target, discord.TextChannel):
            return "No matching text channel found."
        private = bool(args.get("private"))
        perm = "create_private_threads" if private else "create_public_threads"
        err = self._guard(user, guild, perm)
        if err:
            return err
        thread_type = discord.ChannelType.private_thread if private else discord.ChannelType.public_thread
        thread = await target.create_thread(
            name=(args.get("name") or "thread").strip(),
            type=thread_type,
            reason=f"AI request by {user} ({user.id})",
        )
        log.info("AI: %s created thread %s in #%s", user, thread, target.name)
        return f"Created thread {thread.mention} in #{target.name}."

    async def _tool_delete_thread(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_threads")
        if err:
            return err
        thread = self._resolve_channel(guild, args.get("thread", ""))
        if not isinstance(thread, discord.Thread):
            return "No matching thread found."
        name = thread.name
        await thread.delete(reason=f"AI request by {user} ({user.id})")
        log.info("AI: %s deleted thread %s", user, name)
        return f"Deleted thread '{name}'."

    async def _tool_create_webhook(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_webhooks")
        if err:
            return err
        target = self._resolve_channel(guild, args.get("channel", "")) or channel
        if not isinstance(target, discord.TextChannel):
            return "No matching text channel found."
        webhook = await target.create_webhook(
            name=(args.get("name") or "webhook")[:80], reason=f"AI request by {user} ({user.id})"
        )
        log.info("AI: %s created webhook '%s' in #%s", user, webhook.name, target.name)
        return (
            f"Created webhook '{webhook.name}' (id {webhook.id}) in #{target.name}. "
            "Its URL is in Server Settings > Integrations (kept out of chat on purpose)."
        )

    async def _tool_delete_webhook(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_webhooks")
        if err:
            return err
        raw = str(args.get("webhook", "")).strip()
        webhooks = await guild.webhooks()
        match = next((w for w in webhooks if str(w.id) == raw or w.name.lower() == raw.lower()), None)
        if match is None:
            return "No matching webhook found."
        name = match.name
        await match.delete(reason=f"AI request by {user} ({user.id})")
        log.info("AI: %s deleted webhook '%s'", user, name)
        return f"Deleted webhook '{name}'."

    async def _tool_create_emoji(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, EXPRESSION_PERM)
        if err:
            return err
        name = (args.get("name") or "").strip()
        url = (args.get("image_url") or "").strip()
        if not name or not url:
            return "An emoji name and image URL are required."
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                return "Couldn't download that image."
            data = await resp.read()
        if len(data) > 256 * 1024:
            return "That image is larger than 256 KB."
        emoji = await guild.create_custom_emoji(
            name=name, image=data, reason=f"AI request by {user} ({user.id})"
        )
        log.info("AI: %s created emoji :%s:", user, emoji.name)
        return f"Created emoji :{emoji.name}:."

    async def _tool_delete_emoji(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, EXPRESSION_PERM)
        if err:
            return err
        name = (args.get("emoji") or "").strip().strip(":")
        emoji = discord.utils.get(guild.emojis, name=name)
        if emoji is None:
            return "No matching emoji found."
        await emoji.delete(reason=f"AI request by {user} ({user.id})")
        log.info("AI: %s deleted emoji :%s:", user, name)
        return f"Deleted emoji :{name}:."

    async def _tool_edit_server(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_guild")
        if err:
            return err
        kwargs = {}
        if args.get("name"):
            kwargs["name"] = args["name"]
        if args.get("verification_level"):
            try:
                kwargs["verification_level"] = discord.VerificationLevel[args["verification_level"]]
            except KeyError:
                return "Invalid verification level."
        if args.get("explicit_content_filter"):
            try:
                kwargs["explicit_content_filter"] = discord.ContentFilter[args["explicit_content_filter"]]
            except KeyError:
                return "Invalid content filter level."
        if args.get("default_notifications"):
            try:
                kwargs["default_notifications"] = discord.NotificationLevel[args["default_notifications"]]
            except KeyError:
                return "Invalid notification level."
        if args.get("system_channel"):
            system = self._resolve_channel(guild, args["system_channel"])
            if not isinstance(system, discord.TextChannel):
                return "No matching system channel found."
            kwargs["system_channel"] = system
        if args.get("afk_channel"):
            afk = self._resolve_channel(guild, args["afk_channel"])
            if not isinstance(afk, discord.VoiceChannel):
                return "No matching AFK voice channel found."
            kwargs["afk_channel"] = afk
        if args.get("afk_timeout_seconds") is not None:
            kwargs["afk_timeout"] = int(args["afk_timeout_seconds"])
        if args.get("require_2fa") is not None:
            kwargs["mfa_level"] = (
                discord.MFALevel.require_2fa if args["require_2fa"] else discord.MFALevel.disabled
            )
        if args.get("vanity_code"):
            kwargs["vanity_code"] = args["vanity_code"]
        if args.get("rules_channel"):
            rules = self._resolve_channel(guild, args["rules_channel"])
            if isinstance(rules, discord.TextChannel):
                kwargs["rules_channel"] = rules
        if args.get("public_updates_channel"):
            updates = self._resolve_channel(guild, args["public_updates_channel"])
            if isinstance(updates, discord.TextChannel):
                kwargs["public_updates_channel"] = updates
        if (
            args.get("suppress_join_notifications") is not None
            or args.get("suppress_boost_notifications") is not None
        ):
            flags = guild.system_channel_flags
            kwargs["system_channel_flags"] = discord.SystemChannelFlags(
                suppress_join_notifications=bool(
                    args.get("suppress_join_notifications", flags.suppress_join_notifications)
                ),
                suppress_premium_subscriptions=bool(
                    args.get("suppress_boost_notifications", flags.suppress_premium_subscriptions)
                ),
            )
        if not kwargs:
            return "Nothing to change."
        await guild.edit(reason=f"AI request by {user} ({user.id})", **kwargs)
        log.info("AI: %s edited server %s", user, list(kwargs))
        return f"Updated server settings: {', '.join(kwargs)}."

    async def _tool_set_server_icon(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_guild")
        if err:
            return err
        url = (args.get("image_url") or "").strip()
        if not url:
            return "An image URL is required."
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                return "Couldn't download that image."
            data = await resp.read()
        if len(data) > 256 * 1024:
            return "That image is larger than 256 KB."
        await guild.edit(icon=data, reason=f"AI request by {user} ({user.id})")
        log.info("AI: %s changed the server icon", user)
        return "Updated the server icon."

    # ----------------------------------------------------------- read: extras

    async def _tool_list_emojis(self, guild, channel, user, args) -> str:
        if not guild.emojis:
            return "No custom emojis."
        return "Emojis:\n" + "\n".join(
            f"- :{e.name}: (id {e.id}, animated={e.animated})" for e in guild.emojis[:MAX_LIST_ITEMS]
        )

    async def _tool_list_stickers(self, guild, channel, user, args) -> str:
        if not guild.stickers:
            return "No stickers."
        return "Stickers:\n" + "\n".join(f"- {s.name} (id {s.id})" for s in guild.stickers[:MAX_LIST_ITEMS])

    async def _tool_list_webhooks(self, guild, channel, user, args) -> str:
        if args.get("channel"):
            target = self._resolve_channel(guild, args["channel"])
            if not isinstance(target, discord.TextChannel):
                return "No matching text channel found."
            webhooks = await target.webhooks()
        else:
            webhooks = await guild.webhooks()
        if not webhooks:
            return "No webhooks."
        return "Webhooks:\n" + "\n".join(
            f"- {w.name} (id {w.id}, #{getattr(w.channel, 'name', '?')})" for w in webhooks[:MAX_LIST_ITEMS]
        )

    async def _tool_read_audit_log(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "view_audit_log")
        if err:
            return err
        limit = max(1, min(int(args.get("limit", 20)), 50))
        entries = [entry async for entry in guild.audit_logs(limit=limit)]
        if not entries:
            return "The audit log is empty."
        lines = []
        for entry in entries:
            target = getattr(entry.target, "name", None) or getattr(entry.target, "id", "")
            when = entry.created_at.strftime("%Y-%m-%d %H:%M")
            lines.append(f"- {when} {entry.user}: {entry.action.name} {target}")
        return "Audit log (newest first):\n" + "\n".join(lines)

    # ---------------------------------------------------------- message tools

    def _target_channel(self, guild, channel, args):
        if args.get("channel"):
            return self._resolve_channel(guild, args["channel"])
        return channel

    async def _tool_send_message(self, guild, channel, user, args) -> str:
        target = self._target_channel(guild, channel, args)
        err = self._guard_message(user, target)
        if err:
            return err
        content = (args.get("content") or "").strip()
        if not content:
            return "There's nothing to send."
        kwargs = {}
        if args.get("reply_to_message_id"):
            try:
                kwargs["reference"] = target.get_partial_message(int(args["reply_to_message_id"]))
            except (ValueError, TypeError, AttributeError):
                pass
        message = await target.send(content[:1900], **kwargs)
        log.info("AI: %s posted a message in #%s", user, target.name)
        return f"Posted in #{target.name} (message id {message.id})."

    @staticmethod
    def _decorate_field_name(name: str, index: int) -> str:
        """Style a list field name as 'emoji N · Title' (idempotent)."""
        name = (name or "").strip()
        if not name:
            return name
        if " · " in name or ord(name[0]) > 0x2000:  # already decorated
            return name
        lowered = name.lower()
        emoji = next((e for keys, e in _EMOJI_HINTS if any(k in lowered for k in keys)), None)
        if emoji is None:
            emoji = _DEFAULT_EMOJIS[index % len(_DEFAULT_EMOJIS)]
        match = re.match(r"^(\d+)[.)]\s*(.+)$", name)
        if match:
            return f"{emoji} {match.group(1)} · {match.group(2).strip()}"
        return f"{emoji} {name}"

    @staticmethod
    def _split_list(text: str):
        """Split a list-like description into (preamble, [(num, title, body)]).

        Used so a numbered/bulleted list is rendered as separate embed fields
        regardless of how the model formatted its arguments.
        """
        preamble: list[str] = []
        items: list[list] = []
        current = None
        for raw in text.splitlines():
            line = raw.strip()
            match = _ITEM_RE.match(line)
            if match and len((match.group("title") or "").strip()) <= 100:
                if current is not None:
                    items.append(current)
                title = match.group("title").strip().strip("*").strip()
                current = [match.group("num"), title, []]
            elif current is not None:
                if line:
                    current[2].append(line)
            elif line:
                preamble.append(line)
        if current is not None:
            items.append(current)
        return "\n".join(preamble), [(num, title, " ".join(body)) for num, title, body in items]

    async def _tool_send_embed(self, guild, channel, user, args) -> str:
        target = self._target_channel(guild, channel, args)
        err = self._guard_message(user, target)
        if err:
            return err

        description = (args.get("description") or "").strip() or None
        fields = list(args.get("fields") or [])
        auto_split = False
        if not fields and description:
            preamble, items = self._split_list(description)
            if len(items) >= 3:
                description = preamble or None
                fields = [
                    {"name": (f"{num}. {title}" if num else title), "value": body or "\u200b"}
                    for num, title, body in items
                ]
                auto_split = True

        embed = discord.Embed()
        if args.get("title"):
            embed.title = str(args["title"])[:256]
        if description:
            embed.description = description[:4000]
        colour = args.get("colour")
        if colour:
            try:
                embed.colour = discord.Colour.from_str(colour)
            except ValueError:
                return f"'{colour}' isn't a valid hex colour."
        else:
            # Fall back to the bot's configured brand colour so embeds never
            # come out default-grey just because the model omitted it.
            try:
                embed.colour = discord.Colour(await self.bot._config.color())
            except Exception:  # noqa: BLE001 - colour is cosmetic
                pass
        if args.get("footer"):
            embed.set_footer(text=str(args["footer"])[:2048])
        if args.get("image_url"):
            embed.set_image(url=args["image_url"])
        if args.get("thumbnail_url"):
            embed.set_thumbnail(url=args["thumbnail_url"])
        for index, field in enumerate(fields[:25]):
            name = self._decorate_field_name(str(field.get("name", "")), index)
            embed.add_field(
                name=name[:256] or "\u200b",
                value=str(field.get("value", ""))[:1024] or "\u200b",
                inline=bool(field.get("inline", False)),
            )
        message = await target.send(embed=embed)
        log.info(
            "AI: %s posted an embed in #%s%s",
            user,
            target.name,
            " (auto-split into fields)" if auto_split else "",
        )
        return f"Posted an embed in #{target.name} (message id {message.id})."

    async def _tool_read_messages(self, guild, channel, user, args) -> str:
        target = self._target_channel(guild, channel, args)
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            return "No matching text channel found."
        if args.get("message_id"):
            message = await self._fetch_message(target, args["message_id"])
            if message is None:
                return "I couldn't find that message."
            return "Message:\n" + self._describe_message(message)
        limit = max(1, min(int(args.get("limit", 20)), 50))
        messages = [m async for m in target.history(limit=limit)]
        if not messages:
            return "No messages."
        return "Recent messages (oldest first):\n" + "\n".join(
            self._describe_message(message) for message in reversed(messages)
        )

    async def _tool_edit_message(self, guild, channel, user, args) -> str:
        target = self._target_channel(guild, channel, args)
        err = self._guard_message(user, target)
        if err:
            return err
        message = await self._fetch_message(target, args.get("message_id"))
        if message is None:
            return "I couldn't find that message."
        if message.author.id != self.bot.user.id:
            return "I can only edit my own messages."
        await message.edit(content=(args.get("content") or "")[:1900])
        return "Edited the message."

    async def _tool_delete_message(self, guild, channel, user, args) -> str:
        target = self._target_channel(guild, channel, args)
        err = self._guard_message(user, target)
        if err:
            return err
        message = await self._fetch_message(target, args.get("message_id"))
        if message is None:
            return "I couldn't find that message."
        await message.delete()
        return "Deleted the message."

    async def _tool_pin_message(self, guild, channel, user, args) -> str:
        target = self._target_channel(guild, channel, args)
        err = self._guard_message(user, target)
        if err:
            return err
        message = await self._fetch_message(target, args.get("message_id"))
        if message is None:
            return "I couldn't find that message."
        await message.pin()
        return "Pinned the message."

    async def _tool_unpin_message(self, guild, channel, user, args) -> str:
        target = self._target_channel(guild, channel, args)
        err = self._guard_message(user, target)
        if err:
            return err
        message = await self._fetch_message(target, args.get("message_id"))
        if message is None:
            return "I couldn't find that message."
        await message.unpin()
        return "Unpinned the message."

    async def _tool_react_to_message(self, guild, channel, user, args) -> str:
        target = self._target_channel(guild, channel, args)
        err = self._guard_message(user, target)
        if err:
            return err
        message = await self._fetch_message(target, args.get("message_id"))
        if message is None:
            return "I couldn't find that message."
        emoji = (args.get("emoji") or "").strip()
        if not emoji:
            return "An emoji is required."
        await message.add_reaction(emoji)
        return f"Reacted with {emoji}."

    async def _tool_dm_user(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_guild")
        if err:
            return err
        member = self._resolve_member(guild, args.get("user", ""))
        if member is None:
            return "No matching member found."
        content = (args.get("content") or "").strip()
        if not content:
            return "There's nothing to send."
        try:
            await member.send(content[:1900])
        except discord.Forbidden:
            return f"I couldn't DM {member} - they may have DMs closed."
        log.info("AI: %s DMed %s", user, member)
        return f"Sent a DM to {member}."

    # ---------------------------------------------------------- voice tools

    async def _tool_voice_move(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "move_members")
        if err:
            return err
        member = self._resolve_member(guild, args.get("member", ""))
        if member is None:
            return "No matching member found."
        raw = (args.get("channel") or "").strip()
        if raw.lower() in ("disconnect", "none", "off", ""):
            if member.voice is None:
                return f"{member} isn't in a voice channel."
            await member.move_to(None, reason=f"AI request by {user} ({user.id})")
            return f"Disconnected {member} from voice."
        target = self._resolve_channel(guild, raw)
        if not isinstance(target, (discord.VoiceChannel, discord.StageChannel)):
            return "No matching voice channel found."
        await member.move_to(target, reason=f"AI request by {user} ({user.id})")
        log.info("AI: %s moved %s to voice channel %s", user, member, target.name)
        return f"Moved {member} to {target.name}."

    async def _tool_voice_mute(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "mute_members")
        if err:
            return err
        member = self._resolve_member(guild, args.get("member", ""))
        if member is None:
            return "No matching member found."
        if member.voice is None:
            return f"{member} isn't in a voice channel."
        muted = bool(args.get("mute"))
        await member.edit(mute=muted, reason=f"AI request by {user} ({user.id})")
        return f"{'Server-muted' if muted else 'Unmuted'} {member}."

    async def _tool_voice_deafen(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "deafen_members")
        if err:
            return err
        member = self._resolve_member(guild, args.get("member", ""))
        if member is None:
            return "No matching member found."
        if member.voice is None:
            return f"{member} isn't in a voice channel."
        deafened = bool(args.get("deafen"))
        await member.edit(deafen=deafened, reason=f"AI request by {user} ({user.id})")
        return f"{'Server-deafened' if deafened else 'Undeafened'} {member}."

    # --------------------------------------------------- scheduled event tools

    async def _tool_create_scheduled_event(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_events")
        if err:
            return err
        name = (args.get("name") or "").strip()
        if not name:
            return "A name is required."
        start = self._parse_time(args.get("start_time"))
        if start is None:
            return "Give a start time like '2026-10-02T18:00' or 'in 2h'."
        entity = (args.get("entity_type") or "voice").lower()
        kwargs = {
            "name": name,
            "start_time": start,
            "description": args.get("description") or None,
            "reason": f"AI request by {user} ({user.id})",
        }
        if args.get("end_time"):
            end = self._parse_time(args["end_time"])
            if end:
                kwargs["end_time"] = end
        if entity == "external":
            kwargs["entity_type"] = discord.EntityType.external
            kwargs["location"] = args.get("location") or "Discord"
        elif entity == "stage":
            stage = self._resolve_channel(guild, args.get("channel", ""))
            if not isinstance(stage, discord.StageChannel):
                return "No matching stage channel."
            kwargs["entity_type"] = discord.EntityType.stage_instance
            kwargs["channel"] = stage
        else:
            voice = self._resolve_channel(guild, args.get("channel", ""))
            if not isinstance(voice, discord.VoiceChannel):
                return "No matching voice channel."
            kwargs["entity_type"] = discord.EntityType.voice
            kwargs["channel"] = voice
        event = await guild.create_scheduled_event(**kwargs)
        log.info("AI: %s created scheduled event '%s'", user, event.name)
        return f"Created event '{event.name}' (id {event.id}) at {event.start_time:%Y-%m-%d %H:%M} UTC."

    async def _tool_edit_scheduled_event(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_events")
        if err:
            return err
        event = self._resolve_event(guild, args.get("event", ""))
        if event is None:
            return "No matching scheduled event."
        kwargs = {}
        if args.get("name"):
            kwargs["name"] = args["name"]
        if args.get("description"):
            kwargs["description"] = args["description"]
        if args.get("start_time"):
            start = self._parse_time(args["start_time"])
            if start:
                kwargs["start_time"] = start
        if args.get("end_time"):
            end = self._parse_time(args["end_time"])
            if end:
                kwargs["end_time"] = end
        if not kwargs:
            return "Nothing to change."
        await event.edit(**kwargs)
        return f"Updated event '{event.name}'."

    async def _tool_delete_scheduled_event(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_events")
        if err:
            return err
        event = self._resolve_event(guild, args.get("event", ""))
        if event is None:
            return "No matching scheduled event."
        name = event.name
        await event.delete()
        log.info("AI: %s deleted scheduled event '%s'", user, name)
        return f"Deleted event '{name}'."

    # ------------------------------------------------------- automod tools

    async def _tool_create_automod_rule(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_guild")
        if err:
            return err
        name = (args.get("name") or "").strip()
        if not name:
            return "A name is required."
        actions = []
        if args.get("block_message", True):
            actions.append(discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message))
        if args.get("alert_channel"):
            alert = self._resolve_channel(guild, args["alert_channel"])
            if isinstance(alert, discord.TextChannel):
                actions.append(
                    discord.AutoModRuleAction(
                        type=discord.AutoModRuleActionType.send_alert_message, channel_id=alert.id
                    )
                )
        if args.get("timeout_seconds"):
            seconds = self._parse_duration(args["timeout_seconds"])
            if seconds:
                actions.append(
                    discord.AutoModRuleAction(
                        type=discord.AutoModRuleActionType.timeout, duration=timedelta(seconds=seconds)
                    )
                )
        if not actions:
            actions.append(discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message))

        trigger_type = (args.get("trigger_type") or "").lower()
        if trigger_type == "keyword":
            keywords = args.get("keywords") or []
            if not keywords:
                return "Provide at least one keyword."
            trigger = discord.AutoModTrigger(
                type=discord.AutoModRuleTriggerType.keyword, keyword_filter=list(keywords)
            )
        elif trigger_type == "keyword_preset":
            presets = [str(p).upper() for p in (args.get("presets") or [])]
            if not presets:
                return "Provide presets, e.g. profanity, sexual_content, slurs."
            trigger = discord.AutoModTrigger(
                type=discord.AutoModRuleTriggerType.keyword_preset, presets=presets
            )
        elif trigger_type == "mention_spam":
            trigger = discord.AutoModTrigger(
                type=discord.AutoModRuleTriggerType.mention_spam,
                mention_limit=max(1, int(args.get("mention_limit") or 5)),
            )
        elif trigger_type == "spam":
            trigger = discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.spam)
        else:
            return "trigger_type must be keyword, keyword_preset, mention_spam or spam."

        rule = await guild.create_automod_rule(
            name=name,
            event_type=discord.AutoModRuleEventType.message_send,
            trigger=trigger,
            actions=actions,
            reason=f"AI request by {user} ({user.id})",
        )
        log.info("AI: %s created automod rule '%s'", user, rule.name)
        return f"Created AutoMod rule '{rule.name}' (id {rule.id})."

    async def _tool_delete_automod_rule(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_guild")
        if err:
            return err
        rule = await self._resolve_automod_rule(guild, args.get("rule", ""))
        if rule is None:
            return "No matching AutoMod rule."
        name = rule.name
        await rule.delete()
        log.info("AI: %s deleted automod rule '%s'", user, name)
        return f"Deleted AutoMod rule '{name}'."

    async def _tool_list_automod_rules(self, guild, channel, user, args) -> str:
        rules = await guild.fetch_automod_rules()
        if not rules:
            return "No AutoMod rules."
        lines = [
            f"- {r.name} (id {r.id}) trigger={r.trigger.type.name}, enabled={r.enabled}" for r in rules
        ]
        return "AutoMod rules:\n" + "\n".join(lines)

    # --------------------------------------------------------- reorder tools

    async def _tool_move_role(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_roles")
        if err:
            return err
        role = self._resolve_role(guild, args.get("role", ""))
        if role is None or role.is_default():
            return "No movable role matched that name."
        max_position = guild.me.top_role.position - 1
        if user.id != guild.owner_id:
            max_position = min(
                max_position, getattr(user, "top_role", guild.me.top_role).position - 1
            )
        position = max(1, min(int(args.get("position", 1)), max_position))
        await role.edit(position=position, reason=f"AI request by {user} ({user.id})")
        log.info("AI: %s moved role '%s' to %d", user, role.name, role.position)
        return f"Moved '{role.name}' to position {role.position}."

    async def _tool_move_channel(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_channels")
        if err:
            return err
        target = self._resolve_channel(guild, args.get("channel", ""))
        if not isinstance(target, discord.abc.GuildChannel):
            return "No matching channel found."
        kwargs = {}
        raw_category = args.get("category")
        if raw_category:
            if str(raw_category).lower() in ("none", "top", "no category"):
                kwargs["category"] = None
            else:
                category = self._resolve_channel(guild, raw_category)
                if not isinstance(category, discord.CategoryChannel):
                    return "No matching category."
                kwargs["category"] = category
        if args.get("position") is not None:
            kwargs["position"] = int(args["position"])
        if not kwargs:
            return "Give a category or a position."
        await target.edit(reason=f"AI request by {user} ({user.id})", **kwargs)
        return f"Moved '{target.name}'."

    # ------------------------------------------------ banner / prune / stickers

    async def _tool_set_server_banner(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "manage_guild")
        if err:
            return err
        url = (args.get("image_url") or "").strip()
        if not url:
            return "An image URL is required."
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                return "Couldn't download that image."
            data = await resp.read()
        if len(data) > 10 * 1024 * 1024:
            return "That image is too large."
        try:
            await guild.edit(banner=data, reason=f"AI request by {user} ({user.id})")
        except discord.HTTPException as exc:
            return f"Discord refused the banner (needs boost level 2): {exc}"
        return "Updated the server banner."

    async def _tool_prune_members(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, "kick_members")
        if err:
            return err
        days = max(1, min(int(args.get("days", 7)), 30))
        try:
            count = await guild.prune_members(
                days=days,
                compute_prune_count=True,
                reason=args.get("reason") or f"AI request by {user} ({user.id})",
            )
        except discord.Forbidden:
            return "I don't have permission to prune members."
        log.info("AI: %s pruned members inactive %d+ days", user, days)
        return f"Pruned {count} member(s) inactive for {days}+ days."

    async def _tool_create_sticker(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, EXPRESSION_PERM)
        if err:
            return err
        name = (args.get("name") or "").strip()
        description = (args.get("description") or "").strip()
        tags = (args.get("tags") or "").strip()
        url = (args.get("image_url") or "").strip()
        if not all([name, description, tags, url]):
            return "name, description, tags and image_url are all required."
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                return "Couldn't download that image."
            data = await resp.read()
        if len(data) > 512 * 1024:
            return "That image is larger than 512 KB."
        file = discord.File(io.BytesIO(data), filename="sticker.png")
        try:
            sticker = await guild.create_sticker(
                name=name,
                description=description,
                emoji=tags,
                file=file,
                reason=f"AI request by {user} ({user.id})",
            )
        except discord.HTTPException as exc:
            return f"Discord refused the sticker (needs sticker slots / 320x320 PNG): {exc}"
        log.info("AI: %s created sticker '%s'", user, sticker.name)
        return f"Created sticker '{sticker.name}'."

    async def _tool_delete_sticker(self, guild, channel, user, args) -> str:
        err = self._guard(user, guild, EXPRESSION_PERM)
        if err:
            return err
        name = (args.get("sticker") or "").strip()
        sticker = discord.utils.get(guild.stickers, name=name)
        if sticker is None:
            sticker = discord.utils.get(await guild.fetch_stickers(), name=name)
        if sticker is None:
            return "No matching sticker found."
        await guild.delete_sticker(sticker)
        return f"Deleted sticker '{name}'."

    async def _tool_list_scheduled_events(self, guild, channel, user, args) -> str:
        events = guild.scheduled_events
        if not events:
            return "No scheduled events."
        lines = []
        for event in events:
            when = event.start_time.strftime("%Y-%m-%d %H:%M") if event.start_time else "?"
            where = getattr(event.channel, "name", None) or (event.location or "")
            lines.append(f"- {event.name} (id {event.id}) {event.status.name}, starts {when} UTC, {where}")
        return "Scheduled events:\n" + "\n".join(lines)

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
        try:
            async with ctx.typing():
                text = await self._answer(ctx.guild, ctx.channel, ctx.author, prompt)
        except Exception:  # noqa: BLE001
            log.exception("DeepSeek command failed in guild %s", ctx.guild.id)
            text = "Something went wrong handling that request - check the bot logs."
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

    @aiset.command(name="actions")
    async def aiset_actions(self, ctx: commands.Context, enabled: bool):
        """Allow the AI to perform actions (with the requester's permissions)."""
        await self.config.guild(ctx.guild).allow_actions.set(enabled)
        await ctx.tick()

    @aiset.command(name="channel")
    async def aiset_channel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Set an AI channel (no prefix needed), or omit the channel to clear it."""
        conf = self.config.guild(ctx.guild)
        if channel is None:
            await conf.ai_channel.set(None)
            return await ctx.send("AI channel cleared.")
        await conf.ai_channel.set(channel.id)
        await ctx.send(f"{channel.mention} is now an AI channel - every message there gets a reply.")

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
            f"Model: {settings['model']}\nHistory: {settings['max_history']} messages\n"
            f"Max tokens: {settings['max_tokens']}\nThinking: {settings['thinking']}\n"
            f"Actions: {settings['allow_actions']}\nAI channel: {channel}\n"
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
            if (mention_ok or replied_to_bot) and self._warn_ok(message.author.id):
                try:
                    await message.reply(
                        "I'm not connected to DeepSeek yet - set an API key with `!set api deepseek api_key <key>`.",
                        mention_author=False,
                    )
                except discord.HTTPException:
                    pass
            return

        if not self._cooldown_ok(message.author.id):
            return

        try:
            async with message.channel.typing():
                text = await self._answer(message.guild, message.channel, message.author, prompt)
        except (discord.Forbidden, discord.HTTPException):
            return
        except Exception:  # noqa: BLE001 - never leave the user with no reply
            log.exception("DeepSeek failed to answer in guild %s", message.guild.id)
            text = "Something went wrong handling that request - check the bot logs."

        async def send(content: str) -> None:
            try:
                if in_ai_channel and not (mentioned or replied_to_bot):
                    await message.channel.send(content)
                else:
                    await message.reply(content, mention_author=False)
            except discord.HTTPException:
                await message.channel.send(content)

        try:
            await self._send(send, text)
        except (discord.Forbidden, discord.HTTPException):
            return
