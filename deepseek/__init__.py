from .deepseek import DeepSeek

__red_end_user_data_statement__ = (
    "Conversation context is kept in memory only and is not written to disk "
    "unless long-term memory is enabled. Prompts and responses are sent to the "
    "DeepSeek API for processing; with long-term memory enabled, exchanges are "
    "also stored locally via mem0."
)


async def setup(bot):
    await bot.add_cog(DeepSeek(bot))
