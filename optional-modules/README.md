# optional-modules

First-party modules that ship with the vibebot repository but are not
auto-loaded as built-ins. Add them to your bot by registering this repo in
`config.toml` with `subdir = "optional-modules"`, then enable the desired
module from the admin UI or API.

Example `config.toml` entry:

```toml
[[repos]]
name = "vibebot-optional"
url = "https://github.com/hnsk/vibebot.git"
branch = "main"
subdir = "optional-modules"
```

After the next start the bot clones the repo; enable a module via the web UI
(`/modules`) or API.

## Modules

| Name       | Description                                          |
|------------|------------------------------------------------------|
| `remindme` | Let users schedule personal reminders via chat. Syntax: `!remindme <when> <message>` (e.g. `5m`, `1h30m`, `1 day`). |
| `urltitle` | Auto-post page titles for URLs seen in chat. Skips images/video/binaries, prefers `og:title`, caches fetched titles in SQLite (refetched after 10 min). |
| `youtubeinfo` | Post YouTube video info (title, channel, duration, age) when a `youtube.com` / `youtu.be` / `shorts` / `live` URL is seen. Requires a YouTube Data API v3 key. Caches per video ID (default 60 min). Uses only IRC bold — no colour codes. |
| `twitterinfo` | Post tweet / x.com status info (author, text, age, engagement, media) when a `twitter.com` / `x.com` / `fxtwitter.com` / `vxtwitter.com` / `fixupx.com` status URL is seen. No API key — uses Twitter's public syndication endpoint. Optional opt-in fallback to `api.fxtwitter.com`. Caches per tweet ID (default 60 min). Uses only IRC bold — no colour codes. |
