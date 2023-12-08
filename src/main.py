import asyncio
import os
import sys
from typing import Dict, Final, Optional, Tuple

import disnake
import structlog
from disnake.abc import PrivateChannel
from disnake.ext import commands
from twitchAPI.eventsub.webhook import EventSubWebhook
from twitchAPI.helper import first
from twitchAPI.object.eventsub import StreamOfflineEvent, StreamOnlineEvent
from twitchAPI.twitch import Twitch

RETCODE: int = 0
EVLOOP: Final[asyncio.AbstractEventLoop] = asyncio.new_event_loop()
LOGGER: Final[structlog.stdlib.BoundLogger] = structlog.getLogger()
# [twitch name, msg]
ACTIVE_NOTIFICATIONS: Dict[str, disnake.message.Message] = {}
# [discord user id, [twitch name, topic id, topic id]]
REGISTERED_STREAMS: Dict[str, Tuple[str, str, str]] = {}

TWITCH_APP_ID: Final[str] = os.environ.get("TWITCH_APP_ID")
TWITCH_APP_SECRET: Final[str] = os.environ.get("TWITCH_APP_SECRET")
TWITCH_EVENTSUB_URL: Final[str] = os.environ.get("TWITCH_EVENTSUB_URL")
TWITCH_EVENTSUB: EventSubWebhook
TWITCH: Twitch

DISCORD_CMD_SYNC_FLAGS: Final[
    commands.CommandSyncFlags] = commands.CommandSyncFlags.default()
DISCORD_CMD_SYNC_FLAGS.sync_commands_debug = True
DISCORD_CMD_SYNC_FLAGS.sync_commands = True
DISCORD_CMD_SYNC_FLAGS.allow_command_deletion = False
DISCORD_BOT: commands.InteractionBot = commands.InteractionBot(
    command_sync_flags=DISCORD_CMD_SYNC_FLAGS, loop=EVLOOP)
DISCORD_TOKEN: Final[str] = os.environ.get("DISCORD_TOKEN")
DISCORD_CHANNEL: Final[str] = os.environ.get("DISCORD_CHANNEL")
DISCORD_MOD_ROLE: Final[str] = os.environ.get("DISCORD_MOD_ROLE")


async def on_stream_offline(payload: StreamOfflineEvent):
    msg = ACTIVE_NOTIFICATIONS.pop(payload.event.broadcaster_user_name)
    asyncio.run_coroutine_threadsafe(asyncio.Task(msg.delete()), EVLOOP).result()


async def on_stream_online(payload: StreamOnlineEvent):
    ch = DISCORD_BOT.get_channel(int(DISCORD_CHANNEL))
    if ch is None or ch is PrivateChannel:
        return LOGGER.error(
            "attempted to notify users with Stream Online message to private or non-existent channel",
            payload=payload,
            ch=ch,
            target=DISCORD_CHANNEL,
        )
    msg = asyncio.run_coroutine_threadsafe(asyncio.Task(ch.send(
        f"https://twitch.tv/{payload.event.broadcaster_user_name}")), EVLOOP).result()
    ACTIVE_NOTIFICATIONS.update(
        {f"{payload.event.broadcaster_user_name}": msg})


@DISCORD_BOT.slash_command(
    description="Remove stream from the watchlist.",
    options=[
        disnake.Option(name="user",
                       type=disnake.OptionType.user,
                       required=False)
    ],
)
async def remove_stream(
    ctx: disnake.ApplicationCommandInteraction,
    user: Optional[disnake.Member] = None,
) -> None:
    if user is not None:
        if ctx.author.get_role(int(DISCORD_MOD_ROLE)) is not None:
            if str(user.id) in REGISTERED_STREAMS:
                name, topic_a, topic_b = REGISTERED_STREAMS.pop(str(user.id))
                # TODO: also remove live notifications if there are any
                await asyncio.gather(
                    TWITCH_EVENTSUB.unsubscribe_topic(topic_a),
                    TWITCH_EVENTSUB.unsubscribe_topic(topic_b),
                )
                LOGGER.info("unregistering stream",
                            stream=name,
                            topics=(topic_a, topic_b))
                await ctx.send("♻ Stream was removed from the watchlist.")
                return await ctx.delete_original_response(delay=15)

    if str(ctx.author.id) in REGISTERED_STREAMS:
        name, topic_a, topic_b = REGISTERED_STREAMS.pop(str(ctx.author.id))
        # TODO: also remove live notifications if there are any
        await asyncio.gather(
            TWITCH_EVENTSUB.unsubscribe_topic(topic_a),
            TWITCH_EVENTSUB.unsubscribe_topic(topic_b),
        )
        LOGGER.info("unregistering stream",
                    stream=name,
                    topics=(topic_a, topic_b))
        await ctx.send("♻ Stream was removed from the watchlist.")
        return await ctx.delete_original_response(delay=15)

    await ctx.send("Stream was not on the watchlist, nothing was done.")
    return await ctx.delete_original_response(delay=15)


@DISCORD_BOT.slash_command(description="Add stream to the watchlist.")
async def add_stream(ctx: disnake.ApplicationCommandInteraction,
                     stream: str) -> None:
    twitch_user = await first(TWITCH.get_users(logins=[stream]))
    if twitch_user is None:
        await ctx.send(
            f"Could not find Twitch User with matching name of: {stream}.")
        return await ctx.delete_original_response(delay=15)

    # check if discord user already registered a stream
    # TODO: also remove live notifications if there are any
    # QUEST: should mods also be bound to the same limitations?
    if str(ctx.author.id) in REGISTERED_STREAMS:
        name, topic_a, topic_b = REGISTERED_STREAMS.pop(str(ctx.author.id))
        LOGGER.info(
            f"user {name} already registered a stream, in-placing...",
            topics=(topic_a, topic_b),
        )
        await asyncio.gather(
            TWITCH_EVENTSUB.unsubscribe_topic(topic_a),
            TWITCH_EVENTSUB.unsubscribe_topic(topic_b),
        )

    LOGGER.info("subscribing to events")
    topic_a, topic_b = await asyncio.gather(
        TWITCH_EVENTSUB.listen_stream_online(twitch_user.id, on_stream_online),
        TWITCH_EVENTSUB.listen_stream_offline(twitch_user.id,
                                              on_stream_offline),
    )
    LOGGER.info("registering stream",
                stream=twitch_user.id,
                topics=(topic_a, topic_b))
    REGISTERED_STREAMS.update(
        {f"{ctx.author.id}": (twitch_user.display_name, topic_a, topic_b)})
    LOGGER.info("responding to user.")
    await ctx.send("🎥 Stream added to the watchlist!")
    return await ctx.delete_original_response(delay=15)


@DISCORD_BOT.event
async def on_ready():
    LOGGER.info("bot is ready")


async def main() -> int:
    global TWITCH
    global TWITCH_EVENTSUB

    # logging.basicConfig(level=logging.DEBUG)

    LOGGER.info("connecting to the Twitch API")
    TWITCH = await Twitch(TWITCH_APP_ID, TWITCH_APP_SECRET)

    LOGGER.info("setting up EventSub handlers")
    TWITCH_EVENTSUB = EventSubWebhook(TWITCH_EVENTSUB_URL, 8080, TWITCH)
    TWITCH_EVENTSUB.start()

    await TWITCH_EVENTSUB.unsubscribe_all()

    # run discord bot until interrupted
    LOGGER.info("all ready, running Discord bot until interrupted", )
    try:
        await DISCORD_BOT.start(DISCORD_TOKEN, reconnect=True)
    except KeyboardInterrupt:
        LOGGER.info("stop called! shutting down...")
    finally:
        LOGGER.info("gracefully disconnecting from all services")
        await asyncio.gather(TWITCH_EVENTSUB.stop(), TWITCH.close(),
                             DISCORD_BOT.close())
    return RETCODE


if __name__ == "__main__":
    # DISCORD_BOT.run(DISCORD_TOKEN)
    sys.exit(EVLOOP.run_until_complete(main()))