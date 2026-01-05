# bot.py
"""Discord bot for managing Minecraft servers on AWS EC2."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import discord
from discord.ext import tasks

from config import BotCfg, ServerCfg
from constants import (
    VERBOSE,
    CONSECUTIVE_EMPTY_LIMIT,
    MONITOR_INTERVAL_MIN,
    AUTO_STOP_DELAY_MIN,
    GRACE_PERIOD_MIN,
    STOP_TIMEOUT_SEC,
)
from errors import AWSError, ProxyError
from models import ServerMonitor
from services import EC2Service, MinecraftService, NFTablesClient


class MinecraftServerBot:
    """Handles Discord bot logic, interactions with AWS EC2, and Minecraft server status."""

    def __init__(self, cfg: BotCfg) -> None:
        self.cfg = cfg
        self.ec2 = EC2Service(cfg.aws_key, cfg.aws_secret)
        self.monitor = ServerMonitor.from_config(cfg.servers)

        # Initialize proxy client if proxy mode is enabled
        self.proxy: NFTablesClient | None = None
        if cfg.proxy_mode:
            self.proxy = NFTablesClient()
            logging.info("Proxy mode enabled")

        # Setup Discord client intents
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)

        # Register event handlers
        self.client.event(self.on_ready)
        self.client.event(self.on_message)

    # -- utilities -----------------------------------------------------------
    def get_server(self, name: str) -> ServerCfg | None:
        """Get a server config by name."""
        return self.cfg.servers.get(name)

    def server_names(self) -> list[str]:
        """Get list of all server names."""
        return list(self.cfg.servers.keys())

    async def say(
        self,
        channel: discord.abc.Messageable | None,
        *args,
        **kw,
    ) -> discord.Message | None:
        """Sends a message to a Discord channel."""
        if channel is None:
            logging.warning("Attempted to send message but no channel is known.")
            return None
        if VERBOSE:
            content = kw.get("embed", None)
            logging.debug(f"-> {channel.id}: {content or args}")
        return await channel.send(*args, **kw)

    async def _send_unknown_server_error(
        self,
        channel: discord.abc.Messageable,
        server_name: str,
    ) -> None:
        """Send an error message for an unknown server name."""
        await self.say(
            channel,
            embed=self.embed(
                f"Unknown server: `{server_name}`. Use `s!list` to see available servers.",
                discord.Color.red(),
            ),
        )

    async def _send_aws_error(
        self,
        channel: discord.abc.Messageable,
        error: AWSError,
    ) -> None:
        """Send an error message for an AWS error."""
        await self.say(
            channel,
            embed=self.embed(f"AWS Error: {error}", discord.Color.red()),
        )

    async def _get_running_servers(self, exclude: str | None = None) -> list[str]:
        """Get list of currently running server names."""
        running = []
        for name, server in self.cfg.servers.items():
            if exclude and name == exclude:
                continue
            try:
                state = await self.ec2.get_state(server)
                if state in ("running", "pending"):
                    running.append(name)
            except AWSError:
                pass  # Skip servers we can't check
        return running

    async def _update_proxy(
        self,
        server: ServerCfg,
        channel: discord.abc.Messageable | None,
    ) -> bool:
        """Update the proxy to route traffic to the server's EC2 instance.

        Returns True if successful, False otherwise.
        """
        if not self.proxy:
            return True  # Proxy not enabled, nothing to do

        try:
            public_ip = await self.ec2.get_public_ip(server)
            if not public_ip:
                logging.warning(f"No public IP for {server.name}, cannot update proxy")
                if channel:
                    await self.say(
                        channel,
                        embed=self.embed(
                            f"Warning: Could not get public IP for `{server.name}`. "
                            "Proxy not updated.",
                            discord.Color.orange(),
                        ),
                    )
                return False

            await self.proxy.set_destination(public_ip)
            logging.info(f"Proxy updated to route to {public_ip} for {server.name}")
            return True

        except ProxyError as e:
            logging.error(f"Failed to update proxy for {server.name}: {e}")
            if channel:
                await self.say(
                    channel,
                    embed=self.embed(
                        f"Warning: Failed to update proxy routing: {e}",
                        discord.Color.orange(),
                    ),
                )
            return False

    async def _clear_proxy(
        self,
        channel: discord.abc.Messageable | None,
    ) -> bool:
        """Clear the proxy routing (reject incoming connections).

        Returns True if successful, False otherwise.
        """
        if not self.proxy:
            return True  # Proxy not enabled, nothing to do

        try:
            await self.proxy.clear_destination()
            logging.info("Proxy routing cleared")
            return True

        except ProxyError as e:
            logging.error(f"Failed to clear proxy: {e}")
            if channel:
                await self.say(
                    channel,
                    embed=self.embed(
                        f"Warning: Failed to clear proxy routing: {e}",
                        discord.Color.orange(),
                    ),
                )
            return False

    # -- embeds ---------------------------------------------------------------
    @staticmethod
    def embed(text: str, colour: discord.Color) -> discord.Embed:
        """Creates a simple Discord embed with text and color."""
        return discord.Embed(description=text, colour=colour)

    async def status_embed(self, server: ServerCfg) -> discord.Embed:
        """Creates a detailed status embed for a specific server."""
        try:
            state = await self.ec2.get_state(server)
        except AWSError:
            return self.embed(
                f"Could not retrieve status for `{server.name}` (AWS Error).",
                discord.Color.dark_red(),
            )

        if state == "running":
            mc_status = await MinecraftService.check_status(server.ip)
            if mc_status.online:
                embed = discord.Embed(
                    title=mc_status.motd or f"Minecraft Server: {server.name}",
                    colour=discord.Color.green(),
                )
                embed.set_thumbnail(url="https://www.packpng.com/static/pack.png")
                embed.add_field(name="Server", value=f"`{server.name}`", inline=True)
                embed.add_field(
                    name="Version",
                    value=mc_status.version or "Unknown",
                    inline=True,
                )
                embed.add_field(
                    name="Players",
                    value=f"{mc_status.players}/{mc_status.max_players or '?'}",
                    inline=True,
                )
                player_list = (
                    "\n".join(mc_status.player_names)
                    if mc_status.players
                    else "No one is online."
                )
                if len(player_list) > 1024:
                    player_list = player_list[:1020] + "..."
                embed.add_field(name="Player List", value=player_list, inline=False)
                # Show maintenance mode indicator
                server_state = self.monitor.get_state(server.name)
                if server_state and server_state.maintenance:
                    embed.add_field(
                        name="Mode",
                        value="Maintenance (auto-stop paused)",
                        inline=False,
                    )
                return embed
            else:
                return self.embed(
                    f"Server `{server.name}` is running (EC2) but Minecraft is not reachable at `{server.ip}`.",
                    discord.Color.orange(),
                )
        elif state == "pending":
            return self.embed(
                f"Server `{server.name}` is starting...",
                discord.Color.yellow(),
            )
        elif state in ("stopping", "shutting-down"):
            return self.embed(
                f"Server `{server.name}` is stopping...",
                discord.Color.orange(),
            )
        elif state == "stopped":
            return self.embed(
                f"Server `{server.name}` is offline.",
                discord.Color.red(),
            )
        else:
            return self.embed(
                f"Server `{server.name}` state is unusual: `{state}`.",
                discord.Color.greyple(),
            )

    async def _get_server_status_field(
        self,
        name: str,
        server: ServerCfg,
    ) -> tuple[str, str]:
        """Get status info for a single server. Returns (name, status_text)."""
        server_state = self.monitor.get_state(name)
        maintenance_suffix = " :wrench:" if server_state and server_state.maintenance else ""
        try:
            state = await self.ec2.get_state(server)
            if state == "running":
                mc_status = await MinecraftService.check_status(server.ip)
                if mc_status.online:
                    return name, f"Online - {mc_status.players} players{maintenance_suffix}"
                else:
                    return name, f"EC2 running, MC starting...{maintenance_suffix}"
            elif state == "stopped":
                return name, f"Offline{maintenance_suffix}"
            elif state in ("pending", "stopping", "shutting-down"):
                return name, f"{state.capitalize()}{maintenance_suffix}"
            else:
                return name, f"{state}{maintenance_suffix}"
        except AWSError:
            return name, "Error"

    async def all_servers_embed(self) -> discord.Embed:
        """Creates an embed showing status of all servers."""
        embed = discord.Embed(title="Server Status", colour=discord.Color.blue())

        # Check all servers in parallel
        tasks_list = [
            self._get_server_status_field(name, server)
            for name, server in self.cfg.servers.items()
        ]
        results = await asyncio.gather(*tasks_list)

        for name, status_text in results:
            server = self.cfg.servers[name]
            embed.add_field(
                name=name,
                value=f"{status_text}\n`{server.ip}`",
                inline=True,
            )

        return embed

    # -- commands ------------------------------------------------------------
    async def cmd_status(
        self,
        channel: discord.abc.Messageable,
        server_name: str | None,
    ) -> None:
        """Handles s!status [server] - show status of one or all servers."""
        if server_name:
            server = self.get_server(server_name)
            if not server:
                await self._send_unknown_server_error(channel, server_name)
                return
            await self.say(channel, embed=await self.status_embed(server))
        else:
            await self.say(channel, embed=await self.all_servers_embed())

    async def cmd_start(
        self,
        channel: discord.abc.Messageable,
        server_name: str | None,
    ) -> None:
        """Handles s!start <server> - start a specific server."""
        if not server_name:
            await self.say(
                channel,
                embed=self.embed(
                    "Usage: `s!start <server>`\nUse `s!list` to see available servers.",
                    discord.Color.red(),
                ),
            )
            return

        server = self.get_server(server_name)
        if not server:
            await self._send_unknown_server_error(channel, server_name)
            return

        # Remember channel for auto-stop notifications (works with DMs too)
        state = self.monitor.get_state(server_name)
        if state and hasattr(channel, "id"):
            state.set_command_channel(channel.id)

        try:
            ec2_state = await self.ec2.get_state(server)
        except AWSError as e:
            await self._send_aws_error(channel, e)
            return

        # Check single server mode before starting
        if self.cfg.single_server_mode and ec2_state != "running":
            running_servers = await self._get_running_servers(exclude=server_name)
            if running_servers:
                servers_list = ", ".join(f"`{s}`" for s in running_servers)
                await self.say(
                    channel,
                    embed=self.embed(
                        f"Cannot start `{server_name}`: single server mode is enabled "
                        f"and {servers_list} is already running. "
                        f"Stop it first with `s!stop` or `s!kill`.",
                        discord.Color.red(),
                    ),
                )
                return

        if ec2_state == "running":
            await self.say(
                channel,
                embed=self.embed(
                    f"Server `{server_name}` is already running!",
                    discord.Color.yellow(),
                ),
            )
            return

        if ec2_state in ("pending", "stopping", "shutting-down"):
            await self.say(
                channel,
                embed=self.embed(
                    f"Server `{server_name}` is currently busy (`{ec2_state}`). Please wait.",
                    discord.Color.orange(),
                ),
            )
            return

        # Start the server
        await self.say(
            channel,
            embed=self.embed(f"Starting server `{server_name}`...", discord.Color.green()),
        )

        try:
            await self.ec2.start_instance(server)
            # Record start time for grace period
            if state:
                state.record_start()
        except AWSError as e:
            await self.say(
                channel,
                embed=self.embed(
                    f"Failed to start server `{server_name}`: {e}",
                    discord.Color.red(),
                ),
            )
            return

        # Update proxy routing to point to this server
        await self._update_proxy(server, channel)

        # Wait for health checks
        await self.say(
            channel,
            embed=self.embed(
                f"Server `{server_name}` EC2 instance is running. Waiting for Minecraft...",
                discord.Color.yellow(),
            ),
        )

        # Progress callback for status updates (only notify once after 180s)
        notified_slow_start = False

        async def on_progress(elapsed: int, total: int) -> None:
            nonlocal notified_slow_start
            if not notified_slow_start and elapsed >= 180:
                notified_slow_start = True
                await self.say(
                    channel,
                    embed=self.embed(
                        f"Still waiting for Minecraft on `{server_name}`... ({elapsed}s/{total}s)",
                        discord.Color.yellow(),
                    ),
                )

        mc_status = await MinecraftService.wait_for_ready(
            server.ip,
            progress_callback=on_progress,
        )

        if mc_status:
            await self.say(
                channel,
                f"Server `{server_name}` is ready!",
                embed=await self.status_embed(server),
            )
        else:
            # Check if server was stopped during startup - don't show misleading message
            try:
                current_state = await self.ec2.get_state(server)
            except AWSError:
                current_state = "unknown"

            if current_state == "running":
                await self.say(
                    channel,
                    embed=self.embed(
                        f"Server `{server_name}` started (EC2), but Minecraft did not respond in time. "
                        "It may still be starting.",
                        discord.Color.orange(),
                    ),
                )

    async def cmd_stop(
        self,
        channel: discord.abc.Messageable | None,
        server_name: str | None,
        auto: bool = False,
    ) -> None:
        """Handles s!stop <server> - stop a specific server."""
        if not server_name:
            if not auto:
                await self.say(
                    channel,
                    embed=self.embed(
                        "Usage: `s!stop <server>`\nUse `s!list` to see available servers.",
                        discord.Color.red(),
                    ),
                )
            return

        server = self.get_server(server_name)
        if not server:
            if not auto and channel:
                await self._send_unknown_server_error(channel, server_name)
            return

        # For auto-stop, get channel from saved state
        state = self.monitor.get_state(server_name)
        if auto and channel is None and state and state.last_command_channel_id:
            retrieved_channel = self.client.get_channel(state.last_command_channel_id)
            if retrieved_channel is not None:
                channel = retrieved_channel

        if channel is None:
            logging.warning(f"cmd_stop called for {server_name} without a valid channel")
            return

        try:
            ec2_state = await self.ec2.get_state(server)
        except AWSError as e:
            await self._send_aws_error(channel, e)
            return

        if ec2_state != "running":
            await self.say(
                channel,
                embed=self.embed(
                    f"Server `{server_name}` is not running (`{ec2_state}`), cannot stop.",
                    discord.Color.yellow(),
                ),
            )
            return

        # Check for players (manual stop only)
        if not auto:
            mc_status = await MinecraftService.check_status(server.ip)
            if not mc_status.online:
                # MC is unreachable - warn user but allow stop
                await self.say(
                    channel,
                    embed=self.embed(
                        f"Warning: Cannot verify player count for `{server_name}` "
                        "(Minecraft not responding). Proceeding with stop.",
                        discord.Color.orange(),
                    ),
                )
            elif mc_status.players > 0:
                await self.say(
                    channel,
                    embed=self.embed(
                        f"Cannot stop `{server_name}`: {mc_status.players} player(s) online!",
                        discord.Color.red(),
                    ),
                )
                return

        # Stop the server
        msg = (
            f"Server `{server_name}` has been empty for {AUTO_STOP_DELAY_MIN} minutes. Stopping..."
            if auto
            else f"Stopping server `{server_name}`..."
        )
        await self.say(channel, embed=self.embed(msg, discord.Color.orange()))

        try:
            # Try graceful stop with timeout, force stop if it takes too long
            try:
                await asyncio.wait_for(
                    self.ec2.stop_instance(server),
                    timeout=STOP_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                await self.say(
                    channel,
                    embed=self.embed(
                        f"Graceful stop timed out for `{server_name}`. Force stopping...",
                        discord.Color.orange(),
                    ),
                )
                await self.ec2.stop_instance(server, force=True)

            # Clear proxy routing since server is stopped
            await self._clear_proxy(channel)

            await self.say(
                channel,
                embed=self.embed(f"Server `{server_name}` stopped!", discord.Color.red()),
            )
            # Reset empty counter on successful stop
            if state:
                state.reset_empty()
        except AWSError as e:
            await self.say(
                channel,
                embed=self.embed(
                    f"Failed to stop server `{server_name}`: {e}",
                    discord.Color.red(),
                ),
            )

    async def cmd_ip(
        self,
        channel: discord.abc.Messageable,
        server_name: str | None,
    ) -> None:
        """Handles s!ip [server] - show IP of one or all servers."""
        if server_name:
            server = self.get_server(server_name)
            if not server:
                await self._send_unknown_server_error(channel, server_name)
                return
            await self.say(
                channel,
                embed=self.embed(
                    f"IP for `{server_name}`: `{server.ip}`",
                    discord.Color.blue(),
                ),
            )
        else:
            # Show all IPs
            lines = [f"- `{name}`: `{srv.ip}`" for name, srv in self.cfg.servers.items()]
            await self.say(
                channel,
                embed=discord.Embed(
                    title="Server IPs",
                    description="\n".join(lines),
                    colour=discord.Color.blue(),
                ),
            )

    async def cmd_list(self, channel: discord.abc.Messageable) -> None:
        """Handles s!list - list all configured servers."""
        await self.say(channel, embed=await self.all_servers_embed())

    async def cmd_kill(
        self,
        channel: discord.abc.Messageable,
        server_name: str | None,
    ) -> None:
        """Handles s!kill <server> - force stop a server immediately."""
        if not server_name:
            await self.say(
                channel,
                embed=self.embed(
                    "Usage: `s!kill <server>`\nUse `s!list` to see available servers.",
                    discord.Color.red(),
                ),
            )
            return

        server = self.get_server(server_name)
        if not server:
            await self._send_unknown_server_error(channel, server_name)
            return

        try:
            ec2_state = await self.ec2.get_state(server)
        except AWSError as e:
            await self._send_aws_error(channel, e)
            return

        if ec2_state not in ("running", "pending", "stopping"):
            await self.say(
                channel,
                embed=self.embed(
                    f"Server `{server_name}` is not running (`{ec2_state}`), cannot kill.",
                    discord.Color.yellow(),
                ),
            )
            return

        # Force stop immediately without checking players
        await self.say(
            channel,
            embed=self.embed(
                f"Force stopping server `{server_name}`...",
                discord.Color.orange(),
            ),
        )

        try:
            await self.ec2.stop_instance(server, force=True)

            # Clear proxy routing since server is stopped
            await self._clear_proxy(channel)

            await self.say(
                channel,
                embed=self.embed(
                    f"Server `{server_name}` has been force stopped!",
                    discord.Color.red(),
                ),
            )
            # Reset empty counter on successful stop
            state = self.monitor.get_state(server_name)
            if state:
                state.reset_empty()
        except AWSError as e:
            await self.say(
                channel,
                embed=self.embed(
                    f"Failed to force stop server `{server_name}`: {e}",
                    discord.Color.red(),
                ),
            )

    async def cmd_maintain(
        self,
        channel: discord.abc.Messageable,
        server_name: str | None,
    ) -> None:
        """Handles s!maintain <server> - toggle maintenance mode."""
        if not server_name:
            await self.say(
                channel,
                embed=self.embed(
                    "Usage: `s!maintain <server>`\nUse `s!list` to see available servers.",
                    discord.Color.red(),
                ),
            )
            return

        server = self.get_server(server_name)
        if not server:
            await self._send_unknown_server_error(channel, server_name)
            return

        state = self.monitor.get_state(server_name)
        if not state:
            await self.say(
                channel,
                embed=self.embed(f"No state found for `{server_name}`.", discord.Color.red()),
            )
            return

        enabled = state.toggle_maintenance()
        if enabled:
            await self.say(
                channel,
                embed=self.embed(
                    f"Maintenance mode **enabled** for `{server_name}`. Auto-stop is paused.",
                    discord.Color.orange(),
                ),
            )
        else:
            await self.say(
                channel,
                embed=self.embed(
                    f"Maintenance mode **disabled** for `{server_name}`. Auto-stop resumed.",
                    discord.Color.green(),
                ),
            )

    async def cmd_help(self, channel: discord.abc.Messageable) -> None:
        """Handles s!help - show command help."""
        await self.say(
            channel,
            embed=discord.Embed(
                title="Minecraft Server Bot Commands",
                description=(
                    "`s!status [server]` - Check server status (all if no server specified)\n"
                    "`s!start <server>` - Start a specific server\n"
                    "`s!stop <server>` - Stop a specific server (if empty)\n"
                    "`s!kill <server>` - Force stop a server immediately\n"
                    "`s!maintain <server>` - Toggle maintenance mode (pauses auto-stop)\n"
                    "`s!ip [server]` - Show server IP(s)\n"
                    "`s!list` - List all servers with status\n"
                    "`s!help` - Display this message"
                ),
                colour=discord.Color.blue(),
            ),
        )

    # -- discord events ------------------------------------------------------
    async def on_ready(self) -> None:
        """Called when the bot successfully connects to Discord."""
        logging.info(f"Logged in as {self.client.user}")
        if not self.monitor_players.is_running():
            logging.info("Starting player monitoring background task.")
            self.monitor_players.start()
        else:
            logging.warning("monitor_players task was already running on_ready.")

    async def on_message(self, message: discord.Message) -> None:
        """Called when a message is sent in a channel the bot can see."""
        if message.author == self.client.user:
            return
        if not message.content.startswith("s!"):
            return

        # Parse the command and arguments
        full_command = message.content[2:].strip()
        parts = full_command.split()
        cmd = parts[0].lower() if parts else ""
        args = parts[1:] if len(parts) > 1 else []
        server_name = args[0] if args else None

        if VERBOSE:
            logging.debug(
                f"Cmd='{cmd}', Args={args} from {message.author} in #{message.channel}"
            )

        # Command dispatcher
        match cmd:
            case "status":
                await self.cmd_status(message.channel, server_name)
            case "start":
                await self.cmd_start(message.channel, server_name)
            case "stop":
                await self.cmd_stop(message.channel, server_name, auto=False)
            case "kill":
                await self.cmd_kill(message.channel, server_name)
            case "maintain":
                await self.cmd_maintain(message.channel, server_name)
            case "ip":
                await self.cmd_ip(message.channel, server_name)
            case "list":
                await self.cmd_list(message.channel)
            case "help":
                await self.cmd_help(message.channel)
            case "":
                await self.say(
                    message.channel,
                    embed=self.embed("Command missing. Try `s!help`.", discord.Color.red()),
                )
            case _:
                await self.say(
                    message.channel,
                    embed=self.embed(
                        f"Unknown command: `{cmd}`. Try `s!help`.",
                        discord.Color.red(),
                    ),
                )

    # -- background job ------------------------------------------------------
    @tasks.loop(minutes=MONITOR_INTERVAL_MIN)
    async def monitor_players(self) -> None:
        """Periodically checks all servers and auto-stops empty ones."""
        await self.client.wait_until_ready()

        for server_state in self.monitor.all_states():
            server = server_state.server

            try:
                ec2_state = await self.ec2.get_state(server)
            except AWSError as e:
                logging.warning(f"Monitor: failed to get state for {server.name}: {e}")
                continue

            # Only monitor running servers
            if ec2_state != "running":
                if server_state.consecutive_empty > 0:
                    logging.info(
                        f"Resetting empty counter for {server.name} (state: {ec2_state})"
                    )
                    server_state.reset_empty()
                continue

            # Skip if in maintenance mode
            if server_state.maintenance:
                logging.debug(f"Skipping {server.name}: maintenance mode enabled")
                continue

            # Skip if in grace period after start
            if server_state.in_grace_period(GRACE_PERIOD_MIN):
                logging.debug(f"Skipping {server.name}: in grace period")
                continue

            # Check Minecraft status
            mc_status = await MinecraftService.check_status(server.ip)
            server_state.record_check(mc_status.players if mc_status.online else 0)

            if mc_status.online and mc_status.players == 0:
                count = server_state.increment_empty()
                logging.info(
                    f"Server {server.name} is empty. "
                    f"Consecutive: {count}/{CONSECUTIVE_EMPTY_LIMIT}"
                )

                if count >= CONSECUTIVE_EMPTY_LIMIT:
                    logging.info(f"Auto-stopping server {server.name} due to inactivity.")
                    await self.cmd_stop(None, server.name, auto=True)
            else:
                if server_state.consecutive_empty > 0:
                    logging.info(
                        f"Resetting empty counter for {server.name}. "
                        f"Players: {mc_status.players}, Online: {mc_status.online}"
                    )
                    server_state.reset_empty()

    # -- entry point ---------------------------------------------------------
    def run(self) -> None:
        """Starts the Discord bot client."""
        try:
            logging.info("Starting Discord client...")
            self.client.run(self.cfg.token)
        except discord.LoginFailure:
            logging.error("Failed to log in. Check your Discord token in the config file.")
        except Exception as e:
            logging.error(f"An unexpected error occurred while running the bot: {e}")
            raise
