# Railway Log Watchdog

Continuously monitors all Railway services for errors and posts alerts to #war-room.

## What It Does

- **Polls every 60 seconds** (configurable) for logs from all services in the project
- **Filters for `severity: error`** log entries
- **Deduplicates** - only alerts on NEW errors, not repeat alerts for the same issue
- **Posts to #war-room** via JUGGERNAUT when errors are detected
- **Tracks crashed services** - alerts when any deployment is in CRASHED state

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RAILWAY_API_TOKEN` | Yes | - | Your Railway API token |
| `RAILWAY_PROJECT_ID` | No | spartan-agents-v2 ID | Project to monitor |
| `JUGGERNAUT_URL` | No | Production URL | JUGGERNAUT endpoint for war-room posts |
| `CHECK_INTERVAL_SECONDS` | No | 60 | How often to check logs |

## Endpoints

- `GET /health` - Health check with stats
- `GET /status` - Detailed status of all monitored services
- `POST /check-now` - Trigger immediate check
- `POST /clear-seen` - Clear seen errors (will re-alert on existing errors)

## How Deduplication Works

Errors are hashed by service name + normalized message (numbers removed to handle timestamps/IDs).
Once an error is seen, it won't alert again until `/clear-seen` is called.

## Deployment

1. Create new service in Railway
2. Connect to this repo
3. Set `RAILWAY_API_TOKEN` environment variable
4. Deploy

The watchdog will start monitoring immediately.
