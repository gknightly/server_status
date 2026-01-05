Discord bot to manage AWS EC2 instances running Minecraft servers.

## Features

- Start/stop multiple Minecraft servers on AWS EC2
- Check server status and player counts
- Auto-stop servers after 15 minutes of inactivity
- Monitor all configured servers simultaneously

## Commands

| Command | Description |
|---------|-------------|
| `s!status [server]` | Show status of all servers, or a specific one |
| `s!start <server>` | Start a specific server |
| `s!stop <server>` | Stop a server (only if empty) |
| `s!ip [server]` | Show server IP address(es) |
| `s!list` | List all servers with current status |
| `s!help` | Show command help |

## Setup

1. Run `sudo docker compose up` once to generate `config.json`
2. Edit `config.json` with your credentials and server details:
   ```json
   {
       "DISCORD_TOKEN": "your-bot-token",
       "AWS_ACCESS_KEY": "your-aws-access-key",
       "AWS_SECRET": "your-aws-secret-key",
       "servers": {
           "survival": {
               "INSTANCE_ID": "i-0123456789abcdef0",
               "AWS_REGION": "us-east-2",
               "SERVER_IP": "1.2.3.4"
           }
       }
   }
   ```
3. Run `sudo docker compose up -d` to start the bot

## How It Works

The bot manages AWS EC2 instances, not the Minecraft server process directly.
Use a systemd service on the EC2 instance to start/stop Minecraft on boot/shutdown.

### Auto-Stop

Servers are automatically stopped after 15 minutes with no players online.
The bot checks all configured servers every 5 minutes.
