from .deepseek import DeepSeek

__red_end_user_data_statement__ = (
    "Conversation context is kept in memory only and is not written to disk. "
    "Prompts and responses are sent to the DeepSeek API for processing."
)


async def setup(bot):
    await bot.add_cog(DeepSeek(bot))
