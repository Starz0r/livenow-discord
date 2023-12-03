import asyncio
import os
import signal
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

DISCORD_BOT: Final[commands.InteractionBot] = commands.InteractionBot()
DISCORD_TOKEN: Final[str] = os.environ.get("DISCORD_TOKEN")
DISCORD_CHANNEL: Final[str] = os.environ.get("DISCORD_CHANNEL")

RETCODE: int = 0
EVLOOP: Final[asyncio.AbstractEventLoop] = asyncio.new_event_loop()
LOGGER: Final[structlog.stdlib.BoundLogger] = structlog.getLogger()
RUNNING: bool = True
MSGS: Dict[str, disnake.message.Message] = {}


async def on_stream_offline(payload: StreamOfflineEvent):
    msg = MSGS.pop(payload.event.broadcaster_user_name)
    await msg.delete()


async def on_stream_online(payload: StreamOnlineEvent):
    ch = DISCORD_BOT.get_channel(int(DISCORD_CHANNEL))
    if ch is None or ch is PrivateChannel:
        return LOGGER.error(
            "Attempted to notify users with Stream Online message to private or non-existent channel",
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
            f"Could not find Twitch User with matching name of: {stream}.")
    await asyncio.gather(
        TWITCH_EVENTSUB.listen_stream_online(twitch_user.id, on_stream_online),
        TWITCH_EVENTSUB.listen_stream_offline(twitch_user.id,
                                              on_stream_offline),
    )


def stop_running(sig, frame):
    LOGGER.debug("stop called! shutting down...", sig=sig)
    global RUNNING
    RUNNING = False


async def main() -> int:
    global TWITCH
    global TWITCH_EVENTSUB

    TWITCH = await Twitch(TWITCH_APP_ID, TWITCH_APP_SECRET)
    TWITCH_EVENTSUB = EventSubWebhook(TWITCH_EVENTSUB_URL, 8080, TWITCH)
    TWITCH_EVENTSUB.start()

    # start discord bot
    DISCORD_BOT.run(DISCORD_TOKEN)

    # run until an interrupt is received
    signal.signal(signal.SIGINT, stop_running)
    while RUNNING:
        await asyncio.sleep(1)
    await asyncio.gather(TWITCH_EVENTSUB.stop(), TWITCH.close(),
                         DISCORD_BOT.close())
    return RETCODE


if __name__ == "__main__":
    sys.exit(EVLOOP.run_until_complete(main()))
