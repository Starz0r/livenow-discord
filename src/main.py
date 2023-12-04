import asyncio
import os
import sys
from typing import Dict, Final

import disnake
import structlog
from disnake.abc import PrivateChannel
from disnake.ext import commands
from twitchAPI.eventsub.webhook import EventSubWebhook
from twitchAPI.helper import first
from twitchAPI.object.eventsub import StreamOfflineEvent, StreamOnlineEvent
from twitchAPI.twitch import Twitch

TWITCH_APP_ID: Final[str] = os.environ.get("TWITCH_APP_ID")
TWITCH_APP_SECRET: Final[str] = os.environ.get("TWITCH_APP_SECRET")
TWITCH_EVENTSUB_URL: Final[str] = os.environ.get("TWITCH_EVENTSUB_URL")
TWITCH_EVENTSUB: EventSubWebhook
TWITCH: Twitch

DISCORD_BOT: commands.InteractionBot = commands.InteractionBot()
DISCORD_TOKEN: Final[str] = os.environ.get("DISCORD_TOKEN")
DISCORD_CHANNEL: Final[str] = os.environ.get("DISCORD_CHANNEL")

RETCODE: int = 0
EVLOOP: Final[asyncio.AbstractEventLoop] = asyncio.new_event_loop()
LOGGER: Final[structlog.stdlib.BoundLogger] = structlog.getLogger()
MSGS: Dict[str, disnake.message.Message] = {}


async def on_stream_offline(payload: StreamOfflineEvent):
    msg = MSGS.pop(payload.event.broadcaster_user_name)
    await msg.delete()


async def on_stream_online(payload: StreamOnlineEvent):
    ch = DISCORD_BOT.get_channel(int(DISCORD_CHANNEL))
    if ch is None or ch is PrivateChannel:
        return LOGGER.error(
            "attempted to notify users with Stream Online message to private or non-existent channel",
            payload=payload,
            ch=ch,
            target=DISCORD_CHANNEL,
        )
    msg = await ch.send(
        f"https://twitch.tv/{payload.event.broadcaster_user_name}")
    MSGS.update({f"{payload.event.broadcaster_user_name}": msg})


@DISCORD_BOT.slash_command()
async def add_stream(ctx: disnake.ApplicationCommandInteraction, stream: str):
    twitch_user = await first(TWITCH.get_users(logins=[stream]))
    if twitch_user is None:
        return await ctx.response.send_message(
            f"could not find Twitch User with matching name of: {stream}.")
    await asyncio.gather(
        TWITCH_EVENTSUB.listen_stream_online(twitch_user.id, on_stream_online),
        TWITCH_EVENTSUB.listen_stream_offline(twitch_user.id,
                                              on_stream_offline),
    )


async def main() -> int:
    global TWITCH
    global TWITCH_EVENTSUB

    LOGGER.info("connecting to the Twitch API")
    TWITCH = await Twitch(TWITCH_APP_ID, TWITCH_APP_SECRET)

    LOGGER.info("setting up EventSub handlers")
    TWITCH_EVENTSUB = EventSubWebhook(TWITCH_EVENTSUB_URL, 8080, TWITCH)
    TWITCH_EVENTSUB.start()

    # run discord bot until interrupted
    LOGGER.info("all ready, running Discord bot until interrupted")
    try:
        EVLOOP.run_until_complete(
            DISCORD_BOT.start(DISCORD_TOKEN, reconnect=True))
    except KeyboardInterrupt:
        LOGGER.info("stop called! shutting down...")
    finally:
        LOGGER.info("gracefully disconnecting from all services")
        await asyncio.gather(TWITCH_EVENTSUB.stop(), TWITCH.close(),
                             DISCORD_BOT.close())
    return RETCODE


if __name__ == "__main__":
    sys.exit(EVLOOP.run_until_complete(main()))
