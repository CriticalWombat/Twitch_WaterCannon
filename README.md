# 💧 Water Cannon – Twitch → GPIO Controller

Listens for a Twitch channel point redemption named **waterCannon** and
pulses a Raspberry Pi GPIO pin HIGH for 5 seconds to trigger a relay/valve.

---

## Project layout

```
water-cannon/
├── app/
│   ├── main.py           # Application logic
│   └── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example          # Copy to .env and fill in your values
└── README.md
```

---

## Prerequisites

| What | Where |
|------|-------|
| Twitch application (Client ID + Secret) | https://dev.twitch.tv/console |
| User **refresh** token (`channel:read:redemptions` scope) | https://twitchtokengenerator.com |
| Broadcaster numeric user ID | https://www.streamweasels.com/tools/convert-twitch-username-to-user-id/ |

---

## Setup

### 1. Create the channel point reward on Twitch

In your **Creator Dashboard → Viewer Rewards → Channel Points → Manage Rewards**,
create a reward with the title **exactly** `waterCannon` (case-sensitive).

### 2. Get a refresh token (one-time)

1. Go to https://twitchtokengenerator.com
2. Enter your Client ID and Client Secret
3. Enable the `channel:read:redemptions` scope
4. Copy the **Refresh Token** (not the access token) into `.env`

The app handles all future token refreshes automatically from this point on.

### 3. Configure environment

```bash
cp .env.example .env
nano .env   # fill in CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, BROADCASTER_ID, GPIO_PIN
```

### 4. Build & run

```bash
docker compose up --build -d
```

### 5. Watch logs

```bash
docker compose logs -f
```

---

## Token refresh behaviour

| Scenario | What happens |
|---|---|
| Container starts | Access token is refreshed immediately from the refresh token |
| API returns 401 | Token is refreshed on the spot and the request is retried once |
| Refresh token rotates | New refresh token is stored in memory and logged (see note below) |

> ⚠️ **Refresh token rotation:** Twitch rotates the refresh token each time it
> is used. The new token is kept in memory for the lifetime of the container.
> If the container is **restarted**, it will use the original `TWITCH_REFRESH_TOKEN`
> from `.env`. As long as the container hasn't been restarted since the last
> rotation, the in-memory token is still valid and a fresh access token will be
> fetched on startup. In practice this is fine for a long-running Pi deployment.
> If you ever need to recover the latest rotated token, it is printed in the logs
> any time rotation is detected.

---

## GPIO wiring

```
Pi BCM 18 (pin 12)  ──→  relay/MOSFET IN
Pi GND              ──→  relay/MOSFET GND
```

> ⚠️  **Never drive a solenoid or motor directly from a GPIO pin.**
> Use a relay module or MOSFET with a flyback diode rated for your load.

---

## Placeholders to fill in

| Placeholder | File | Description |
|---|---|---|
| `PLACEHOLDER_CLIENT_ID` | `.env` | Twitch app Client ID |
| `PLACEHOLDER_CLIENT_SECRET` | `.env` | Twitch app Client Secret |
| `PLACEHOLDER_REFRESH_TOKEN` | `.env` | User refresh token |
| `PLACEHOLDER_BROADCASTER_ID` | `.env` | Numeric channel ID |
| `GPIO_PIN=18` | `.env` | Your actual BCM pin |

---

## Behaviour notes

- If multiple redemptions arrive while the cannon is firing, they are
  **queued** and fired in order of arrival. Each queued shot fires its full
  pulse duration before the next one begins. The queue is unbounded, so
  every redemption is honoured — long viewer sessions will drain gradually.
- The app reconnects automatically on WebSocket drops.
- In dry-run mode (non-Pi hardware / no RPi.GPIO), GPIO calls are logged
  but not executed — safe for local testing.
