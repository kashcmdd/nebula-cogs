# discord-bot-cogs

Custom cogs for the Nebula deployment
([Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot)).

These are original cogs — code written for this deployment, not bundled with Red.

## Cogs

| Cog | What it does |
| --- | --- |
| [deepseek](deepseek/) | A DeepSeek-backed AI assistant: `!ai`, plus mention / reply / dedicated-channel chat. |

## Install

From Discord, as the bot owner:

```
!addpath /absolute/path/to/discord-bot-cogs
!load deepseek
```

`!addpath` persists, and `!load` remembers the cog, so both survive restarts. The
cog requires no extra Python packages (`aiohttp` ships with Red).

## deepseek

Two ways to talk to it, as intended:

**Command**
```
!ai what is a good name for a cat?
!ask summarise this: ...
```

**Natural language**
- **Mention** it: `@Nebula tell me a joke`
- **Reply** to one of its messages to continue a thread
- **AI channel** (opt-in): with a channel set, every message there is a prompt —
  no prefix or mention needed. Set with `!aiset channel #channel`.

Conversation context is kept per channel and user, in memory.

### API key

The key is read from Red's shared API tokens, service `deepseek`:
```
!set api deepseek api_key <your key>
```

Models are `deepseek-flash` (fast, default) and `deepseek-v4-pro` (reasoning).
The legacy `deepseek-chat` / `deepseek-reasoner` names were retired in July 2026
and will error.

### Scope

By default the persona is scoped to **Discord-only** help: it declines
off-topic requests and steers back to servers, roles, permissions, moderation
and this bot's commands. View the current prompt with `!aiset system`, and
replace it with your own with `!aiset system <text>` (a longer prompt is easier
to set by editing this cog's `DEFAULT_SYSTEM` and restarting).

### Server tools

**Read (13):** `server_info`, `list_roles`, `list_channels`, `member_info`,
`list_bans`, `list_invites`, `list_emojis`, `list_stickers`, `list_webhooks`,
`read_audit_log`, `read_messages`, `list_scheduled_events`,
`list_automod_rules`.

**Act (53):**
- **Roles:** `add_role`, `remove_role`, `create_role`, `edit_role`,
  `delete_role`, `move_role`
- **Channels:** `create_text_channel`, `create_voice_channel`,
  `create_category`, `delete_channel`, `rename_channel`, `set_channel_topic`,
  `set_slowmode`, `set_channel_lock`, `move_channel`
- **Permissions:** `set_channel_permission`, `clear_channel_permission`
- **Threads:** `create_thread`, `delete_thread`
- **Members:** `kick_member`, `ban_member`, `unban_member`, `timeout_member`,
  `remove_timeout`, `set_nickname`, `prune_members`
- **Voice:** `voice_move`, `voice_mute`, `voice_deafen`
- **Messages:** `send_message`, `send_embed`, `edit_message`, `delete_message`,
  `pin_message`, `unpin_message`, `react_to_message`, `purge_messages`
- **DMs:** `dm_user`
- **Invites:** `create_invite`
- **Webhooks:** `create_webhook`, `delete_webhook`
- **Emojis/stickers:** `create_emoji`, `delete_emoji`, `create_sticker`,
  `delete_sticker`
- **Scheduled events:** `create_scheduled_event`, `edit_scheduled_event`,
  `delete_scheduled_event`
- **AutoMod:** `create_automod_rule`, `delete_automod_rule`
- **Server:** `edit_server`, `set_server_icon`, `set_server_banner`

Every action is checked against the **requester's** Discord permission at
execution time — never the bot's — plus role-hierarchy rules, and is logged. The
AI acts immediately, including purges and deletions; **banning is the only
action that asks for confirmation first**. Disable all actions with
`!aiset actions false`.

### Settings

| Command | What it does |
| --- | --- |
| `!aiset model <name>` | `deepseek-flash` or `deepseek-v4-pro` |
| `!aiset system <text>` | Set the persona / system prompt (omit to view) |
| `!aiset history <n>` | Messages of context kept (0-100, default 10) |
| `!aiset maxtokens <n>` | Max tokens per reply (64-8192, default 800) |
| `!aiset thinking <bool>` | Enable DeepSeek thinking mode (slower, smarter) |
| `!aiset actions <bool>` | Allow role changes by the AI for users with permission |
| `!aiset channel <#chan>` | Set an AI channel (omit to clear) |
| `!aiset mentions <bool>` | Reply when mentioned (default on) |
| `!aiset clear` | Clear all context for the server |
| `!aiset showsettings` | Show current settings |
| `!aiclear` | Forget your context in this channel |

`aiset` requires Manage Server; `ai` is available to everyone (tune with Red's
`!permissions` if needed).

## License

MIT (see `LICENSE`). Depends on Red-DiscordBot (GPLv3), which is not included here.

## A note on cost

Every reply is a billed API call. `deepseek-flash` is cheap and the defaults
favour short replies, but a public AI channel can add up — keep it limited to
trusted channels/roles if you open it up.
